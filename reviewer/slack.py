import httpx

from .config import Config


def post_message(cfg: Config, text: str) -> None:
    if not cfg.slack_token or not cfg.slack_channel:
        raise RuntimeError("SLACK_BOT_TOKEN/SLACK_CHANNEL_ID não configurados")
    r = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {cfg.slack_token}"},
        json={"channel": cfg.slack_channel, "text": text, "unfurl_links": False},
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack: {data.get('error')}")
