"""
Ask Bullish — conversational AI over the Stealth Finder signal database.
"""
import os
import json
import anthropic

_client = None

def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _load_signal_manifest() -> str:
    """Build a compact text summary of all enriched signals from the DB."""
    from ..extensions import db
    from ..models.item import Item

    rows = Item.query.filter(
        Item.description.contains('"_type": "signal"')
    ).order_by(Item.created_at.desc()).limit(300).all()

    lines = []
    for item in rows:
        try:
            meta = json.loads(item.description or "{}")
            if meta.get("_type") != "signal":
                continue
            e = meta.get("enrichment") or {}
            if not e.get("enriched"):
                continue

            founder = e.get("founder") or {}
            founder_str = founder.get("name") if founder.get("confidence") != "unknown" and founder.get("name") else "Unknown (stealth)"

            signals = meta.get("signal_type", "unknown")
            notes = meta.get("team_notes", "")

            line = (
                f"- {meta.get('company_name', item.title)} | "
                f"{meta.get('category', '')} | "
                f"{e.get('watch_level', '?').upper()} | "
                f"Score: {e.get('bullish_score', '?')} | "
                f"Theme: {e.get('cultural_theme') or 'None'} | "
                f"Founder: {founder_str} | "
                f"Signal: {signals} | "
                f"Filed: {(meta.get('timestamp') or '')[:10]} | "
                f"Thesis: {e.get('one_line_thesis', '')}"
            )
            if notes:
                line += f" | Team Note: {notes}"
            lines.append(line)
        except Exception:
            pass

    if not lines:
        return "No enriched signals in the database yet."
    # Cap at 100 signals to stay well within API token limits
    manifest = "\n".join(lines[:100])
    import logging
    logging.getLogger(__name__).info("manifest size: %d chars, %d signals", len(manifest), len(lines[:100]))
    return manifest


SYSTEM_PROMPT = """You are the Bullish AI Analyst — an intelligent research assistant embedded in Bullish's Stealth Finder platform.

Bullish is a $75M seed-stage consumer brand VC fund. You have real-time access to Bullish's signal database: trademark filings, Delaware incorporations, and domain registrations that have been scored against Bullish's investment thesis.

Your job: help the Bullish team analyze signals, find patterns, surface the most interesting brands, and answer questions about what's in the pipeline.

SIGNAL DATABASE (current snapshot):
{manifest}

HOW TO RESPOND:
- Be concise and direct — this is a fast-paced VC environment
- When listing brands, always include: name, score, watch level, category, thesis
- Use the Bullish scoring framework: HOT (≥70), WARM (50-69), COLD (<50)
- Reference specific signals by name and back up observations with data from the manifest
- If asked for a recommendation, give one — don't hedge
- Format lists cleanly. Use line breaks. Keep responses under 400 words unless a detailed analysis is explicitly requested.
- Never make up brands that aren't in the database — only reference what's in the manifest above.

You are sharp, opinionated, and know the Bullish thesis cold."""


def ask_bullish(messages: list) -> str:
    """
    Run a multi-turn conversation with Claude over the signal database.
    messages: list of {"role": "user"|"assistant", "content": "..."}
    Returns the assistant's reply as a string.
    """
    client = _get_client()
    manifest = _load_signal_manifest()
    system = SYSTEM_PROMPT.replace("{manifest}", manifest)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system,
        messages=messages,
    )
    return response.content[0].text.strip()
