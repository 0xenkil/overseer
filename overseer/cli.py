"""Overseer CLI:  overseer <setup|run|doctor|status|provider|install|start|stop|restart|logs>"""
import argparse
import os
import subprocess
import sys
import time

from . import __version__
from . import config as configmod
from . import providers
from . import doctor
from .telegram import Telegram

SERVICE = "overseer"
UNIT_PATH = "/etc/systemd/system/overseer.service"
WRAPPER = "/usr/local/bin/overseer"


def _ask(prompt, default=None, secret=False):
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if val:
            return val


def _choose(prompt, options, default_idx=0):
    print(prompt)
    for i, o in enumerate(options):
        print(f"  {i + 1}) {o}")
    while True:
        raw = input(f"Pick 1-{len(options)} [{default_idx + 1}]: ").strip()
        if not raw:
            return options[default_idx]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]


KEY_HELP = {
    "gemini-api": ("Google Gemini API key (free)", "https://aistudio.google.com/apikey",
                   ["Open the link, sign in with Google",
                    "Click 'Create API key'",
                    "Copy it (starts with 'AIza...')"]),
    "groq": ("Groq API key (free)", "https://console.groq.com/keys",
             ["Open the link, sign in (Google or GitHub)",
              "Click 'Create API Key'",
              "Copy it (starts with 'gsk_...')"]),
    "claude": ("Anthropic Claude API key", "https://console.anthropic.com/settings/keys",
               ["Open the link, sign in",
                "Create a key under 'API Keys'",
                "Copy it (starts with 'sk-ant-...')"]),
}

TG_COMMANDS = [
    {"command": "help", "description": "what I can do"},
    {"command": "status", "description": "server + agent health"},
    {"command": "model", "description": "which AI is running"},
    {"command": "new", "description": "start fresh (clear memory)"},
    {"command": "whoami", "description": "your id + backend"},
]


def cmd_setup(args):
    print(f"\n========== Overseer setup (v{__version__}) ==========")
    print("Three quick steps - I'll tell you exactly what to do.\n")
    cfg = configmod.load()

    # ---- STEP 1: the AI brain ----
    print("STEP 1 of 3  -  Choose the AI brain\n")
    pick = _choose("Which AI should run your agent?",
                   ["Gemini  -  free & easy, great all-rounder",
                    "Groq    -  free & blazing fast   (recommended)",
                    "Claude  -  smartest, but paid"],
                   default_idx=1)
    prov = {"Gemini": "gemini-api", "Groq": "groq", "Claude": "claude"}[pick.split()[0]]
    cfg["provider"] = prov
    cfg["model"] = None  # auto-pick - users never deal with model ids

    label, link, steps = KEY_HELP[prov]
    print(f"\nYou'll need a {label}. Here's how to get one:")
    print(f"   -> {link}")
    for i, s in enumerate(steps, 1):
        print(f"      {i}. {s}")
    cfg["api_key"] = _ask(f"\nPaste your {label} here")

    print("   checking the key ...", end=" ", flush=True)
    try:
        ok, detail = providers.build(cfg, "ping").ping()
        if ok:
            print("works!")
        elif any(x in detail.lower() for x in ("429", "quota", "rate", "exhaust")):
            print("valid (just rate-limited right now - that's fine).")
        else:
            print(f"rejected: {detail[:50]} ... you can fix it later with `overseer provider`.")
    except Exception as e:
        print(f"couldn't verify ({str(e)[:40]}).")

    # ---- STEP 2: the Telegram bot ----
    print("\nSTEP 2 of 3  -  Create your Telegram bot (this is how you'll chat with it)\n")
    print("   1. In Telegram, open a chat with  @BotFather")
    print("   2. Send:  /newbot")
    print("   3. Give it a name, then a username that ends in 'bot'")
    print("   4. BotFather sends you a token like  123456789:AAE-xxxxxxxxxxxxxxxx")
    cfg["telegram_token"] = _ask("\nPaste that bot token here")
    tg = Telegram(cfg["telegram_token"])
    botname = "your bot"
    try:
        botname = "@" + tg.get_me()["result"]["username"]
        print(f"   connected to {botname}")
    except Exception as e:
        print(f"   ! couldn't reach that bot ({str(e)[:50]}) - re-run setup with the right token.")

    # ---- STEP 3: lock it to you ----
    print("\nSTEP 3 of 3  -  Lock the bot to you (so only you can command it)\n")
    paused = _sc("is-active", SERVICE).strip() == "active"
    if paused:
        _sc("stop", SERVICE)
    tg.delete_webhook(False)
    ids = []
    print(f"   Open {botname} in Telegram and send it any message (e.g. 'hi').")
    input("   Press Enter AFTER you've sent it ... ")
    print("   looking for your message ...", end=" ", flush=True)
    off, deadline = 0, time.time() + 25
    while time.time() < deadline and not ids:
        try:
            for u in tg.get_updates(offset=off, timeout=3).get("result", []):
                off = u["update_id"] + 1
                ch = (u.get("message") or u.get("edited_message") or {}).get("chat", {})
                if ch.get("id") and str(ch["id"]) not in ids:
                    ids.append(str(ch["id"]))
        except Exception:
            time.sleep(1)
    if ids:
        print(f"got you (chat id {ids[0]}).")
        cfg["allowed_chat_ids"] = ids
    else:
        print("didn't catch it.")
        manual = _ask("   No problem - message @userinfobot in Telegram to get your id number, paste it here")
        cfg["allowed_chat_ids"] = [x.strip() for x in manual.split(",") if x.strip()]
    cfg["owner_chat_id"] = cfg["allowed_chat_ids"][0] if cfg["allowed_chat_ids"] else ""
    cfg["protected_services"] = cfg.get("protected_services") or []

    path = configmod.save(cfg)
    tg.set_my_commands(TG_COMMANDS)  # so the '/' menu shows up in Telegram
    print(f"\nSaved ✓  ({path})")

    print("")
    if os.name == "posix" and _ask("Run it 24/7 now (install as a background service)? (y/n)", default="y").lower().startswith("y"):
        cmd_install(args)
        print(f"\nAll set! Open {botname} in Telegram and say hi. \U0001F47B")
    else:
        if paused:
            _sc("start", SERVICE)
        print(f"\nDone. Start it with:  overseer install   - then open {botname} in Telegram.")


def _service_unit():
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = sys.executable or "/usr/bin/python3"
    cfg_path = configmod.config_path()
    return f"""[Unit]
Description=Overseer agent (Telegram AI operator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH={pkg_parent}
Environment=OVERSEER_CONFIG={cfg_path}
ExecStart={py} -m overseer run
Restart=always
RestartSec=3
MemoryHigh=450M

[Install]
WantedBy=multi-user.target
"""


def cmd_install(args):
    if os.name != "posix":
        return print("systemd install is Linux-only. On the VPS run: overseer install")
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = sys.executable or "/usr/bin/python3"
    try:
        with open(UNIT_PATH, "w") as f:
            f.write(_service_unit())
        with open(WRAPPER, "w") as f:
            f.write(f'#!/bin/sh\nexec env PYTHONPATH="{pkg_parent}" "{py}" -m overseer "$@"\n')
        os.chmod(WRAPPER, 0o755)
    except PermissionError:
        return print("Need root to install the service. Re-run with sudo:  sudo overseer install")
    _sc("daemon-reload")
    _sc("enable", "--now", SERVICE)
    time.sleep(3)
    print("Installed + started. Manage with: overseer status | logs | restart")
    cmd_status(args)


def _sc(*a):
    try:
        return subprocess.run(["systemctl", *a], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return "(systemctl not found)"


def cmd_run(args):
    from .agent import main
    main()


def cmd_doctor(args):
    print(doctor.format_report(doctor.run_checks(configmod.load())))


def cmd_status(args):
    print(_sc("is-active", SERVICE).strip() or "unknown")
    print(_sc("status", SERVICE, "--no-pager")[:1200])


def cmd_provider(args):
    cfg = configmod.load()
    prov = _choose("Switch backend to:", list(providers.PROVIDERS.keys()),
                   default_idx=list(providers.PROVIDERS).index(cfg.get("provider", "gemini-api")))
    cfg["provider"] = prov
    if prov != "gemini-oauth":
        cfg["api_key"] = _ask(f"{prov} API key", default=cfg.get("api_key") or None)
    cfg["model"] = None
    configmod.save(cfg)
    ok, detail = providers.build(cfg, "ping").ping()
    print(f"Switched to {prov}: {'OK' if ok else 'FAILED'} ({detail})")
    if _ask("Restart the service to apply? (y/n)", default="y").lower().startswith("y"):
        _sc("restart", SERVICE)
        print("restarted.")


def cmd_logs(args):
    os.execvp("journalctl", ["journalctl", "-u", SERVICE, "-n", "60", "-f"])


def cmd_simple(name):
    def f(args):
        print(_sc(name, SERVICE))
    return f


def main(argv=None):
    p = argparse.ArgumentParser(prog="overseer", description="Lean self-healing AI agent for your server, over Telegram.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="interactive first-time setup").set_defaults(fn=cmd_setup)
    sub.add_parser("run", help="run the agent in the foreground").set_defaults(fn=cmd_run)
    sub.add_parser("install", help="install + start the systemd service").set_defaults(fn=cmd_install)
    sub.add_parser("doctor", help="run a health checkup").set_defaults(fn=cmd_doctor)
    sub.add_parser("status", help="service status").set_defaults(fn=cmd_status)
    sub.add_parser("provider", help="switch AI backend").set_defaults(fn=cmd_provider)
    sub.add_parser("logs", help="tail the service logs").set_defaults(fn=cmd_logs)
    sub.add_parser("start").set_defaults(fn=cmd_simple("start"))
    sub.add_parser("stop").set_defaults(fn=cmd_simple("stop"))
    sub.add_parser("restart").set_defaults(fn=cmd_simple("restart"))
    p.add_argument("--version", action="version", version=f"overseer {__version__}")
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        return p.print_help()
    args.fn(args)
