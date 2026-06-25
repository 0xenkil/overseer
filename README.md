# Overseer

**The lean, self-healing AI agent that runs your server from Telegram.**

Message your bot, it does the work — sysadmin, automation, monitoring, scripting, research — by actually running commands on the box and verifying the result. **Zero dependencies** (pure Python stdlib), so it drops onto the tiniest VPS and starts in milliseconds.

```
you:      anything failing?
overseer: nginx flapped 3× in the last hour — bad server block in a reload loop.
          Fixed it, reloaded clean. Quiet now. Also: disk's at 86%, want me to clear old logs?

you:      yeah do it
overseer: Cleared 4.2G of rotated logs + apt cache. Disk now 71%. 👍
```

---

## Why another one?

"AI on your server over Telegram" is a crowded space now — OpenClaw made it popular, and there's a wave of lean clones (Nanobot, ZeroClaw, Hermes…). Overseer isn't trying to be first. It's trying to be the **smallest, most robust, and safest** one in that lane:

- **Smallest** — **truly zero dependencies**, pure stdlib, ~12 MB resident. No `pip install`, no Node, no Docker. The lean clones still pull in libraries; Overseer pulls in *nothing*, so it runs on a 512 MB box without noticing. (OpenClaw eats ~700 MB+.)
- **Most robust** — it survives the real-world walls the new clones haven't hit yet: provider rate-limits (model fallback + backoff), Cloudflare bot-blocks, reasoning-model quirks, and oversized requests (it **auto-trims its own context and retries** instead of failing).
- **Safest** — locked to *your* Telegram id, a protected-services list it won't touch, confirmation before destructive actions, and secrets kept `chmod 600`. (Palo Alto Networks literally called OpenClaw a "security nightmare" — this is the boring, careful alternative.)
- **Self-healing** — a built-in *doctor* diagnoses failures (bad key, rate-limit, network) and DMs you the exact fix, plus a **watchdog** that proactively pings you when a service dies, disk fills, load spikes, or the box reboots.

## Install

On your VPS (Linux, Python 3.8+):

```bash
curl -fsSL https://raw.githubusercontent.com/TheENkil/overseer/main/install.sh | sh
overseer setup
```

`setup` is a guided 3-step wizard: pick a backend (it shows you exactly where to get the key), create a Telegram bot via [@BotFather](https://t.me/BotFather), and it **auto-detects your chat id** (just message the bot when it asks). Then it installs the 24/7 service. Done.

No git? Clone/copy the folder anywhere and run `python3 -m overseer setup`.

## Backends

Pick by friendly label (Fast / Smart / …) — you never touch a raw model id. Each has a built-in fallback chain + backoff, so a transient `429`/`503` won't kill a task.

| Backend | Key (free tier) | Notes |
|---|---|---|
| `gemini` | [aistudio.google.com](https://aistudio.google.com/apikey) | Easy, solid all-rounder. |
| `groq` | [console.groq.com](https://console.groq.com/keys) | Free + very fast (default: `gpt-oss-120b`). |
| `claude` | [console.anthropic.com](https://console.anthropic.com/settings/keys) | Strongest reasoning, paid. |

Switch backend or model anytime:

```bash
overseer provider
```

## Commands

```
overseer setup       guided first-time setup (creds, telegram, chat-id, install)
overseer install     install + start the systemd service
overseer doctor      full health checkup (telegram, LLM creds, disk, memory)
overseer status      service status
overseer provider    switch AI backend / model
overseer logs        tail the live logs
overseer start|stop|restart
```

In Telegram (the `/` menu lists them): `/status` (health) · `/model` (current AI) · `/new` (reset memory) · `/whoami` · `/help`.

## What the agent can do

- **run_shell** — bash as root
- **web_fetch** — pull any URL/API (HTML stripped)
- **write_file** / **read_file**

It chains these in a loop, verifying as it goes, before replying — and **auto-compacts** old tool output when a request gets too big for the model's token limit, so long multi-step tasks complete instead of erroring.

## Safety

- Runs commands **as root** and obeys whoever can message the bot — so it's **locked to your `allowed_chat_ids`**; everyone else is ignored.
- List your **`protected_services`** (e.g. `xray`, `tor`); the agent won't stop/reconfigure them — or do destructive/irreversible actions — without explicit confirmation.
- Secrets (`config.json`, state) are `chmod 600` and `.gitignore`d. Run it on a box you own.

## Architecture

```
overseer/
  agent.py      telegram long-poll -> provider loop -> tools  (the runtime)
  providers.py  Gemini | Groq | Claude behind one interface (fallback + backoff + auto-compact)
  tools.py      run_shell, web_fetch, write_file, read_file
  doctor.py     health checks + failure diagnosis + self-healing alerts
  watchdog.py   proactive anomaly alerts (service down, disk, load, reboot)
  persona.py    the voice
  telegram.py   tiny Bot API client
  config.py     JSON config (+ env overrides)
  cli.py        guided setup wizard + service management
```

Each provider keeps the conversation in its own native format and exposes a uniform
`chat / user_turn / tool_results_turn / compact` surface, so the agent loop never has to
care which brain is plugged in.

## License

MIT © 2026 TheENkil
