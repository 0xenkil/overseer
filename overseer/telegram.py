"""Tiny Telegram Bot API client (long-poll + send). Stdlib only."""
import json
import urllib.request


def _split(text, n=4000):
    out, cur = [], ""
    for line in str(text).split("\n"):
        if len(cur) + len(line) + 1 > n:
            if cur:
                out.append(cur)
            cur = line[:n] if len(line) > n else line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or ["(empty)"]


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method, params=None, timeout=70):
        req = urllib.request.Request(f"{self.base}/{method}",
                                     data=json.dumps(params or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def get_me(self):
        return self.call("getMe", timeout=15)

    def get_updates(self, offset=0, timeout=50):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout}, timeout=timeout + 15)

    def delete_webhook(self, drop=True):
        try:
            return self.call("deleteWebhook", {"drop_pending_updates": drop}, timeout=15)
        except Exception:
            return None

    def send_chat_action(self, chat_id, action="typing"):
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)
        except Exception:
            pass

    def send(self, chat_id, text):
        for chunk in _split(text):
            try:
                self.call("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=20)
            except Exception:
                pass
