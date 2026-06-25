"""Proactive watchdog: periodically snapshots the box and messages the owner when
something noteworthy CHANGES (a watched service drops, a unit fails, disk/mem/load
crosses a threshold, the box rebooted). State-diff based, so it alerts on transitions
- not every tick - and tells you when things recover too. Stdlib only."""
import os
import shutil
import subprocess


def _svc_active(name):
    try:
        return subprocess.run(["systemctl", "is-active", name],
                              capture_output=True, text=True, timeout=10).stdout.strip() == "active"
    except Exception:
        return None


def _failed_units():
    try:
        out = subprocess.run(["systemctl", "--failed", "--no-legend", "--plain"],
                             capture_output=True, text=True, timeout=10).stdout
        return set(line.split()[0] for line in out.splitlines() if line.strip())
    except Exception:
        return set()


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except Exception:
        return ""


def snapshot(cfg):
    s = {"services": {}, "failed": set(), "boot": _boot_id(), "cores": os.cpu_count() or 1}
    try:
        du = shutil.disk_usage("/")
        s["disk_pct"] = int(du.used / du.total * 100)
    except Exception:
        s["disk_pct"] = 0
    try:
        with open("/proc/meminfo") as f:
            mi = {x.split(":")[0]: int(x.split()[1]) for x in f if ":" in x}
        s["mem_avail_mb"] = mi.get("MemAvailable", 0) // 1024
    except Exception:
        s["mem_avail_mb"] = 9999
    try:
        with open("/proc/loadavg") as f:
            s["load5"] = float(f.read().split()[1])
    except Exception:
        s["load5"] = 0.0
    for n in cfg.get("protected_services", []):
        s["services"][n] = _svc_active(n)
    s["failed"] = _failed_units()
    return s


def diff(prev, cur, cfg):
    """Return a list of human alert strings for noteworthy changes (empty on first run)."""
    if prev is None:
        return []
    alerts = []
    cores = cur.get("cores", 1)

    if prev.get("boot") and cur.get("boot") and cur["boot"] != prev["boot"]:
        alerts.append("🔄 The server *rebooted*.")

    for n, up in cur.get("services", {}).items():
        was = prev.get("services", {}).get(n)
        if was and up is False:
            alerts.append(f"🔴 Service `{n}` went *DOWN*.")
        elif was is False and up:
            alerts.append(f"🟢 Service `{n}` is *back up*.")

    new_failed = cur.get("failed", set()) - prev.get("failed", set())
    if new_failed:
        alerts.append("⚠️ New *failed* unit(s): " + ", ".join("`%s`" % u for u in sorted(new_failed)))

    disk_lim = cfg.get("watch_disk_pct", 90)
    if cur["disk_pct"] >= disk_lim > prev["disk_pct"]:
        alerts.append(f"💾 Disk is *{cur['disk_pct']}%* full (over {disk_lim}%).")

    mem_lim = cfg.get("watch_mem_mb", 100)
    if cur["mem_avail_mb"] < mem_lim <= prev["mem_avail_mb"]:
        alerts.append(f"🧠 *Low memory*: only {cur['mem_avail_mb']}MB available.")

    load_lim = cfg.get("watch_load_mult", 4) * cores
    if cur["load5"] > load_lim >= prev["load5"]:
        alerts.append(f"📈 *Load spike*: 5-min load {cur['load5']:.1f} on {cores} core(s).")

    return alerts
