"""Overseer runtime: Telegram long-poll -> provider (function-calling loop) -> tools.
Single worker (serial, safe), background typing indicator, periodic self-health checks,
and doctor alerts on failure. systemd Restart=always is the outer watchdog."""
import json
import os
import queue
import threading
import time
import traceback

from . import config as configmod
from . import providers
from . import tools as toolmod
from . import doctor
from . import watchdog
from .persona import system_prompt
from .telegram import Telegram


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# models offered by /model (you can also type any model id your backend supports)
MODELS_BY_PROVIDER = {
    "gemini-api": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-pro"],
    "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant",
             "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct"],
    "claude": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"],
}


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        # remember the active provider's existing key so /provider can switch back to it
        if cfg.get("api_key") and cfg.get("provider") not in (cfg.get("keys") or {}):
            cfg.setdefault("keys", {})[cfg["provider"]] = cfg["api_key"]
        os.makedirs(cfg["state_dir"], exist_ok=True)
        self.tg = Telegram(cfg["telegram_token"])
        self.system = system_prompt(cfg.get("protected_services"))
        self.provider = providers.build(cfg, self.system, log)
        self.dispatch = toolmod.dispatch(cfg.get("cmd_timeout", 180), log)
        self.allowed = set(cfg.get("allowed_chat_ids", []))
        self.q = queue.Queue()
        self.last_alert = 0.0

    # --- conversation memory ---
    def _hp(self, cid):
        return os.path.join(self.cfg["state_dir"], f"hist_{cid}.json")

    def load(self, cid):
        try:
            with open(self._hp(cid)) as f:
                return json.load(f)
        except Exception:
            return []

    def save(self, cid, hist):
        if len(hist) > 80:
            hist = hist[-80:]
        try:
            with open(self._hp(cid) + ".tmp", "w") as f:
                json.dump(hist, f)
            os.replace(self._hp(cid) + ".tmp", self._hp(cid))
        except Exception as e:
            log("save err", e)

    # --- the agentic loop for one message ---
    def run_task(self, cid, text):
        hist = self.load(cid)
        hist += self.provider.user_turn(text)
        for _ in range(self.cfg.get("max_tool_iters", 25)):
            reply = None
            for _attempt in range(5):  # on 413: trim old tool outputs and retry
                try:
                    reply = self.provider.chat(hist)
                    break
                except providers.RequestTooLarge:
                    hist, changed = self.provider.compact(hist)
                    if not changed:
                        break
            if reply is None:
                self.save(cid, hist)
                return ("That task pulled more data than the model's token limit allows, even after trimming. "
                        "Try a narrower request, or switch model with /model.")
            hist.append(reply.assistant_turn)
            if reply.calls:
                results = []
                for c in reply.calls:
                    fn = self.dispatch.get(c["name"])
                    out = fn(c["args"]) if fn else {"error": "unknown tool " + str(c["name"])}
                    results.append({"id": c.get("id"), "name": c["name"], "output": out})
                hist += self.provider.tool_results_turn(results)
                continue
            self.save(cid, hist)
            return reply.text or "(done)"
        self.save(cid, hist)
        return "Hit the step limit - partial work may be done. Say 'continue' and I'll keep going."

    # --- per-message handling ---
    def handle(self, cid, text):
        if text.startswith("/") and self.command(cid, text):
            return
        self.tg.send_chat_action(cid)
        stop = threading.Event()
        threading.Thread(target=self._keep_typing, args=(cid, stop), daemon=True).start()
        try:
            self.tg.send(cid, self.run_task(cid, text))
        except providers.AuthError as e:
            self.tg.send(cid, doctor.alert(self.cfg, e))
        except Exception as e:
            log("task err", traceback.format_exc())
            _, msg, fix = doctor.diagnose(e)
            self.tg.send(cid, f"⚠ {msg}.\nFix: {fix}")
        finally:
            stop.set()

    def _keep_typing(self, cid, stop):
        while not stop.wait(6):
            self.tg.send_chat_action(cid)

    HELP = ("I'm Overseer - I run this server for you. Just tell me what you need, plain English:\n"
            "  • \"is everything healthy?\"\n"
            "  • \"what's eating disk / memory?\"\n"
            "  • \"restart nginx\" / \"set up a nightly backup\"\n"
            "  • \"look up X\" / recon & research\n\n"
            "Commands:\n"
            "/status – server + agent health\n"
            "/model – show / switch the AI model\n"
            "/provider – show / switch backend (gemini·groq·claude)\n"
            "/setkey – add an API key:  /setkey groq gsk_...\n"
            "/new – forget this chat, start fresh\n"
            "/whoami – your id + backend\n"
            "/help – this message")

    def _rebuild_provider(self):
        self.provider = providers.build(self.cfg, self.system, log)

    def command(self, cid, text):
        parts = text.split()
        c = parts[0].lower().split("@")[0]
        arg = " ".join(parts[1:]).strip()
        if c in ("/start", "/help"):
            self.tg.send(cid, self.HELP); return True
        if c in ("/new", "/reset"):
            self.save(cid, []); self.tg.send(cid, "Done – memory cleared, fresh start."); return True
        if c == "/status":
            self.tg.send(cid, doctor.format_report(doctor.run_checks(self.cfg))); return True
        if c == "/whoami":
            self.tg.send(cid, f"backend: {self.cfg.get('provider')}\nmodel: {self.provider.models[0]}\nyour chat id: {cid}"); return True
        if c == "/model":
            return self._cmd_model(cid, arg)
        if c in ("/provider", "/providers"):
            return self._cmd_provider(cid, arg) if (c == "/provider" and arg) else self._cmd_providers(cid)
        if c == "/setkey":
            return self._cmd_setkey(cid, arg)
        self.tg.send(cid, f"I don't know {c}. Try /help."); return True

    def _cmd_model(self, cid, arg):
        prov = self.cfg.get("provider")
        cur = self.provider.models[0]
        if not arg:
            opts = MODELS_BY_PROVIDER.get(prov, [])
            lines = [f"🧠 *Current:* `{prov}/{cur}`", "", "Switch with `/model <name>`:"]
            lines += [("✅ " if m == cur else "• ") + f"`{m}`" for m in opts]
            lines.append("_(or type any model id your backend supports)_")
            self.tg.send(cid, "\n".join(lines)); return True
        self.cfg["model"] = arg
        configmod.save(self.cfg)
        self._rebuild_provider()
        ok, detail = self.provider.ping()
        self.tg.send(cid, f"{'✅' if ok else '⚠️'} Model → `{prov}/{arg}`" + ("" if ok else f"\n_{detail[:60]}_")); return True

    def _cmd_provider(self, cid, arg):
        prov = arg.lower().split()[0]
        if prov not in providers.PROVIDERS:
            self.tg.send(cid, f"Unknown backend. Options: {', '.join(providers.PROVIDERS)}"); return True
        has_key = (self.cfg.get("keys") or {}).get(prov) or (prov == self.cfg.get("provider") and self.cfg.get("api_key"))
        if not has_key:
            self.tg.send(cid, f"No API key for *{prov}* yet.\nAdd one first: `/setkey {prov} <your-key>`"); return True
        self.cfg["provider"] = prov
        self.cfg["model"] = None
        if (self.cfg.get("keys") or {}).get(prov):
            self.cfg["api_key"] = self.cfg["keys"][prov]
        configmod.save(self.cfg)
        self._rebuild_provider()
        ok, detail = self.provider.ping()
        self.tg.send(cid, f"{'✅' if ok else '⚠️'} Backend → *{prov}* (`{self.provider.models[0]}`)" + ("" if ok else f"\n_{detail[:60]}_")); return True

    def _cmd_setkey(self, cid, arg):
        sp = arg.split()
        if len(sp) < 2:
            self.tg.send(cid, "Usage: `/setkey <provider> <api-key>`\ne.g. `/setkey groq gsk_...`\nBackends: " + ", ".join(providers.PROVIDERS)); return True
        prov, key = sp[0].lower(), sp[1]
        if prov not in providers.PROVIDERS:
            self.tg.send(cid, f"Unknown backend. Options: {', '.join(providers.PROVIDERS)}"); return True
        self.cfg.setdefault("keys", {})[prov] = key
        if prov == self.cfg.get("provider"):
            self.cfg["api_key"] = key
            self._rebuild_provider()
        configmod.save(self.cfg)
        test = dict(self.cfg); test["provider"] = prov; test["api_key"] = key; test["model"] = None
        try:
            ok, detail = providers.build(test, "ping").ping()
        except Exception as e:
            ok, detail = False, str(e)
        head = f"✅ Key works — saved for *{prov}*." if ok else f"⚠️ Saved, but the key was rejected: _{detail[:50]}_"
        self.tg.send(cid, head + f"\nUse it: `/provider {prov}`\n🔒 Delete your /setkey message so the key isn't left in chat.")
        return True

    def _cmd_providers(self, cid):
        keys = self.cfg.get("keys") or {}
        active = self.cfg.get("provider")
        lines = ["🔌 *Backends:*"]
        for p in providers.PROVIDERS:
            has = bool(keys.get(p) or (p == active and self.cfg.get("api_key")))
            tag = "✅ *active*" if p == active else ("🔑 key set" if has else "— no key")
            lines.append(f"• `{p}`  {tag}")
        lines.append("\n`/provider <name>` switch · `/setkey <name> <key>` add key · `/model` change model")
        self.tg.send(cid, "\n".join(lines)); return True

    # --- workers ---
    def worker(self):
        while True:
            cid, text = self.q.get()
            try:
                self.handle(cid, text)
            except Exception:
                log("worker err", traceback.format_exc())

    def health_loop(self):
        while True:
            time.sleep(600)
            try:
                ok, detail = self.provider.ping()
                if not ok and (time.time() - self.last_alert) > 3600:
                    self.last_alert = time.time()
                    owner = self.cfg.get("owner_chat_id")
                    if owner:
                        self.tg.send(owner, doctor.alert(self.cfg, providers.AuthError(detail)))
            except Exception as e:
                log("health err", e)

    def watchdog_loop(self):
        """Proactively message the owner when the box does something unusual."""
        if not self.cfg.get("watch_enabled", True):
            return
        owner = self.cfg.get("owner_chat_id")
        interval = self.cfg.get("watch_interval", 300)
        prev = None
        while True:
            try:
                cur = watchdog.snapshot(self.cfg)
                for alert in watchdog.diff(prev, cur, self.cfg):
                    log("watchdog:", alert)
                    if owner:
                        self.tg.send(owner, "👁 *Overseer noticed:*\n" + alert)
                prev = cur
            except Exception as e:
                log("watchdog err", e)
            time.sleep(interval)

    def run(self):
        log(f"Overseer up. provider={self.cfg.get('provider')} models={self.provider.models} allowed={sorted(self.allowed)}")
        self.tg.delete_webhook(True)
        self.tg.set_my_commands([
            {"command": "help", "description": "what I can do"},
            {"command": "status", "description": "server + agent health"},
            {"command": "model", "description": "show / switch the AI model"},
            {"command": "provider", "description": "show / switch backend"},
            {"command": "setkey", "description": "add an API key (/setkey groq <key>)"},
            {"command": "new", "description": "start fresh (clear memory)"},
            {"command": "whoami", "description": "your id + backend"},
        ])
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.health_loop, daemon=True).start()
        threading.Thread(target=self.watchdog_loop, daemon=True).start()
        offset = 0
        while True:
            try:
                r = self.tg.get_updates(offset)
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message") or u.get("edited_message")
                    if not m:
                        continue
                    cid = str(m["chat"]["id"])
                    txt = m.get("text", "")
                    if self.allowed and cid not in self.allowed:
                        log("ignore unauthorized chat", cid)
                        continue
                    if txt:
                        self.q.put((cid, txt))
            except Exception as e:
                log("poll err", repr(e))
                time.sleep(3)


def main(cfg=None):
    main_cfg = cfg or configmod.load()
    if not main_cfg.get("telegram_token") or not main_cfg.get("api_key") and main_cfg.get("provider") != "gemini-oauth":
        raise SystemExit("Overseer is not configured. Run:  overseer setup")
    Agent(main_cfg).run()
