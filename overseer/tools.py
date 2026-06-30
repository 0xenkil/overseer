"""Agent tools. Provider-agnostic: TOOL_SPECS are plain JSON-schema; each LLM provider
formats them into its own tool shape. Stdlib only."""
import os
import re
import subprocess
import urllib.request

# cap tool output so a single request stays well under tight free-tier token/min limits
# (e.g. Groq gpt-oss = 8000 TPM); big dumps get truncated rather than 413-ing the request
MAX_OUT = 2000

TOOL_SPECS = [
    {"name": "run_shell",
     "description": "Execute a bash command on the server as root. Returns combined stdout+stderr and the exit code. Your primary way to inspect and manage the box.",
     "parameters": {"type": "object",
                    "properties": {"command": {"type": "string", "description": "The bash command to run."}},
                    "required": ["command"]}},
    {"name": "web_fetch",
     "description": "HTTP(S) GET a URL and return its readable text (HTML stripped) or raw JSON. Use for web research, OSINT, and reading pages or APIs.",
     "parameters": {"type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"]}},
    {"name": "write_file",
     "description": "Create or overwrite a file with exact text content (atomic write). Use for scripts/configs to avoid shell-quoting issues.",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "read_file",
     "description": "Read a UTF-8 text file and return its content.",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]}},
]

_ENV = dict(os.environ)
_ENV.setdefault("HOME", "/root")
_ENV["PATH"] = "/root/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + _ENV.get("PATH", "")
_ENV.setdefault("XDG_RUNTIME_DIR", "/run/user/0")


def _trunc(s):
    return s if len(s) <= MAX_OUT else s[:MAX_OUT] + f"\n...[truncated {len(s) - MAX_OUT} chars]"


def run_shell(args, cmd_timeout=180, log=print):
    cmd = args.get("command", "")
    log("run_shell:", cmd[:200].replace("\n", " "))
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=cmd_timeout, cwd="/root", env=_ENV)
        out = (p.stdout or "") + (p.stderr or "")
        return {"exit_code": p.returncode, "output": _trunc(out.strip()) or "(no output)"}
    except subprocess.TimeoutExpired:
        return {"error": f"command timed out after {cmd_timeout}s"}
    except Exception as e:
        return {"error": str(e)}


def web_fetch(args, log=print, **_):
    url = args.get("url", "")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    log("web_fetch:", url[:200])
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(2_000_000)
            ctype = r.headers.get("Content-Type", "")
        text = raw.decode("utf-8", "replace")
        if "json" not in ctype:
            text = re.sub(r"(?is)<(script|style|noscript|head)\b.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')]:
                text = text.replace(a, b)
            text = re.sub(r"[ \t\r\f]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text)
        return {"url": url, "content": _trunc(text.strip()) or "(empty)"}
    except Exception as e:
        return {"error": str(e)}


def write_file(args, log=print, **_):
    path, content = args.get("path", ""), args.get("content", "")
    log("write_file:", path)
    try:
        tmp = path + ".overseer.tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, path)
        return {"ok": True, "bytes": len(content.encode())}
    except Exception as e:
        return {"error": str(e)}


def read_file(args, log=print, **_):
    try:
        with open(args.get("path", ""), "r", errors="replace") as f:
            return {"content": _trunc(f.read(MAX_OUT * 2))}
    except Exception as e:
        return {"error": str(e)}


def dispatch(cmd_timeout=180, log=print):
    """Return {name: callable(args)->dict} bound to runtime settings."""
    return {
        "run_shell": lambda a: run_shell(a, cmd_timeout=cmd_timeout, log=log),
        "web_fetch": lambda a: web_fetch(a, log=log),
        "write_file": lambda a: write_file(a, log=log),
        "read_file": lambda a: read_file(a, log=log),
    }
