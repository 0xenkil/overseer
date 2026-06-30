"""LLM backends behind one interface. Each provider speaks its own native tool-call
format (Gemini functionCall, OpenAI/Groq tool_calls, Anthropic tool_use); the agent
loop stays provider-agnostic. Shared model-fallback + backoff. Stdlib only.

A provider keeps the conversation in its OWN native message format. The agent loop:
    hist += p.user_turn(text)
    while True:
        r = p.chat(hist)
        hist.append(r.assistant_turn)
        if not r.calls: return r.text
        hist += p.tool_results_turn(run(r.calls))
"""
import json
import os
import time
import urllib.request
import urllib.error

from .tools import TOOL_SPECS


class AuthError(Exception):
    """Credentials rejected (401/403) - not retryable."""


class RateLimited(Exception):
    """Transient (429/5xx/network) - retry with backoff."""


class ProviderError(Exception):
    """Bad request / unexpected - not retryable."""


class RequestTooLarge(Exception):
    """Request exceeded the model's token limit (413) - shrink history and retry."""


class Reply:
    def __init__(self, text, calls, assistant_turn):
        self.text = text                # final text (when calls is empty)
        self.calls = calls              # [{"id","name","args"}]
        self.assistant_turn = assistant_turn  # native turn to append to history


# Some provider APIs (Groq, Anthropic) sit behind Cloudflare, which bans the default
# "Python-urllib" User-Agent with a 403/1010. Present a normal UA so requests go through.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36 Overseer/0.2")


def _post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": _UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise AuthError(f"{e.code} {detail}")
        if e.code in (408, 409, 429, 500, 502, 503, 504):
            raise RateLimited(f"{e.code} {detail}")
        if e.code == 413 or "too large" in detail.lower():
            raise RequestTooLarge(f"{e.code} {detail}")
        raise ProviderError(f"{e.code} {detail}")
    except urllib.error.URLError as e:
        raise RateLimited(f"network: {e.reason}")
    except Exception as e:
        raise RateLimited(f"network: {e!r}")


def _get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class Provider:
    name = "base"
    default_models = []

    def __init__(self, cfg, system, log=print):
        self.cfg = cfg
        self.system = system
        self.log = log
        # per-provider key (set from Telegram /setkey) falls back to the single api_key
        self.api_key = (cfg.get("keys") or {}).get(self.name) or cfg.get("api_key", "")
        self.models = [cfg["model"]] if cfg.get("model") else list(self.default_models)

    def chat(self, history):
        last = None
        for rnd in range(4):
            too_large_all = True
            for model in self.models:
                try:
                    return self._chat_once(history, model)
                except RequestTooLarge:
                    last = f"{model} too-large"
                    self.log("provider", self.name, last, "- trying next/higher-limit model")
                    continue  # a different model may have a bigger token budget
                except RateLimited as e:
                    last = f"{model} {e}"
                    too_large_all = False
                    self.log("provider", self.name, last)
                    continue
            if too_large_all:
                # every model rejected purely for size; backoff won't help - let the agent compact
                raise RequestTooLarge(f"{self.name}: request too large on all models ({last})")
            wait = min(6 * (rnd + 1), 20)
            self.log(f"all {self.name} models busy (round {rnd + 1}/4), backing off {wait}s")
            time.sleep(wait)
        raise RateLimited(f"{self.name} rate-limited/overloaded - try again in a moment ({last})")

    # --- to be implemented by each provider ---
    def _chat_once(self, history, model):
        raise NotImplementedError

    def user_turn(self, text):
        raise NotImplementedError

    def tool_results_turn(self, results):  # results: [{"id","name","output"(dict)}]
        raise NotImplementedError

    def ping(self):  # -> (ok: bool, detail: str)
        raise NotImplementedError

    def compact(self, history):
        """Shrink a too-large request: stub all but the most recent few tool outputs,
        keeping message structure intact. Returns (new_history, changed_bool)."""
        return history, False


_STUB = "[older output trimmed to fit the model's token limit]"


# ----------------------------------------------------------------------------- Gemini (API key)
class GeminiAPI(Provider):
    name = "gemini-api"
    # gemini-2.5/2.0-flash are rock-solid for tool loops and have higher free quota.
    # gemini-3 preview models require a stricter thought_signature round-trip on
    # parallel tool calls - set `model` explicitly in config if you want them.
    default_models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    base = "https://generativelanguage.googleapis.com/v1beta"

    def _auth(self, url):
        return url + ("?key=" + self.api_key), {}

    def _tools(self):
        return [{"function_declarations": [
            {"name": s["name"], "description": s["description"], "parameters": s["parameters"]} for s in TOOL_SPECS]}]

    def _chat_once(self, history, model):
        url, hdr = self._auth(f"{self.base}/models/{model}:generateContent")
        body = {"system_instruction": {"parts": [{"text": self.system}]},
                "contents": history, "tools": self._tools(),
                "tool_config": {"function_calling_config": {"mode": "AUTO"}},
                "generationConfig": {"temperature": 0.6}}
        r = _post(url, hdr, body)
        cands = r.get("candidates") or []
        if not cands:
            raise ProviderError("no candidates: " + json.dumps(r.get("promptFeedback", {}))[:160])
        content = cands[0].get("content", {}) or {"role": "model", "parts": []}
        parts = content.get("parts", []) or []
        calls = []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append({"id": fc.get("id"), "name": fc.get("name"), "args": fc.get("args", {})})
        text = "".join(p.get("text", "") for p in parts).strip()
        return Reply(text, calls, content)

    def user_turn(self, text):
        return [{"role": "user", "parts": [{"text": text}]}]

    def tool_results_turn(self, results):
        parts = []
        for r in results:
            fr = {"name": r["name"], "response": r["output"] if isinstance(r["output"], dict) else {"result": r["output"]}}
            if r.get("id"):
                fr["id"] = r["id"]
            parts.append({"functionResponse": fr})
        return [{"role": "user", "parts": parts}]

    def ping(self):
        try:
            url, hdr = self._auth(f"{self.base}/models/{self.models[0]}:generateContent")
            _post(url, hdr, {"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                             "generationConfig": {"maxOutputTokens": 1}}, timeout=20)
            return True, "ok"
        except AuthError as e:
            return False, f"auth rejected: {e}"
        except Exception as e:
            return False, str(e)

    def compact(self, history):
        idxs = [i for i, t in enumerate(history)
                if any("functionResponse" in p for p in t.get("parts", []))]
        keep = set(idxs[-2:])
        changed = False
        for i in idxs:
            if i in keep:
                continue
            for p in history[i].get("parts", []):
                fr = p.get("functionResponse")
                if fr and fr.get("response") != {"output": _STUB}:
                    fr["response"] = {"output": _STUB}
                    changed = True
        return history, changed


# ----------------------------------------------------------------------------- Gemini (OAuth, experimental)
class GeminiOAuth(GeminiAPI):
    """Uses the google-gemini-cli OAuth token (~/.gemini/oauth_creds.json) as a Bearer
    token. Much higher free quota than an AI-Studio key. EXPERIMENTAL: if the token's
    scope doesn't cover the Generative Language API it will 401 - fall back to gemini-api."""
    name = "gemini-oauth"
    CREDS = os.path.expanduser("~/.gemini/oauth_creds.json")

    def _token(self):
        with open(self.CREDS) as f:
            c = json.load(f)
        return c.get("access_token") or c.get("token")

    def _auth(self, url):
        return url, {"Authorization": "Bearer " + (self._token() or "")}


# ----------------------------------------------------------------------------- Groq (OpenAI-compatible)
class Groq(Provider):
    name = "groq"
    # gpt-oss models emit clean OpenAI-style tool calls; llama-3.3-70b often malforms them
    # (Groq returns tool_use_failed), so it is deliberately NOT the default.
    default_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
    url = "https://api.groq.com/openai/v1/chat/completions"

    def _tools(self):
        return [{"type": "function", "function": {
            "name": s["name"], "description": s["description"], "parameters": s["parameters"]}} for s in TOOL_SPECS]

    def _chat_once(self, history, model):
        # strip 'reasoning' from EVERY history message (not just new ones) so old saved
        # chats from before the fix don't re-trigger Groq's "reasoning not supported" 400
        msgs = [{"role": "system", "content": self.system}]
        msgs += [{k: v for k, v in m.items() if k != "reasoning"} for m in history]
        body = {"model": model, "messages": msgs,
                "tools": self._tools(), "tool_choice": "auto", "temperature": 0.6}
        r = _post(self.url, {"Authorization": "Bearer " + self.api_key}, body)
        msg = r["choices"][0]["message"]
        calls = []
        for tc in (msg.get("tool_calls") or []):
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            calls.append({"id": tc["id"], "name": tc["function"]["name"], "args": args})
        # Keep ONLY fields valid as an input message. Reasoning models (gpt-oss) add a
        # 'reasoning' field that the API rejects when echoed back on the next round
        # ("reasoning is not supported with this model").
        clean = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            clean["tool_calls"] = msg["tool_calls"]
        return Reply((msg.get("content") or "").strip(), calls, clean)

    def user_turn(self, text):
        return [{"role": "user", "content": text}]

    def tool_results_turn(self, results):
        return [{"role": "tool", "tool_call_id": r["id"], "name": r["name"],
                 "content": json.dumps(r["output"])} for r in results]

    def ping(self):
        try:
            _get("https://api.groq.com/openai/v1/models", {"Authorization": "Bearer " + self.api_key})
            return True, "ok"
        except urllib.error.HTTPError as e:
            return False, f"{e.code} (key rejected?)" if e.code in (401, 403) else str(e.code)
        except Exception as e:
            return False, str(e)

    def compact(self, history):
        idxs = [i for i, m in enumerate(history) if m.get("role") == "tool"]
        keep = set(idxs[-2:])
        changed = False
        for i in idxs:
            if i not in keep and history[i].get("content") != _STUB:
                history[i]["content"] = _STUB
                changed = True
        return history, changed


# ----------------------------------------------------------------------------- Claude (Anthropic)
class Claude(Provider):
    name = "claude"
    default_models = ["claude-haiku-4-5", "claude-sonnet-4-6"]
    url = "https://api.anthropic.com/v1/messages"

    def _hdr(self):
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _tools(self):
        return [{"name": s["name"], "description": s["description"], "input_schema": s["parameters"]} for s in TOOL_SPECS]

    def _chat_once(self, history, model):
        body = {"model": model, "max_tokens": 4096, "system": self.system,
                "messages": history, "tools": self._tools(), "temperature": 0.6}
        r = _post(self.url, self._hdr(), body)
        blocks = r.get("content", []) or []
        calls, text = [], []
        for b in blocks:
            if b.get("type") == "tool_use":
                calls.append({"id": b["id"], "name": b["name"], "args": b.get("input", {})})
            elif b.get("type") == "text":
                text.append(b.get("text", ""))
        return Reply("".join(text).strip(), calls, {"role": "assistant", "content": blocks})

    def user_turn(self, text):
        return [{"role": "user", "content": text}]

    def tool_results_turn(self, results):
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": json.dumps(r["output"])} for r in results]}]

    def ping(self):
        try:
            _post(self.url, self._hdr(), {"model": self.models[0], "max_tokens": 1,
                                          "messages": [{"role": "user", "content": "ping"}]}, timeout=20)
            return True, "ok"
        except AuthError as e:
            return False, f"auth rejected: {e}"
        except Exception as e:
            return False, str(e)

    def compact(self, history):
        idxs = [i for i, m in enumerate(history)
                if isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"])]
        keep = set(idxs[-2:])
        changed = False
        for i in idxs:
            if i in keep:
                continue
            for b in history[i]["content"]:
                if b.get("type") == "tool_result" and b.get("content") != _STUB:
                    b["content"] = _STUB
                    changed = True
        return history, changed


# GeminiOAuth is kept in the codebase but NOT advertised: it needs Google's Code Assist
# API (a different endpoint/format) to actually work. Deferred rather than shipped as a
# dead button. The three below are fully functional.
PROVIDERS = {p.name: p for p in (GeminiAPI, Groq, Claude)}


def build(cfg, system, log=print):
    name = cfg.get("provider", "gemini-api")
    cls = PROVIDERS.get(name)
    if not cls:
        raise ProviderError(f"unknown provider '{name}'. options: {', '.join(PROVIDERS)}")
    return cls(cfg, system, log)
