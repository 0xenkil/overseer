"""Tiny Telegram Bot API client (long-poll + send). Stdlib only."""
import json
import urllib.request


def _plain(t):
    # clean fallback when Telegram rejects our Markdown: drop the emphasis/code markers
    return str(t).replace("`", "").replace("*", "")


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

    def set_my_commands(self, commands):
        """Register the slash-command menu so Telegram's '/' button lists them."""
        try:
            self.call("setMyCommands", {"commands": commands}, timeout=15)
        except Exception:
            pass

    def send(self, chat_id, text):
        # Try Markdown for nice formatting; if the model emitted broken markdown,
        # fall back to plain text so the message always gets delivered.
        for chunk in _split(text):
            try:
                self.call("sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}, timeout=20)
            except Exception:
                try:
                    self.call("sendMessage", {"chat_id": chat_id, "text": _plain(chunk)}, timeout=20)
                except Exception:
                    pass

    def react(self, chat_id, message_id, emoji="👀"):
        try:
            self.call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
                                             "reaction": [{"type": "emoji", "emoji": emoji}]}, timeout=10)
        except Exception:
            pass

    @staticmethod
    def _kb(rows):
        # rows: list of rows; each row a list of (label, callback_data) tuples
        return {"inline_keyboard": [[{"text": l, "callback_data": d} for (l, d) in row] for row in rows]}

    def send_buttons(self, chat_id, text, rows):
        base = {"chat_id": chat_id, "text": text, "reply_markup": self._kb(rows)}
        try:
            self.call("sendMessage", {**base, "parse_mode": "Markdown"}, timeout=20)
        except Exception:
            try:
                self.call("sendMessage", base, timeout=20)
            except Exception:
                pass

    def answer_callback(self, cb_id, text=""):
        try:
            self.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text}, timeout=15)
        except Exception:
            pass

    def edit_message(self, chat_id, message_id, text, rows=None):
        base = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if rows is not None:
            base["reply_markup"] = self._kb(rows)
        try:
            self.call("editMessageText", {**base, "parse_mode": "Markdown"}, timeout=15)
        except Exception:
            try:
                self.call("editMessageText", base, timeout=15)
            except Exception:
                pass
