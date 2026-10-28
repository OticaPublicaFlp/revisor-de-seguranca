import httpx

from .config import Config

POLL_TIMEOUT = 50


class Telegram:
    def __init__(self, cfg: Config):
        self.chat_id = cfg.telegram_chat_id
        self.base = f"https://api.telegram.org/bot{cfg.telegram_token}"
        self.client = httpx.Client(timeout=POLL_TIMEOUT + 10)

    def _call(self, method: str, **payload) -> dict:
        r = self.client.post(f"{self.base}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def send_summary(self, text: str, run_id: int, slack_enabled: bool = True) -> int:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if slack_enabled:  # sem Slack configurado, não há o que aprovar: mensagem sem botões
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "✅ Enviar ao Slack", "callback_data": f"slack:{run_id}"},
                {"text": "❌ Descartar", "callback_data": f"drop:{run_id}"},
            ]]}
        msg = self._call("sendMessage", **payload)
        return msg["message_id"]

    def send_text(self, text: str) -> None:
        self._call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML")

    def send_document(self, filename: str, content: str) -> None:
        r = self.client.post(
            f"{self.base}/sendDocument",
            data={"chat_id": self.chat_id},
            files={"document": (filename, content.encode("utf-8"), "text/markdown")},
        )
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram sendDocument: {r.json().get('description')}")

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def clear_keyboard(self, message_id: int, footer: str) -> None:
        self._call(
            "editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": [[{"text": footer, "callback_data": "noop"}]]},
        )

    def get_updates(self, offset: int) -> list[dict]:
        return self._call(
            "getUpdates", offset=offset, timeout=POLL_TIMEOUT,
            allowed_updates=["callback_query"],
        )


def discover(token: str) -> None:
    """Mostra bot, grupos e pessoas que o bot já viu. Não confirma (consome) nenhuma atualização."""
    base = f"https://api.telegram.org/bot{token}"

    def call(method: str, **payload):
        data = httpx.post(f"{base}/{method}", json=payload, timeout=30).json()
        if not data.get("ok"):
            raise SystemExit(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    me = call("getMe")
    print(f"Bot: @{me['username']} (privacidade em grupos: "
          f"{'só comandos e menções' if not me.get('can_read_all_group_messages') else 'lê tudo'})")
    if call("getWebhookInfo").get("url"):
        print("ATENÇÃO: há um webhook configurado; o polling não vai funcionar até removê-lo.")

    chats: dict[int, str] = {}
    people: dict[int, str] = {}
    updates = call("getUpdates", timeout=0, allowed_updates=["message", "my_chat_member", "callback_query"])
    for upd in updates:
        obj = upd.get("message") or upd.get("my_chat_member") or (upd.get("callback_query") or {}).get("message") or {}
        chat = obj.get("chat")
        if chat:
            chats[chat["id"]] = f"{chat.get('type')} — {chat.get('title') or chat.get('first_name', '')}"
        sender = obj.get("from") or (upd.get("callback_query") or {}).get("from")
        if sender and not sender.get("is_bot"):
            handle = f" @{sender['username']}" if sender.get("username") else ""
            people[sender["id"]] = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip() + handle

    if not chats:
        print("\nNenhuma conversa encontrada ainda. Adicione o bot ao grupo e envie uma mensagem "
              f"mencionando @{me['username']} (ou /start@{me['username']}); depois rode este comando de novo.")
        return
    print("\nChats que o bot viu (use o id do grupo em TELEGRAM_CHAT_ID):")
    for cid, desc in chats.items():
        print(f"  {cid}  {desc}")
    print("\nPessoas (use os ids de quem pode aprovar em TELEGRAM_APPROVER_IDS):")
    for pid, desc in people.items():
        print(f"  {pid}  {desc}")
