# Overseer

**A lean, self-healing AI agent that runs your server from Telegram.**

Message your bot, it does the work — sysadmin, automation, coding, research, OSINT/recon — by actually running commands on the box and verifying the result. One Python file's worth of dependencies (zero — it's **stdlib only**), so it drops onto the tiniest VPS and starts in milliseconds.

```
you:      nginx down?
overseer: Yeah — OOM-killed 3h ago. Restarted it, it's up, bumped its mem cap so it won't recur.

you:      OSINT on the username "naval"
overseer: 3 hits — @naval (X, AngelList founder), naval.eth, nav.al (personal blog, Cloudflare).
          Pulled the blog's RSS + linked socials. Want the email footprint too?
```

---

## Why

Most "AI on your server" setups are heavy frameworks that eat 500MB+, pin themselves to one model, and fall over when a token expires. Overseer is the opposite:

- **Tiny & fast** — pure Python stdlib, ~12MB resident, runs on a 512MB box without noticing.
- **Bring any brain** — Gemini (API key *or* OAuth), Groq, or Claude. Switch with one command.
- **Self-healing** — a built-in *doctor* diagnoses failures (bad key, rate limit, network) and tells you the exact fix over Telegram. systemd restarts it; the doctor explains *why* it fell over.
- **Actually does things** — real shell access, web fetch, file read/write, in a tool-calling loop until the job's done. It acts, then reports — it doesn't lecture or ask permission for safe stuff.
- **Locked to you** — only your Telegram chat id(s) can command it.

## Install

On your VPS (Linux, Python 3.8+):

```bash
curl -fsSL https://raw.githubusercontent.com/TheENkil/overseer/main/install.sh | sh
overseer setup
```

`setup` walks you through: pick a backend, paste the key, paste your Telegram bot token (from [@BotFather](https://t.me/BotFather)), and it **auto-detects your chat id** (just message the bot when it asks). Then it installs the 24/7 systemd service. Done.

No git? Clone/copy the folder anywhere and run `python3 -m overseer setup`.

## Backends

| Provider | Auth | Notes |
|---|---|---|
| `gemini-api` | Google AI Studio key | Free tier; great default. |
| `gemini-oauth` | `google-gemini-cli` login | Higher quota, incl. Pro. *Experimental.* |
| `groq` | Groq API key | Blazing fast (Llama 3.3 70B etc.). |
| `claude` | Anthropic API key | Strongest reasoning/tool use. |

Each model has a built-in fallback chain + backoff, so a transient `429`/`503` won't kill a task. Switch anytime:

```bash
overseer provider
```

## Commands

```
overseer setup       first-time wizard (creds, telegram, chat-id, install)
overseer run         run in the foreground
overseer install     install + start the systemd service
overseer doctor      full health checkup (telegram, LLM creds, disk, memory)
overseer status      service status
overseer provider    switch AI backend
overseer logs        tail the live logs
overseer start|stop|restart
```

In Telegram: `/new` (reset memory) · `/status` (health) · `/whoami` (model + your chat id).

## Tools the agent has

- **run_shell** — bash as root
- **web_fetch** — pull any URL/API (HTML stripped)
- **write_file** / **read_file**

It chains these in a loop, verifying as it goes, before replying.

## Safety

- The agent runs commands **as root** and obeys whoever can message the bot — so it's **locked to your `allowed_chat_ids`**, and nobody else's messages are processed.
- List your **`protected_services`** (e.g. `xray`, `tor`) in setup; the agent won't touch them, and won't do destructive/irreversible actions, without explicit confirmation.
- Secrets (`config.json`, state) are `chmod 600` and `.gitignore`d. Run it on a box you own and control.

## Architecture

```
overseer/
  agent.py      telegram long-poll -> provider loop -> tools  (the runtime)
  providers.py  Gemini-API | Gemini-OAuth | Groq | Claude behind one interface
  tools.py      run_shell, web_fetch, write_file, read_file
  doctor.py     health checks + failure diagnosis + self-healing alerts
  persona.py    the voice
  telegram.py   tiny Bot API client
  config.py     JSON config (+ env overrides)
  cli.py        setup wizard + service management
```

Each provider keeps the conversation in its own native format and exposes a uniform
`chat / user_turn / tool_results_turn` surface, so the agent loop never has to care
which brain is plugged in.

## License

MIT © 2026 TheENkil
