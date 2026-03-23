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
        Item.description.contains('"_type":"signal"')
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

BULLISH CULTURAL TENSIONS (how to reason about themes in the pipeline):
Every brand in this database exists because of two forces: Industry Calcification (incumbents slow/blind/locked in) + Technological Innovation (new tools enabling new distribution/products). Those forces create three cultural tensions:

1. UBIQUITOUS WELLNESS ("wellness") — human desires: Physicality, Tranquility, Order
   The shift FROM 20th-century toxicity (processed food, chemical beauty, reactive sick-care) TO intentional health optimization. Consumers are managing their bodies like a system. Includes: functional food/bev, GLP-1 adjacent, longevity/healthspan, sleep/recovery, mental wellness, women's health, clean nutrition, pet wellness. Ask: does this brand help someone optimize or protect their body and mind?

2. UNCOMPROMISING SELF ("self") — human desires: Acceptance, Idealism, Social Standing, Independence
   The shift FROM mass-market shame and conformity TO authentic self-expression and identity ownership. Consumers demand products built FOR them, not marketed at them. Includes: beauty, skincare, personal care, grooming, fragrance, fashion, body confidence. Ask: does this brand let someone express or embrace who they are — unapologetically?

3. INDIVIDUALS > INSTITUTIONS ("individuals") — human desires: Independence, Power, Social Contact, Vengeance
   The trust collapse in large institutions — big pharma, big food, Wall Street, legacy media — and the rise of founder-led, community-first brands that go around incumbents. Includes: DTC disruption of legacy categories, community-led brands, creator economy, direct relationships bypassing gatekeepers. Default tension for any brand that succeeds by going around an entrenched institution.

When asked about themes or tensions in the pipeline, reason from these desire-based roots — not just category labels.

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
