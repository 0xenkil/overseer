# Architecture

Overseer is a small, single-process Python agent (stdlib only). A Telegram long-poll
feeds a worker that runs an LLM tool-calling loop against your server, with a doctor and
a watchdog watching its back.

## System overview

```mermaid
flowchart LR
    U(["📱 You"]) <-->|long-poll| TG["Telegram Bot API"]
    TG <--> A["Agent loop<br/>(agent.py)"]

    A -->|chat| P{"Provider<br/>(providers.py)"}
    P --> G["Gemini"]
    P --> Q["Groq"]
    P --> C["Claude"]

    A -->|tool calls| T["Tools<br/>run_shell · web_fetch<br/>read_file · write_file"]
    T --> SRV[("🖥️ Your server<br/>(root)")]

    D["🩺 Doctor"] -. health checks .-> A
    D -. 'here's the fix' .-> TG
    W["👁️ Watchdog"] -. anomalies .-> TG

    classDef brain fill:#0e2236,stroke:#22d3ee,color:#cdebf5;
    classDef core fill:#0c1c30,stroke:#34d399,color:#e8f6ee;
    class G,Q,C brain;
    class A,P core;
```

- **agent.py** — the runtime: polls Telegram, queues messages to one serial worker, runs the tool loop, persists per-chat memory.
- **providers.py** — Gemini / Groq / Claude behind one interface, each speaking its own native tool-call format.
- **tools.py** — the four actions the agent can take on the box.
- **doctor.py / watchdog.py** — reactive self-healing + proactive alerts.

## How one message is handled

```mermaid
sequenceDiagram
    autonumber
    participant U as You
    participant A as Agent
    participant L as LLM (provider)
    participant S as Server

    U->>A: "anything failing?"
    A->>L: system + history + tools
    L-->>A: tool_call run_shell("systemctl --failed")
    A->>S: execute (as root, timeout-bounded)
    S-->>A: output (capped, truncated)
    A->>L: tool result
    Note over A,L: loop until the model returns text<br/>(no more tool calls)
    L-->>A: "nginx flapped 3× — fixed it."
    A-->>U: formatted reply
```

The loop repeats — the model can chain many tool calls (inspect → act → verify) before it
replies. Each round re-sends the conversation, so the model always reasons on fresh state.

## Resilience — why tasks don't just die

Free-tier LLM endpoints throw a lot of transient errors. The provider layer turns each into
a recovery, not a failure:

```mermaid
flowchart TD
    R["chat request"] --> X{"response?"}
    X -->|200 OK| OK["use it ✅"]
    X -->|429 / 503 rate-limited| B["wait + try next model in chain<br/>(4 rounds of backoff)"] --> R
    X -->|413 request too large| K["compact: stub oldest tool outputs"] --> R
    X -->|401 / 403 bad key| H["🩺 Doctor DMs you the exact fix"]
    X -->|Cloudflare 1010| UA["real User-Agent already set"] --> OK

    classDef ok fill:#0c2a1e,stroke:#34d399,color:#d6ffe9;
    classDef warn fill:#2a230c,stroke:#eab308,color:#fff6d6;
    class OK ok;
    class B,K,UA,H warn;
```

| Failure | What Overseer does |
|---|---|
| `429` / `503` (rate-limit / overload) | Back off and fall through the model chain (e.g. `gpt-oss-120b → 20b → llama-3.1-8b`). |
| `413` (request too large) | **Auto-compact** — stub the oldest tool outputs, keep recent ones, retry. Long tasks finish instead of erroring. |
| `401` / `403` (bad/expired key) | Doctor classifies it and DMs you the one-line fix. |
| Cloudflare `1010` (UA ban) | Every request carries a real browser User-Agent. |
| reasoning-model quirk | `reasoning` field is stripped from history before each send. |

## The provider abstraction

Every backend implements the same tiny surface, so the agent loop never branches on which
brain is plugged in:

```mermaid
classDiagram
    class Provider {
        +chat(history) Reply
        +user_turn(text)
        +tool_results_turn(results)
        +compact(history)
        +ping()
    }
    class GeminiAPI
    class Groq
    class Claude
    Provider <|-- GeminiAPI
    Provider <|-- Groq
    Provider <|-- Claude
```

Each keeps the conversation in its **own native format** (Gemini `functionCall`, OpenAI/Groq
`tool_calls`, Anthropic `tool_use`) — translation lives inside the provider, not the agent.

## Proactive watchdog

A background thread snapshots the box every few minutes and alerts you on **changes**, not
every tick (so no spam):

```mermaid
flowchart LR
    S["snapshot<br/>every 5 min"] --> DIFF{"changed vs<br/>last snapshot?"}
    DIFF -->|service down| A1["🔴 DM you"]
    DIFF -->|unit failed| A1
    DIFF -->|disk > 90%| A1
    DIFF -->|low memory| A1
    DIFF -->|load spike| A1
    DIFF -->|rebooted| A1
    DIFF -->|nothing| Z["stay quiet"]
```

## Layout

```
overseer/
  agent.py      runtime: telegram poll → worker → tool loop → memory
  providers.py  Gemini | Groq | Claude (fallback + backoff + compact)
  tools.py      run_shell, web_fetch, read_file, write_file
  doctor.py     health checks + failure diagnosis + self-healing alerts
  watchdog.py   proactive anomaly alerts
  persona.py    system prompt / voice
  telegram.py   tiny Bot API client
  config.py     JSON config (+ env overrides)
  cli.py        guided setup wizard + service management
```
