"""
Slack Webhook notifications for Bullish Stealth Finder.
"""
import os
import json
import urllib.request
import urllib.error
import logging

logger = logging.getLogger(__name__)


def _get_webhook_url() -> str:
    """Get Slack webhook URL from DB settings first, then env var."""
    try:
        from ..models.item import Item
        item = Item.query.filter_by(title="__bullish_settings__").first()
        if item:
            data = json.loads(item.description or "{}")
            url = data.get("slack_webhook_url", "")
            if url:
                return url
    except Exception:
        pass
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def send_slack_hot_alert(hot_brands: list, scan_name: str) -> bool:
    """
    Post a HOT signal alert to Slack via incoming webhook.
    Returns True if sent successfully, False otherwise.
    """
    webhook_url = _get_webhook_url()
    if not webhook_url:
        return False

    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")
    count = len(hot_brands)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔵 {count} HOT Signal{'s' if count != 1 else ''} — {scan_name}",
                "emoji": True,
            },
        }
    ]

    for b in hot_brands:
        score = b.get("score", "—")
        name = b.get("name", "")
        category = b.get("category", "")
        thesis = b.get("thesis", "")
        theme = b.get("theme", "")

        text = f"*{name}* · {category} · Score: *{score}*"
        if thesis:
            text += f"\n_{thesis}_"
        if theme:
            text += f"\n🎯 {theme}"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        if b.get("item_id"):
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"View {name} →", "emoji": False},
                    "url": f"{app_url}/signal/{b['item_id']}",
                    "action_id": f"view_{b['item_id']}",
                }],
            })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "View in Stealth Finder →", "emoji": True},
            "url": app_url,
            "style": "primary",
        }],
    })

    payload = json.dumps({"blocks": blocks}).encode("utf-8")

    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Slack alert failed: %s", exc)
        return False


def send_slack_test(webhook_url: str) -> bool:
    """Send a test message to verify the webhook URL works."""
    payload = json.dumps({
        "text": "✅ Bullish Stealth Finder — Slack integration is working!"
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Slack test failed: %s", exc)
        return False
