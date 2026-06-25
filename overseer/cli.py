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


def cmd_setup(args):
    print(f"\n=== Overseer setup (v{__version__}) ===\n")
    cfg = configmod.load()

    # 1. provider
    prov = _choose("Which AI backend should power the agent?",
                   ["gemini-api  (Google AI Studio key - free, easy)",
                    "groq  (Groq API key - free + very fast, recommended)",
                    "claude  (Anthropic API key - strongest)"],
                   default_idx=0).split()[0]
    cfg["provider"] = prov

    # 2. credentials
    if prov == "gemini-oauth":
        print("Using OAuth token from ~/.gemini/oauth_creds.json (run `gemini` once to log in if needed).")
        cfg["api_key"] = ""
    else:
        label = {"gemini-api": "Google AI Studio API key", "groq": "Groq API key", "claude": "Anthropic API key"}[prov]
        cfg["api_key"] = _ask(f"Paste your {label}", default=cfg.get("api_key") or None, secret=True)

    # 3. model
    chain = providers.PROVIDERS[prov].default_models
    print(f"Default model chain for {prov}: {', '.join(chain)}")
    m = _ask("Model id (blank = use the default chain)", default="")
    cfg["model"] = m or None

    # 4. telegram token
    cfg["telegram_token"] = _ask("Telegram bot token (from @BotFather)", default=cfg.get("telegram_token") or None, secret=True)

    # 5. chat-id auto-detect (pause the running service first so it doesn't eat the update)
    tg = Telegram(cfg["telegram_token"])
    try:
        me = tg.get_me()
        print(f"Connected to bot: @{me['result']['username']}")
    except Exception as e:
        print(f"! couldn't reach the bot ({e}). Double-check the token.")
    paused = _sc("is-active", SERVICE).strip() == "active"
    if paused:
        print("(pausing the running agent so it doesn't grab the message)")
        _sc("stop", SERVICE)
    tg.delete_webhook(False)  # keep pending updates - don't drop the user's message
    ids = []
    print("\nOpen Telegram and send ANY message to your bot now.")
    input("Press Enter AFTER you've sent it... ")
    print("listening for your message (up to ~20s)...")
    off, deadline = 0, time.time() + 20
    while time.time() < deadline and not ids:
        try:
            for u in tg.get_updates(offset=off, timeout=3).get("result", []):
                off = u["update_id"] + 1
                ch = (u.get("message") or u.get("edited_message") or {}).get("chat", {})
                if ch.get("id") and str(ch["id"]) not in ids:
                    ids.append(str(ch["id"]))
                    print(f"  got it -> {ch['id']}  ({ch.get('username') or ch.get('first_name', '?')})")
        except Exception:
            time.sleep(1)
    if ids:
        keep = _ask(f"Lock the agent to these chat id(s) {','.join(ids)}? (comma-list to edit)", default=",".join(ids))
        cfg["allowed_chat_ids"] = [x.strip() for x in keep.split(",") if x.strip()]
    else:
        manual = _ask("Enter the allowed Telegram chat id(s), comma-separated")
        cfg["allowed_chat_ids"] = [x.strip() for x in manual.split(",") if x.strip()]
    cfg["owner_chat_id"] = cfg["allowed_chat_ids"][0] if cfg["allowed_chat_ids"] else ""

    # 6. protected services
    prot = _ask("Critical services the agent must NEVER touch without asking (comma-list, blank to skip)", default="")
    cfg["protected_services"] = [x.strip() for x in prot.split(",") if x.strip()]

    path = configmod.save(cfg)
    print(f"\nSaved config -> {path}")

    # 7. validate
    print("\nRunning a quick checkup...")
    for name, ok, detail in doctor.run_checks(cfg):
        print(f"  [{'OK' if ok else 'XX'}] {name}: {detail}")

    # 8. offer install
    if os.name == "posix" and _ask("Install as a 24/7 systemd service now? (y/n)", default="y").lower().startswith("y"):
        cmd_install(args)
    else:
        if paused:
            _sc("start", SERVICE)
            print("(resumed the running agent with the new config)")
        print("\nDone. Start it anytime with:  overseer install  (service)  or  overseer run  (foreground)")


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
