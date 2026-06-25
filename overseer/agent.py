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
from .persona import system_prompt
from .telegram import Telegram


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
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
            reply = self.provider.chat(hist)
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
            "/model – which AI is running\n"
            "/new – forget this chat, start fresh\n"
            "/whoami – your id + backend\n"
            "/help – this message")

    def command(self, cid, text):
        c = text.split()[0].lower().split("@")[0]
        if c in ("/start", "/help"):
            self.tg.send(cid, self.HELP)
            return True
        if c in ("/new", "/reset"):
            self.save(cid, [])
            self.tg.send(cid, "Done – memory cleared, fresh start.")
            return True
        if c == "/status":
            self.tg.send(cid, doctor.format_report(doctor.run_checks(self.cfg)))
            return True
        if c == "/whoami":
            self.tg.send(cid, f"backend: {self.cfg.get('provider')}\nmodel: {self.provider.models[0]}\nyour chat id: {cid}")
            return True
        if c == "/model":
            fb = ", ".join(self.provider.models[1:]) or "none"
            self.tg.send(cid, f"Running: {self.cfg.get('provider')} / {self.provider.models[0]}\nFallbacks: {fb}\n"
                              f"To switch, on the server run:  overseer provider")
            return True
        self.tg.send(cid, f"I don't know {c}. Try /help.")
        return True

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

    def run(self):
        log(f"Overseer up. provider={self.cfg.get('provider')} models={self.provider.models} allowed={sorted(self.allowed)}")
        self.tg.delete_webhook(True)
        self.tg.set_my_commands([
            {"command": "help", "description": "what I can do"},
            {"command": "status", "description": "server + agent health"},
            {"command": "model", "description": "which AI is running"},
            {"command": "new", "description": "start fresh (clear memory)"},
            {"command": "whoami", "description": "your id + backend"},
        ])
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.health_loop, daemon=True).start()
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
