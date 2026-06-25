"""The doctor: health checks + failure diagnosis + self-healing reports.

When the agent hits trouble (bad key, rate limit, network), the doctor classifies it
and tells the owner exactly what's wrong and how to fix it - in plain language, over
Telegram. `overseer doctor` runs the full checkup from the CLI."""
import json
import shutil

from . import providers
from .telegram import Telegram


def diagnose(exc):
    """Classify an exception -> (category, human message, suggested fix)."""
    s = str(exc).lower()
    if "413" in s or "too large" in s:
        return ("too-big", "that task pulled more data than the free token-per-minute limit allows in one request",
                "ask something narrower, switch to a lighter model (`overseer provider` -> Fastest), or raise limits on Groq's free Dev tier")
    if isinstance(exc, providers.AuthError) or "401" in s or "403" in s or ("key" in s and "invalid" in s):
        return ("auth", "the LLM credentials were rejected",
                "run `overseer setup` to re-enter the key, or `overseer provider` to switch backend")
    if isinstance(exc, providers.RateLimited) or "429" in s or "rate" in s or "quota" in s or "overload" in s or "503" in s:
        return ("rate-limit", "the LLM backend is rate-limited / out of quota",
                "wait a moment, add billing to the key, or switch provider with `overseer provider`")
    if "network" in s or "timed out" in s or "resolve" in s or "connection" in s or "getaddrinfo" in s:
        return ("network", "couldn't reach the LLM or Telegram endpoint",
                "check the server's network/DNS - the agent keeps retrying automatically")
    return ("unknown", f"unexpected error: {str(exc)[:160]}", "check logs: journalctl -u overseer -n 50")


def alert(cfg, exc):
    """The 'doctor figure' message sent to the owner when something breaks."""
    cat, msg, fix = diagnose(exc)
    return f"\U0001FA7A Doctor here - something needs you.\nProblem: {msg}.\nFix: {fix}"


def run_checks(cfg):
    """Full checkup. Returns [(name, ok, detail)]."""
    checks = []
    try:
        me = Telegram(cfg["telegram_token"]).get_me()
        ok = bool(me.get("ok"))
        checks.append(("telegram bot", ok, ("@" + me["result"]["username"]) if ok else json.dumps(me)[:80]))
    except Exception as e:
        checks.append(("telegram bot", False, str(e)[:80]))

    try:
        p = providers.build(cfg, "ping")
        ok, detail = p.ping()
        checks.append((f"LLM ({cfg.get('provider')})", ok, detail[:100]))
    except Exception as e:
        checks.append((f"LLM ({cfg.get('provider')})", False, str(e)[:100]))

    try:
        du = shutil.disk_usage("/")
        free = du.free // (1024 ** 3)
        pct = int(du.used / du.total * 100)
        checks.append(("disk", free >= 1, f"{free}G free, {pct}% used"))
    except Exception:
        pass

    try:
        with open("/proc/meminfo") as f:
            mi = {ln.split(":")[0]: ln.split()[1] for ln in f if ":" in ln}
        avail = int(mi.get("MemAvailable", "0")) // 1024
        checks.append(("memory", avail > 80, f"{avail}MB available"))
    except Exception:
        pass
    return checks


def format_report(checks):
    lines = ["\U0001FA7A Overseer checkup:"]
    for name, ok, detail in checks:
        lines.append(f"{'OK ' if ok else 'XX '} {name}: {detail}")
    healthy = all(ok for _, ok, _ in checks)
    lines.append("All green." if healthy else "Some checks need attention ^")
    return "\n".join(lines)
