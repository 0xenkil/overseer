"""Config load/save. JSON file at ~/.overseer/config.json (override with $OVERSEER_CONFIG).
Env vars (OVERSEER_PROVIDER, OVERSEER_API_KEY, ...) override the file for containers/systemd."""
import json
import os

DEFAULT_PATH = os.environ.get("OVERSEER_CONFIG") or os.path.expanduser("~/.overseer/config.json")

DEFAULTS = {
    "provider": "gemini-api",        # gemini-api | gemini-oauth | groq | claude
    "model": None,                   # None -> provider's built-in fallback chain
    "api_key": "",                   # gemini-api / groq / claude (not needed for gemini-oauth)
    "telegram_token": "",
    "allowed_chat_ids": [],          # only these Telegram chat ids may command the agent
    "cmd_timeout": 180,              # per-shell-command timeout (s)
    "max_tool_iters": 25,            # max tool calls per user message
    "protected_services": [],        # systemd services the agent must not touch w/o confirmation
    "owner_chat_id": "",             # where the doctor + watchdog send alerts (defaults to first allowed)
    "state_dir": "",                 # conversation memory; defaults next to the config
    # proactive watchdog - messages the owner when the box does something unusual
    "watch_enabled": True,
    "watch_interval": 300,           # seconds between checks
    "watch_disk_pct": 90,            # alert when disk crosses this %
    "watch_mem_mb": 100,             # alert when available memory drops below this
    "watch_load_mult": 4,            # alert when 5-min load > this * cpu cores
}


def config_path():
    return DEFAULT_PATH


def load(path=None):
    path = path or DEFAULT_PATH
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    for k in ("provider", "model", "api_key", "telegram_token"):
        ev = os.environ.get("OVERSEER_" + k.upper())
        if ev:
            cfg[k] = ev
    if os.environ.get("OVERSEER_ALLOWED_CHAT_IDS"):
        cfg["allowed_chat_ids"] = [x.strip() for x in os.environ["OVERSEER_ALLOWED_CHAT_IDS"].split(",") if x.strip()]
    cfg["allowed_chat_ids"] = [str(x) for x in cfg.get("allowed_chat_ids", [])]
    if not cfg.get("owner_chat_id") and cfg["allowed_chat_ids"]:
        cfg["owner_chat_id"] = cfg["allowed_chat_ids"][0]
    if not cfg.get("state_dir"):
        cfg["state_dir"] = os.path.join(os.path.dirname(os.path.abspath(path)), "state")
    return cfg


def save(cfg, path=None):
    path = path or DEFAULT_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path
