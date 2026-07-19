import os
import re
import resend


def _strip_year(theme):
    """Strip leading year prefix like '2026 ' or '2026 Theme: ' from a theme string."""
    if not theme:
        return theme
    return re.sub(r'^\d{4}\s*(Theme:?\s*)?', '', theme, flags=re.IGNORECASE).strip()


def _resend_client():
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY environment variable is not set")
    resend.api_key = api_key
    return os.environ.get("MAIL_FROM", "noreply@mail.bullish.co")


_LOGO_LOCKUP = """<!-- Logo lockup — table-based for email client compatibility -->
<table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:10px;">
  <tr>
    <td style="width:36px;height:36px;vertical-align:middle;">
      <img src="https://brentvartan.github.io/stealth-finder-frontend/bullish-mark.svg"
           width="36" height="36" alt="Bullish"
           style="display:block;border:0;outline:none;text-decoration:none;" />
    </td>
    <td style="padding-left:10px;vertical-align:middle;line-height:1.2;">
      <span style="display:block;font-family:Arial,sans-serif;font-size:10px;font-weight:800;letter-spacing:0.15em;color:#fff;text-transform:uppercase;">Stealth</span>
      <span style="display:block;font-family:Arial,sans-serif;font-size:10px;font-weight:800;letter-spacing:0.15em;color:#fff;text-transform:uppercase;">Startup</span>
      <span style="display:block;font-family:Arial,sans-serif;font-size:10px;font-weight:800;letter-spacing:0.15em;color:#fff;text-transform:uppercase;">Finder</span>
    </td>
  </tr>
</table>"""


def send_hot_alert(to_email: str, hot_brands: list, scan_name: str) -> None:
    """Send a HOT signal alert email via Resend when new HOT brands are discovered."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    count = len(hot_brands)
    subject = f"🔵 {count} HOT Signal{'s' if count != 1 else ''} — Stealth Startup Finder"

    brand_cards = ""
    for b in hot_brands:
        brand_cards += f"""
        <div style="border:2px solid #052EF0;border-radius:8px;padding:20px;margin:16px 0;background:#fff;">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
            <div style="background:#052EF0;color:#fff;border-radius:6px;padding:8px 12px;font-family:monospace;font-weight:bold;font-size:20px;min-width:52px;text-align:center;">
              {b.get('score', '—')}
            </div>
            <div>
              <div style="font-family:monospace;font-weight:bold;font-size:18px;letter-spacing:2px;text-transform:uppercase;color:#000;">
                {b.get('name', '')}
              </div>
              <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">
                {b.get('category', '')}
              </div>
            </div>
          </div>
          {f'<p style="font-style:italic;color:#333;margin:8px 0;border-left:3px solid #052EF0;padding-left:12px;">{b["thesis"]}</p>' if b.get('thesis') else ''}
          {f'<p style="font-size:12px;color:#052EF0;font-weight:600;margin:8px 0;">Theme: {_strip_year(b["theme"])}</p>' if b.get('theme') else ''}
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">

        <!-- Header -->
        <div style="padding:32px 40px 24px;border-bottom:1px solid #222;">
          {_LOGO_LOCKUP}
          <h1 style="margin:0;color:#052EF0;font-family:monospace;font-size:28px;font-weight:bold;letter-spacing:3px;">
            🔵 HOT SIGNAL{('S' if count != 1 else '')}
          </h1>
          <p style="margin:8px 0 0;color:#888;font-size:14px;">
            {count} new HOT brand{'s' if count != 1 else ''} detected — {scan_name}
          </p>
        </div>

        <!-- Brand cards -->
        <div style="padding:24px 40px;">
          {brand_cards}
        </div>

        <!-- CTA -->
        <div style="padding:0 40px 32px;">
          <a href="{app_url}"
             style="display:inline-block;background:#052EF0;color:#fff;text-decoration:none;
                    padding:14px 28px;border-radius:6px;font-family:monospace;font-weight:bold;
                    font-size:13px;letter-spacing:1px;text-transform:uppercase;">
            View in Stealth Startup Finder →
          </a>
        </div>

        <!-- Footer -->
        <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:11px;">
            Bullish Brand Fund III · Stealth Startup Finder · Automated Signal Detection
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    plain_text = f"Bullish Stealth Startup Finder — {count} HOT Signal{'s' if count != 1 else ''}\n\n"
    for b in hot_brands:
        plain_text += f"  {b.get('score', '—')}  {b.get('name', '').upper()}\n"
        if b.get('thesis'):
            plain_text += f"  {b['thesis']}\n"
        if b.get('theme'):
            plain_text += f"  Theme: {_strip_year(b['theme'])}\n"
        plain_text += "\n"
    plain_text += f"View in Stealth Startup Finder: {app_url}\n"

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
        "text":    plain_text,
    })


def send_invite_email(to_email: str, invite_url: str, invited_by: str) -> None:
    """Send a team invite email via Resend."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    reply_to = os.environ.get("MAIL_REPLY_TO", "brent@bullish.co")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your access to Bullish Stealth Startup Finder</title>
</head>
<body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">

    <!-- Wordmark header — no SVG (improves spam score) -->
    <div style="padding:36px 40px 28px;border-bottom:1px solid #222;text-align:center;">
      <div style="font-family:Georgia,serif;font-style:italic;color:#fff;font-size:22px;
                  letter-spacing:2px;line-height:1.3;margin-bottom:6px;">
        Bullish Stealth Startup Finder
      </div>
      <div style="font-family:monospace;font-size:10px;color:#555;letter-spacing:3px;
                  text-transform:uppercase;">
        Bullish Brand Fund III
      </div>
    </div>

    <!-- Heading -->
    <div style="padding:32px 40px 0;">
      <h1 style="margin:0 0 8px;color:#fff;font-family:monospace;font-size:24px;
                 font-weight:bold;letter-spacing:3px;text-transform:uppercase;">
        ACCESS GRANTED
      </h1>
      <p style="margin:0;color:#888;font-size:14px;">
        {invited_by} has added you to the Bullish Stealth Startup Finder team.
      </p>
    </div>

    <!-- Body -->
    <div style="padding:24px 40px 32px;">
      <p style="color:#ccc;font-size:14px;line-height:1.7;margin:0 0 24px;">
        Stealth Startup Finder tracks early-stage consumer brand signals — trademark filings,
        EDGAR incorporations, and domain registrations — enriched with Bullish AI
        to surface the next Bubble, Hu, or Nom Nom before anyone else.
      </p>
      <a href="{invite_url}"
         style="display:inline-block;background:#052EF0;color:#fff;text-decoration:none;
                padding:14px 28px;border-radius:6px;font-family:monospace;font-weight:bold;
                font-size:13px;letter-spacing:1px;text-transform:uppercase;">
        Create Your Account
      </a>
      <p style="color:#555;font-size:11px;margin:20px 0 0;">
        This link expires in 7 days. If you were not expecting this, you can safely ignore it.
      </p>
    </div>

    <!-- Footer -->
    <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
      <p style="margin:0;color:#444;font-size:11px;">
        Bullish Brand Fund III &middot; Stealth Startup Finder
      </p>
    </div>
  </div>
</body>
</html>"""

    plain_text = f"""Bullish Stealth Startup Finder — Team Access

{invited_by} has added you to the Bullish Stealth Startup Finder team.

Stealth Startup Finder tracks early-stage consumer brand signals — trademark filings, EDGAR incorporations, and domain registrations — enriched with Bullish AI to surface the next Bubble, Hu, or Nom Nom before anyone else.

Create your account here:
{invite_url}

This link expires in 7 days. If you were not expecting this, you can safely ignore it.

—
Bullish Brand Fund III · Stealth Startup Finder
"""

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "reply_to": reply_to,
        "subject": "Your access to Bullish Stealth Startup Finder",
        "html":    html,
        "text":    plain_text,
        "headers": {
            "List-Unsubscribe": f"<mailto:{from_address}?subject=unsubscribe>",
        },
    })


def send_weekly_digest_email(to_email: str, hot_signals: list, warm_signals: list, week_label: str) -> None:
    """Send a weekly top-signals digest email via Resend."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    def brand_card(b, is_hot):
        border   = "#052EF0" if is_hot else "#000"
        score_bg = "#052EF0" if is_hot else "#000"
        label    = "HOT" if is_hot else "WARM"
        # Use a table for the score + name row — CSS flexbox is not supported
        # in Gmail and most email clients; tables render reliably everywhere.
        return f"""
        <div style="border:2px solid {border};border-radius:8px;padding:16px;margin:10px 0;background:#fff;">
          <table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:8px;">
            <tr>
              <td valign="middle" style="padding-right:12px;">
                <div style="background:{score_bg};color:#fff;border-radius:5px;padding:6px 12px;font-family:monospace;font-weight:bold;font-size:20px;text-align:center;white-space:nowrap;">
                  {b.get('score', '—')}
                </div>
              </td>
              <td valign="middle">
                <div style="font-family:monospace;font-weight:bold;font-size:15px;letter-spacing:2px;text-transform:uppercase;color:#000;">
                  {b.get('name', '')}
                </div>
                <div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:1px;margin-top:3px;">
                  {label} · {b.get('category', '')}
                </div>
              </td>
            </tr>
          </table>
          {f'<p style="font-style:italic;color:#444;margin:6px 0;border-left:3px solid {border};padding-left:10px;font-size:13px;">{b["thesis"]}</p>' if b.get('thesis') else ''}
          {f'<p style="font-size:11px;color:{border};font-weight:600;margin:4px 0;">Theme: {_strip_year(b["theme"])}</p>' if b.get('theme') else ''}
        </div>
        """

    hot_html  = "".join(brand_card(b, True)  for b in hot_signals)
    warm_html = "".join(brand_card(b, False) for b in warm_signals)

    sections = ""
    if hot_signals:
        sections += f"""
        <div style="margin-bottom:8px;">
          <div style="font-family:monospace;font-size:11px;color:#052EF0;letter-spacing:2px;font-weight:bold;text-transform:uppercase;margin-bottom:4px;">
            🔵 HOT ({len(hot_signals)})
          </div>
          {hot_html}
        </div>"""
    if warm_signals:
        sections += f"""
        <div>
          <div style="font-family:monospace;font-size:11px;color:#000;letter-spacing:2px;font-weight:bold;text-transform:uppercase;margin-bottom:4px;">
            ◼ WARM ({len(warm_signals)})
          </div>
          {warm_html}
        </div>"""

    total = len(hot_signals) + len(warm_signals)
    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">
        <div style="padding:28px 36px 20px;border-bottom:1px solid #222;">
          {_LOGO_LOCKUP}
          <h1 style="margin:12px 0 0;color:#fff;font-family:monospace;font-size:22px;font-weight:bold;letter-spacing:3px;">
            WEEK OF {week_label.upper()}
          </h1>
          <p style="margin:6px 0 0;color:#888;font-size:13px;">
            {total} HOT/WARM signal{'' if total == 1 else 's'} this week
          </p>
        </div>
        <div style="padding:20px 36px;">
          {sections}
        </div>
        <div style="padding:0 36px 28px;">
          <a href="{app_url}"
             style="display:inline-block;background:#052EF0;color:#fff;text-decoration:none;
                    padding:12px 24px;border-radius:6px;font-family:monospace;font-weight:bold;
                    font-size:12px;letter-spacing:1px;text-transform:uppercase;">
            View in Stealth Startup Finder →
          </a>
        </div>
        <div style="padding:14px 36px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:10px;">
            Bullish Brand Fund III · Stealth Startup Finder · Weekly Signal Digest
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    digest_plain = f"Stealth Startup Finder Weekly — {week_label}\n\n"
    if hot_signals:
        digest_plain += "HOT\n"
        for b in hot_signals:
            digest_plain += f"  {b.get('score','—')}  {b.get('name','').upper()}\n"
    if warm_signals:
        digest_plain += "\nWARM\n"
        for b in warm_signals:
            digest_plain += f"  {b.get('score','—')}  {b.get('name','').upper()}\n"
    digest_plain += f"\nView in Stealth Startup Finder: {app_url}\n"

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": f"Stealth Startup Finder Weekly — {len(hot_signals)} HOT, {len(warm_signals)} WARM · {week_label}",
        "html":    html,
        "text":    digest_plain,
    })


def send_confluence_alert(
    to_email: str,
    brand_name: str,
    brand_key: str,
    signal_count: int,
    signal_types: list,
    timeline: list,
    span_days: int,
    bullish_score: int = None,
    watch_level: str = None,
) -> None:
    """
    Send a confluence alert when a brand accumulates multiple signal types.

    timeline is a list of dicts: [{signal_type, detected_at, source_url}, ...]
    """
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    signal_label_map = {
        "trademark":   ("TM",   "Trademark Filed"),
        "delaware":    ("DE",   "Delaware LLC"),
        "domain":      ("URL",  "Domain Registered"),
        "producthunt": ("PH",   "Product Hunt"),
        "instagram":   ("IG",   "Instagram"),
        "shopify":     ("SHOP", "Shopify Store"),
    }

    # Build timeline rows
    timeline_html = ""
    for i, row in enumerate(timeline):
        sig = row["signal_type"]
        badge, label = signal_label_map.get(sig, (sig.upper()[:4], sig.title()))
        connector = "" if i == len(timeline) - 1 else (
            '<div style="width:2px;height:16px;background:#052EF0;margin:0 auto;opacity:0.3;"></div>'
        )
        url_link = (
            f'<a href="{row["source_url"]}" style="color:#052EF0;font-size:10px;text-decoration:none;margin-left:8px;">↗</a>'
            if row.get("source_url") else ""
        )
        timeline_html += f"""
        <div style="display:flex;align-items:center;gap:12px;margin:4px 0;">
          <div style="background:#052EF0;color:#fff;border-radius:4px;padding:3px 7px;
                      font-family:monospace;font-weight:bold;font-size:10px;
                      min-width:40px;text-align:center;letter-spacing:1px;">
            {badge}
          </div>
          <div style="flex:1;">
            <span style="font-size:13px;color:#fff;font-weight:600;">{label}</span>
            <span style="font-size:11px;color:#666;margin-left:8px;">{row['detected_at']}</span>
            {url_link}
          </div>
        </div>
        {connector}
        """

    # Score badge (if enriched)
    score_block = ""
    if bullish_score is not None:
        level_color = "#052EF0" if watch_level == "hot" else ("#555" if watch_level == "warm" else "#333")
        level_label = (watch_level or "").upper()
        score_block = f"""
        <div style="display:flex;align-items:center;gap:10px;margin-top:16px;
                    padding:12px 16px;background:#111;border-radius:6px;
                    border-left:3px solid {level_color};">
          <div style="background:{level_color};color:#fff;border-radius:5px;
                      padding:6px 10px;font-family:monospace;font-weight:bold;
                      font-size:22px;min-width:52px;text-align:center;">
            {bullish_score}
          </div>
          <div>
            <div style="color:#fff;font-family:monospace;font-size:11px;
                        font-weight:bold;letter-spacing:1px;">{level_label}</div>
            <div style="color:#666;font-size:10px;margin-top:2px;">Bullish Score</div>
          </div>
        </div>
        """

    span_text = f"{span_days} days between first and latest signal" if span_days > 0 else "Multiple signals detected"
    types_text = " · ".join(t.upper() for t in sorted(signal_types))

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">

        <!-- Header -->
        <div style="padding:28px 36px 20px;border-bottom:1px solid #222;">
          {_LOGO_LOCKUP}
          <h1 style="margin:0;color:#052EF0;font-family:monospace;font-size:26px;
                     font-weight:bold;letter-spacing:3px;">
            ⚡ SIGNAL CONFLUENCE
          </h1>
          <p style="margin:6px 0 0;color:#888;font-size:13px;">
            {signal_count} distinct signals · {span_text}
          </p>
        </div>

        <!-- Brand + timeline -->
        <div style="padding:24px 36px;">
          <div style="font-family:monospace;font-weight:bold;font-size:24px;
                      letter-spacing:3px;text-transform:uppercase;color:#fff;margin-bottom:4px;">
            {brand_name}
          </div>
          <div style="font-size:11px;color:#052EF0;font-weight:600;
                      letter-spacing:1px;margin-bottom:20px;">
            {types_text}
          </div>

          <!-- Timeline -->
          <div style="padding:16px;background:#0a0a0a;border-radius:8px;
                      border:1px solid #1a1a1a;">
            {timeline_html}
          </div>

          {score_block}
        </div>

        <!-- CTA -->
        <div style="padding:0 36px 28px;">
          <a href="{app_url}"
             style="display:inline-block;background:#052EF0;color:#fff;
                    text-decoration:none;padding:12px 24px;border-radius:6px;
                    font-family:monospace;font-weight:bold;font-size:12px;
                    letter-spacing:1px;text-transform:uppercase;">
            Open in Stealth Startup Finder →
          </a>
        </div>

        <!-- Footer -->
        <div style="padding:14px 36px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:10px;">
            Bullish Brand Fund III · Stealth Startup Finder · Confluence Detection
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    resend.Emails.send({
        "from":    from_address,
        "to":      [to_email],
        "subject": f"⚡ {brand_name} — {signal_count} signals in {span_days}d · Stealth Startup Finder",
        "html":    html,
    })


def send_founder_alert(
    to_email: str,
    brand_name: str,
    founder_name: str,
    founder_score: int,
    founder_tier: str,
    brand_score: int = None,
    watch_level: str = None,
    linkedin_url: str = None,
    breakdown: dict = None,
) -> None:
    """
    Send a founder enrichment alert email when a HOT founder is detected.

    Shows brand + watch-level badge, brand score / founder score side-by-side,
    founder tier badge, LinkedIn link, and top breakdown items.
    """
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    # Watch level badge
    level_color = "#052EF0" if (watch_level or "").lower() == "hot" else "#333"
    level_label = (watch_level or "unknown").upper()

    # Brand score block
    brand_score_html = ""
    if brand_score is not None:
        brand_score_html = f"""
        <div style="flex:1;background:#0a0a0a;border-radius:6px;padding:16px;text-align:center;border:1px solid #222;">
          <div style="color:#888;font-size:10px;font-family:monospace;letter-spacing:1px;
                      text-transform:uppercase;margin-bottom:6px;">Brand Score</div>
          <div style="color:#fff;font-family:monospace;font-weight:bold;font-size:28px;">
            {brand_score}
          </div>
          <div style="background:{level_color};color:#fff;border-radius:4px;padding:3px 8px;
                      font-family:monospace;font-size:10px;font-weight:bold;
                      letter-spacing:1px;display:inline-block;margin-top:6px;">
            {level_label}
          </div>
        </div>
        """

    # Founder score tier color
    tier_color = "#052EF0" if founder_tier in ("HIGH_PRIORITY",) else (
        "#555" if founder_tier == "WATCH_LIST" else "#333"
    )
    tier_label = (founder_tier or "UNKNOWN").replace("_", " ")

    founder_score_html = f"""
    <div style="flex:1;background:#0a0a0a;border-radius:6px;padding:16px;text-align:center;border:1px solid #222;">
      <div style="color:#888;font-size:10px;font-family:monospace;letter-spacing:1px;
                  text-transform:uppercase;margin-bottom:6px;">Founder Score</div>
      <div style="color:#052EF0;font-family:monospace;font-weight:bold;font-size:28px;">
        {founder_score}
      </div>
      <div style="background:{tier_color};color:#fff;border-radius:4px;padding:3px 8px;
                  font-family:monospace;font-size:10px;font-weight:bold;
                  letter-spacing:1px;display:inline-block;margin-top:6px;">
        {tier_label}
      </div>
    </div>
    """

    # LinkedIn link block
    linkedin_html = ""
    if linkedin_url:
        linkedin_html = f"""
        <div style="margin-top:16px;">
          <a href="{linkedin_url}"
             style="color:#052EF0;font-family:monospace;font-size:12px;
                    text-decoration:none;font-weight:600;">
            ↗ View LinkedIn Profile
          </a>
        </div>
        """

    # Breakdown chips (top 2-3 items)
    breakdown_html = ""
    if breakdown:
        priority_keys = ["chip_on_shoulder", "category_proximity", "magnetic_signal"]
        chips = ""
        for key in priority_keys:
            item = breakdown.get(key)
            if not item:
                continue
            score_val = item.get("score", 0)
            max_val   = item.get("max", 0)
            label     = key.replace("_", " ").title()
            flags     = item.get("flags", [])
            flag_text = flags[0] if flags else ""
            chips += f"""
            <div style="background:#111;border-radius:6px;padding:10px 14px;margin:6px 0;
                        border-left:3px solid #052EF0;">
              <div style="display:flex;align-items:center;justify-content:space-between;">
                <span style="color:#fff;font-family:monospace;font-size:11px;
                             font-weight:bold;letter-spacing:1px;">{label}</span>
                <span style="color:#052EF0;font-family:monospace;font-size:13px;
                             font-weight:bold;">{score_val}<span style="color:#444;font-size:10px;">/{max_val}</span></span>
              </div>
              {f'<div style="color:#888;font-size:11px;margin-top:4px;">{flag_text}</div>' if flag_text else ''}
            </div>
            """
        if chips:
            breakdown_html = f"""
            <div style="margin-top:20px;">
              <div style="font-family:monospace;font-size:10px;color:#666;letter-spacing:2px;
                          text-transform:uppercase;margin-bottom:8px;">Score Breakdown</div>
              {chips}
            </div>
            """

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">

        <!-- Header -->
        <div style="padding:28px 36px 20px;border-bottom:1px solid #222;">
          {_LOGO_LOCKUP}
          <h1 style="margin:0;color:#052EF0;font-family:monospace;font-size:26px;
                     font-weight:bold;letter-spacing:3px;">
            🔵 FOUNDER SIGNAL
          </h1>
          <p style="margin:6px 0 0;color:#888;font-size:13px;">
            High-conviction founder identified · LinkedIn enriched
          </p>
        </div>

        <!-- Brand + Founder -->
        <div style="padding:24px 36px 0;">
          <div style="font-family:monospace;font-weight:bold;font-size:24px;
                      letter-spacing:3px;text-transform:uppercase;color:#fff;margin-bottom:2px;">
            {brand_name}
          </div>
          <div style="font-size:14px;color:#ccc;margin-bottom:20px;">
            Founder: <strong style="color:#fff;">{founder_name}</strong>
          </div>

          <!-- Score cards side by side -->
          <div style="display:flex;gap:12px;margin-bottom:0;">
            {brand_score_html}
            {founder_score_html}
          </div>

          {linkedin_html}
          {breakdown_html}
        </div>

        <!-- CTA -->
        <div style="padding:24px 36px 28px;">
          <a href="{app_url}"
             style="display:inline-block;background:#052EF0;color:#fff;
                    text-decoration:none;padding:12px 24px;border-radius:6px;
                    font-family:monospace;font-weight:bold;font-size:12px;
                    letter-spacing:1px;text-transform:uppercase;">
            View Signal →
          </a>
        </div>

        <!-- Footer -->
        <div style="padding:14px 36px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:10px;">
            Bullish Brand Fund III · Stealth Startup Finder · Founder Intelligence
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    plain_text = (
        f"Bullish Stealth Startup Finder — Founder Signal\n\n"
        f"Brand: {brand_name} ({level_label})\n"
        f"Founder: {founder_name}\n"
        f"Founder Score: {founder_score} ({tier_label})\n"
    )
    if brand_score is not None:
        plain_text += f"Brand Score: {brand_score}\n"
    if linkedin_url:
        plain_text += f"LinkedIn: {linkedin_url}\n"
    plain_text += f"\nView in Stealth Startup Finder: {app_url}\n"

    resend.Emails.send({
        "from":    from_address,
        "to":      [to_email],
        "subject": f"🔵 {founder_name} · {brand_name} Founder — Score {founder_score} · Stealth Startup Finder",
        "html":    html,
        "text":    plain_text,
    })


def send_founder_news_alert(to_email, founder_name, company, bullish_score, new_articles, linkedin_url=""):
    """Send a founder news alert when new press coverage is found."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return
    from_address = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    count = len(new_articles)
    article_html = ""
    for a in new_articles:
        article_html += f"""
        <div style="border-left:3px solid #052EF0;padding:10px 14px;margin:10px 0;background:#fff;">
          <a href="{a['link']}" style="font-size:13px;font-weight:bold;color:#052EF0;text-decoration:none;">{a['title']}</a>
          <div style="font-size:11px;color:#999;margin:3px 0;">{a.get('source','')} · {a.get('date','')}</div>
          <div style="font-size:12px;color:#555;margin-top:4px;">{a.get('snippet','')}</div>
        </div>
        """

    score_html = f"""<div style="background:#052EF0;color:#fff;border-radius:6px;padding:6px 14px;
                       font-family:monospace;font-weight:bold;font-size:18px;display:inline-block;margin-bottom:8px;">
                       {bullish_score}</div>""" if bullish_score else ""

    linkedin_html = (f'<a href="{linkedin_url}" style="color:#052EF0;font-size:12px;text-decoration:none;">'
                     f'View LinkedIn &rarr;</a>') if linkedin_url else ""

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">
    <div style="padding:32px 40px 24px;border-bottom:1px solid #222;">
      {_LOGO_LOCKUP}
      <h1 style="margin:0;color:#fff;font-family:monospace;font-size:22px;font-weight:bold;letter-spacing:2px;">
        FOUNDER UPDATE
      </h1>
      <p style="margin:8px 0 0;color:#888;font-size:13px;">
        New press coverage detected for {founder_name} &middot; {company}
      </p>
    </div>
    <div style="padding:24px 40px;">
      {score_html}
      <div style="font-family:monospace;font-weight:bold;font-size:18px;letter-spacing:2px;
                  text-transform:uppercase;color:#fff;margin-bottom:4px;">{company}</div>
      <div style="font-size:13px;color:#888;margin-bottom:4px;">{founder_name}</div>
      {linkedin_html}
      <div style="margin-top:20px;">
        <div style="font-size:10px;font-weight:bold;letter-spacing:2px;color:#666;
                    text-transform:uppercase;margin-bottom:8px;">
          {count} NEW ARTICLE{'S' if count != 1 else ''}
        </div>
        {article_html}
      </div>
    </div>
    <div style="padding:16px 40px 32px;">
      <a href="{app_url}" style="display:inline-block;background:#052EF0;color:#fff;text-decoration:none;
         padding:12px 24px;border-radius:6px;font-family:monospace;font-weight:bold;
         font-size:12px;letter-spacing:1px;text-transform:uppercase;">
        Open Watchlist &rarr;
      </a>
    </div>
    <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
      <p style="margin:0;color:#555;font-size:11px;">
        Bullish Brand Fund III &middot; Stealth Startup Finder &middot; Founder Intelligence
      </p>
    </div>
  </div>
</body>
</html>"""

    plain = f"Founder Update: {founder_name} ({company})\n\n{count} new article(s):\n\n"
    for a in new_articles:
        plain += f"- {a['title']}\n  {a['link']}\n  {a.get('snippet','')}\n\n"
    plain += f"View Watchlist: {app_url}\n"

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": f"Founder Update: {founder_name} · {company} — {count} new article{'s' if count != 1 else ''} · Stealth Startup Finder",
        "html":    html,
        "text":    plain,
    })


def send_rescore_alert(to_email, brand_name, old_score, new_score, new_signal_type, signal_types, thesis=""):
    """Alert when a watchlisted brand's score jumps >=5 points due to a new signal."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return
    from_address = _resend_client()
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")
    delta = new_score - old_score
    signal_chips = "".join(
        f'<span style="background:#1a1a1a;color:#888;font-size:10px;padding:3px 8px;border-radius:3px;'
        f'margin-right:4px;text-transform:uppercase;letter-spacing:1px;">{s}</span>'
        for s in signal_types
    )
    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">
    <div style="padding:32px 40px 24px;border-bottom:1px solid #222;">
      {_LOGO_LOCKUP}
      <h1 style="margin:0;color:#052EF0;font-family:monospace;font-size:22px;font-weight:bold;letter-spacing:2px;">
        SCORE JUMP
      </h1>
      <p style="margin:8px 0 0;color:#888;font-size:13px;">
        A watchlisted brand just got stronger
      </p>
    </div>
    <div style="padding:24px 40px;">
      <div style="font-family:monospace;font-weight:bold;font-size:20px;letter-spacing:2px;
                  text-transform:uppercase;color:#fff;margin-bottom:12px;">{brand_name}</div>
      <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
        <tr>
          <td style="background:#333;color:#888;border-radius:6px;padding:8px 14px;
                     font-family:monospace;font-weight:bold;font-size:20px;text-align:center;">
            {old_score}
          </td>
          <td style="padding:0 12px;color:#052EF0;font-size:20px;font-weight:bold;">&rarr;</td>
          <td style="background:#052EF0;color:#fff;border-radius:6px;padding:8px 14px;
                     font-family:monospace;font-weight:bold;font-size:20px;text-align:center;">
            {new_score}
          </td>
          <td style="padding-left:12px;color:#16a34a;font-size:13px;font-weight:bold;">
            +{delta} pts
          </td>
        </tr>
      </table>
      <div style="margin-bottom:12px;">{signal_chips}</div>
      <p style="font-size:11px;color:#666;margin-bottom:4px;">NEW SIGNAL: <span style="color:#fff;">{new_signal_type.upper()}</span></p>
      {f'<p style="font-style:italic;color:#888;font-size:13px;border-left:3px solid #052EF0;padding-left:12px;margin-top:12px;">{thesis}</p>' if thesis else ''}
    </div>
    <div style="padding:0 40px 32px;">
      <a href="{app_url}" style="display:inline-block;background:#052EF0;color:#fff;text-decoration:none;
         padding:12px 24px;border-radius:6px;font-family:monospace;font-weight:bold;
         font-size:12px;letter-spacing:1px;text-transform:uppercase;">
        View Watchlist &rarr;
      </a>
    </div>
    <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
      <p style="margin:0;color:#555;font-size:11px;">Bullish Brand Fund III &middot; Stealth Startup Finder</p>
    </div>
  </div>
</body>
</html>"""
    plain = f"Score Jump: {brand_name}\n{old_score} -> {new_score} (+{delta} pts)\nNew signal: {new_signal_type}\n\n{thesis}\n\nView Watchlist: {app_url}\n"
    resend.Emails.send({
        "from": f"Bullish <{_resend_client()}>",
        "to": [to_email],
        "subject": f"{brand_name} +{delta}pts ({old_score}->{new_score}) · New {new_signal_type} signal · Stealth Startup Finder",
        "html": html, "text": plain,
    })


def send_password_reset_email(to_email: str, reset_url: str) -> None:
    """Send a password-reset email via Resend.

    Suppressed when MAIL_SUPPRESS_SEND=true (set automatically in TestingConfig).
    Raises RuntimeError if RESEND_API_KEY is not configured.
    """
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    reply_to = os.environ.get("MAIL_REPLY_TO", "brent@bullish.co")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reset your password</title>
</head>
<body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">
    <div style="padding:32px 40px 24px;border-bottom:1px solid #222;text-align:center;">
      <div style="font-family:Georgia,serif;font-style:italic;color:#fff;font-size:20px;
                  letter-spacing:2px;">
        Bullish Stealth Startup Finder
      </div>
    </div>
    <div style="padding:32px 40px;">
      <h1 style="margin:0 0 12px;color:#fff;font-family:monospace;font-size:22px;
                 font-weight:bold;letter-spacing:2px;text-transform:uppercase;">
        Password Reset
      </h1>
      <p style="color:#ccc;font-size:14px;line-height:1.7;margin:0 0 24px;">
        We received a request to reset the password for your account.
        Click the button below to choose a new password.
      </p>
      <a href="{reset_url}"
         style="display:inline-block;background:#052EF0;color:#fff;text-decoration:none;
                padding:14px 28px;border-radius:6px;font-family:monospace;font-weight:bold;
                font-size:13px;letter-spacing:1px;text-transform:uppercase;">
        Reset Password
      </a>
      <p style="color:#555;font-size:11px;margin:20px 0 0;">
        This link expires in 1 hour. If you did not request a password reset,
        you can safely ignore this email.
      </p>
    </div>
    <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
      <p style="margin:0;color:#444;font-size:11px;">
        Bullish Brand Fund III &middot; Stealth Startup Finder
      </p>
    </div>
  </div>
</body>
</html>"""

    plain_text = f"""Bullish Stealth Startup Finder — Password Reset

We received a request to reset the password for your account.

Reset your password here:
{reset_url}

This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email.

—
Bullish Brand Fund III · Stealth Startup Finder
"""

    resend.Emails.send({
        "from":     from_with_name,
        "to":       [to_email],
        "reply_to": reply_to,
        "subject":  "Reset your Bullish Stealth Startup Finder password",
        "html":     html,
        "text":     plain_text,
        "headers": {
            "List-Unsubscribe": f"<mailto:{from_address}?subject=unsubscribe>",
        },
    })


def send_linkedin_poll_reminder_email(
    to_email: str,
    eligible_count: int,
    estimated_cost: float,
    settings_url: str,
) -> None:
    """Quarterly reminder to admins: time to run the LinkedIn network poll."""
    from_address   = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    subject        = f"⏰ Quarterly LinkedIn Network Poll — {eligible_count:,} contacts, ~${estimated_cost:.2f} estimated"

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#F5F0EB;margin:0;padding:40px 20px;">
  <div style="max-width:540px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;border:1px solid #E5E5E0;">
    <div style="background:#052EF0;padding:28px 32px;">
      <p style="color:rgba(255,255,255,.7);font-size:11px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 6px;">Bullish Stealth Startup Finder</p>
      <h1 style="color:white;font-size:22px;font-weight:700;margin:0;">Quarterly LinkedIn Poll</h1>
      <p style="color:rgba(255,255,255,.8);font-size:13px;margin:8px 0 0;">Time to check your network for stealth founders.</p>
    </div>
    <div style="padding:28px 32px;">
      <p style="color:#333;font-size:14px;line-height:1.6;margin:0 0 20px;">
        The quarterly LinkedIn headline poll scans your imported connections for people who've quietly changed
        their title to <strong>Founder</strong>, <strong>Building</strong>, <strong>CEO</strong>, or other
        stealth signals — before any press announcement.
      </p>
      <div style="background:#F5F0EB;border-radius:6px;padding:16px 20px;margin-bottom:24px;">
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Eligible contacts</td>
            <td style="font-size:14px;font-weight:700;color:#052EF0;text-align:right;">{eligible_count:,}</td>
          </tr>
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Credits needed</td>
            <td style="font-size:14px;font-weight:700;color:#333;text-align:right;">{eligible_count * 3:,}</td>
          </tr>
          <tr style="border-top:1px solid #E5E5E0;">
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:8px 0 4px;">Estimated cost</td>
            <td style="font-size:16px;font-weight:700;color:#333;text-align:right;padding-top:8px;">${estimated_cost:.2f}</td>
          </tr>
        </table>
      </div>
      <p style="color:#666;font-size:13px;line-height:1.6;margin:0 0 24px;">
        Log in to see the live estimate (including current credit balance) and click <strong>Approve &amp; Run</strong>.
        Only admins can trigger the poll — no credits are spent until you confirm.
      </p>
      <a href="{settings_url}" style="display:inline-block;background:#052EF0;color:white;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:12px 24px;border-radius:4px;text-decoration:none;">
        Review &amp; Approve →
      </a>
    </div>
    <div style="padding:16px 32px;border-top:1px solid #E5E5E0;">
      <p style="color:#999;font-size:11px;margin:0;">Bullish Brand Fund III · Confidential · bullish.co</p>
    </div>
  </div>
</body>
</html>"""

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    })


def send_linkedin_poll_complete_email(
    to_email: str,
    polled: int,
    stealth_count: int,
    stealth_contacts: list,
    errors: int,
) -> None:
    """Summary email to admins when the quarterly LinkedIn poll finishes."""
    from_address   = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    subject        = f"{'✅' if stealth_count > 0 else '📊'} LinkedIn Poll Complete — {stealth_count} stealth signal{'s' if stealth_count != 1 else ''} detected"

    stealth_rows = ""
    for c in (stealth_contacts or []):
        stealth_rows += (
            f"<tr>"
            f"<td style='padding:8px 0;border-bottom:1px solid #E5E5E0;font-size:13px;color:#333;'>{c.get('name','')}</td>"
            f"<td style='padding:8px 0;border-bottom:1px solid #E5E5E0;font-size:12px;color:#666;'>{c.get('old_headline','') or '—'}</td>"
            f"<td style='padding:8px 0;border-bottom:1px solid #E5E5E0;font-size:12px;font-weight:600;color:#052EF0;'>{c.get('new_headline','')}</td>"
            f"</tr>"
        )

    stealth_section = (
        "<h3 style='font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#333;margin:24px 0 12px;'>Stealth Signals Detected</h3>"
        "<table style='width:100%;border-collapse:collapse;'>"
        "<thead><tr>"
        "<th style='text-align:left;font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.08em;padding-bottom:6px;'>Name</th>"
        "<th style='text-align:left;font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.08em;padding-bottom:6px;'>Old Headline</th>"
        "<th style='text-align:left;font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.08em;padding-bottom:6px;'>New Headline</th>"
        "</tr></thead>"
        f"<tbody>{stealth_rows}</tbody></table>"
    ) if stealth_contacts else (
        "<p style='color:#666;font-size:13px;line-height:1.6;margin:16px 0 0;'>"
        "No stealth signals this quarter. Results are stored in each contact's Watchlist entry."
        "</p>"
    )

    header_bg  = "#052EF0" if stealth_count > 0 else "#020A52"
    errors_row = (
        f"<tr><td style='font-size:12px;color:#999;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;'>Errors</td>"
        f"<td style='font-size:13px;color:#999;text-align:right;'>{errors}</td></tr>"
    ) if errors else ""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#F5F0EB;margin:0;padding:40px 20px;">
  <div style="max-width:540px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;border:1px solid #E5E5E0;">
    <div style="background:{header_bg};padding:28px 32px;">
      <p style="color:rgba(255,255,255,.7);font-size:11px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 6px;">Bullish Stealth Startup Finder</p>
      <h1 style="color:white;font-size:22px;font-weight:700;margin:0;">LinkedIn Poll Complete</h1>
      <p style="color:rgba(255,255,255,.8);font-size:13px;margin:8px 0 0;">Scanned {polled:,} contacts · {stealth_count} stealth signal{'s' if stealth_count != 1 else ''} detected</p>
    </div>
    <div style="padding:28px 32px;">
      <div style="background:#F5F0EB;border-radius:6px;padding:16px 20px;margin-bottom:20px;">
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Profiles scanned</td>
            <td style="font-size:14px;font-weight:700;color:#333;text-align:right;">{polled:,}</td>
          </tr>
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Stealth signals</td>
            <td style="font-size:14px;font-weight:700;color:#052EF0;text-align:right;">{stealth_count}</td>
          </tr>
          {errors_row}
        </table>
      </div>
      {stealth_section}
    </div>
    <div style="padding:16px 32px;border-top:1px solid #E5E5E0;">
      <p style="color:#999;font-size:11px;margin:0;">Bullish Brand Fund III · Confidential · bullish.co</p>
    </div>
  </div>
</body>
</html>"""

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    })


def send_founder_radar_poll_reminder_email(
    to_email: str,
    people_count: int,
    by_tier: dict,
    estimated_cost: float,
    settings_url: str,
) -> None:
    """Quarterly reminder to admins: time to run the Founder Radar poll."""
    from_address   = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    subject        = f"⚡ Quarterly Founder Radar Poll — {people_count:,} founders, ~${estimated_cost:.2f} estimated"

    tier_rows = ""
    tier_labels = {"DEPARTURE": "Recent Departures", "CONVICTION": "Conviction Founders",
                   "ALUMNI": "Exit Alumni", "EXEC": "Brand Executives"}
    for tier in ["DEPARTURE", "CONVICTION", "ALUMNI", "EXEC"]:
        count = by_tier.get(tier, 0)
        if count:
            tier_rows += (
                f"<tr>"
                f"<td style='font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;'>"
                f"{tier_labels.get(tier, tier)}</td>"
                f"<td style='font-size:14px;font-weight:700;color:#052EF0;text-align:right;'>{count}</td>"
                f"</tr>"
            )

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#F5F0EB;margin:0;padding:40px 20px;">
  <div style="max-width:540px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;border:1px solid #E5E5E0;">
    <div style="background:#020A52;padding:28px 32px;">
      <p style="color:rgba(255,255,255,.7);font-size:11px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 6px;">Bullish Stealth Startup Finder</p>
      <h1 style="color:white;font-size:22px;font-weight:700;margin:0;">⚡ Quarterly Founder Radar Poll</h1>
      <p style="color:rgba(255,255,255,.8);font-size:13px;margin:8px 0 0;">Time to check which conviction founders have gone stealth.</p>
    </div>
    <div style="padding:28px 32px;">
      <p style="color:#333;font-size:14px;line-height:1.6;margin:0 0 20px;">
        The Founder Radar poll looks up every name in your curated founder list via NinjaPear —
        detecting headline changes like <strong>Founder</strong>, <strong>Building</strong>, or
        <strong>CEO at [new brand]</strong> that signal someone has quietly gone stealth.
      </p>
      <div style="background:#F5F0EB;border-radius:6px;padding:16px 20px;margin-bottom:24px;">
        <table style="width:100%;border-collapse:collapse;">
          {tier_rows}
          <tr style="border-top:1px solid #E5E5E0;">
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:8px 0 4px;font-weight:700;">Total founders</td>
            <td style="font-size:15px;font-weight:700;color:#020A52;text-align:right;padding-top:8px;">{people_count:,}</td>
          </tr>
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Credits needed</td>
            <td style="font-size:14px;font-weight:700;color:#333;text-align:right;">{people_count * 3:,}</td>
          </tr>
          <tr style="border-top:1px solid #E5E5E0;">
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:8px 0 4px;">Estimated cost</td>
            <td style="font-size:16px;font-weight:700;color:#333;text-align:right;padding-top:8px;">${estimated_cost:.2f}</td>
          </tr>
        </table>
      </div>
      <p style="color:#666;font-size:13px;line-height:1.6;margin:0 0 24px;">
        Log in and go to <strong>Settings → Tools → Founder Radar Poll</strong> to see the live
        estimate and click <strong>Approve &amp; Run</strong>. No credits are spent until you confirm.
      </p>
      <a href="{settings_url}" style="display:inline-block;background:#020A52;color:white;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:12px 24px;border-radius:4px;text-decoration:none;">
        Review &amp; Approve →
      </a>
    </div>
    <div style="padding:16px 32px;border-top:1px solid #E5E5E0;">
      <p style="color:#999;font-size:11px;margin:0;">Bullish Brand Fund III · Confidential · bullish.co</p>
    </div>
  </div>
</body>
</html>"""

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    })


def send_founder_radar_poll_complete_email(
    to_email: str,
    polled: int,
    updated: int,
    stealth_count: int,
    stealth_people: list,
    errors: int,
) -> None:
    """Summary email to admins when the quarterly Founder Radar poll finishes."""
    from_address   = _resend_client()
    from_with_name = f"Bullish <{from_address}>"
    subject        = (
        f"{'⚡' if stealth_count > 0 else '📊'} Founder Radar Poll Complete — "
        f"{stealth_count} stealth signal{'s' if stealth_count != 1 else ''} detected"
    )

    tier_badge = {"DEPARTURE": "🚨", "CONVICTION": "⚡", "ALUMNI": "🏆", "EXEC": "👤"}
    stealth_rows = ""
    for p in (stealth_people or []):
        badge = tier_badge.get(p.get("tier", ""), "")
        stealth_rows += (
            f"<tr>"
            f"<td style='padding:8px 0;border-bottom:1px solid #E5E5E0;font-size:13px;color:#333;white-space:nowrap;'>"
            f"{badge} {p.get('name','')}</td>"
            f"<td style='padding:8px 6px;border-bottom:1px solid #E5E5E0;font-size:11px;color:#999;'>"
            f"{p.get('known_brand','')}</td>"
            f"<td style='padding:8px 0;border-bottom:1px solid #E5E5E0;font-size:12px;font-weight:600;color:#052EF0;'>"
            f"{(p.get('current_company') or p.get('headline',''))[:60]}</td>"
            f"</tr>"
        )

    stealth_section = (
        "<h3 style='font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;"
        "color:#333;margin:24px 0 12px;'>Stealth Signals Detected</h3>"
        "<table style='width:100%;border-collapse:collapse;'>"
        "<thead><tr>"
        "<th style='text-align:left;font-size:11px;color:#999;text-transform:uppercase;"
        "letter-spacing:.08em;padding-bottom:6px;'>Name</th>"
        "<th style='text-align:left;font-size:11px;color:#999;text-transform:uppercase;"
        "letter-spacing:.08em;padding-bottom:6px;'>Known Brand</th>"
        "<th style='text-align:left;font-size:11px;color:#999;text-transform:uppercase;"
        "letter-spacing:.08em;padding-bottom:6px;'>Now At</th>"
        "</tr></thead>"
        f"<tbody>{stealth_rows}</tbody></table>"
    ) if stealth_people else (
        "<p style='color:#666;font-size:13px;line-height:1.6;margin:16px 0 0;'>"
        "No new stealth signals this quarter. Founder Radar profiles have been updated."
        "</p>"
    )

    header_bg  = "#020A52" if stealth_count > 0 else "#333"
    errors_row = (
        f"<tr><td style='font-size:12px;color:#999;text-transform:uppercase;"
        f"letter-spacing:.08em;padding:4px 0;'>Errors</td>"
        f"<td style='font-size:13px;color:#999;text-align:right;'>{errors}</td></tr>"
    ) if errors else ""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#F5F0EB;margin:0;padding:40px 20px;">
  <div style="max-width:540px;margin:0 auto;background:white;border-radius:8px;overflow:hidden;border:1px solid #E5E5E0;">
    <div style="background:{header_bg};padding:28px 32px;">
      <p style="color:rgba(255,255,255,.7);font-size:11px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 6px;">Bullish Stealth Startup Finder</p>
      <h1 style="color:white;font-size:22px;font-weight:700;margin:0;">Founder Radar Poll Complete</h1>
      <p style="color:rgba(255,255,255,.8);font-size:13px;margin:8px 0 0;">
        Scanned {polled:,} founders · {stealth_count} stealth signal{'s' if stealth_count != 1 else ''} detected
      </p>
    </div>
    <div style="padding:28px 32px;">
      <div style="background:#F5F0EB;border-radius:6px;padding:16px 20px;margin-bottom:20px;">
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Founders scanned</td>
            <td style="font-size:14px;font-weight:700;color:#333;text-align:right;">{polled:,}</td>
          </tr>
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Profiles updated in Radar</td>
            <td style="font-size:14px;font-weight:700;color:#333;text-align:right;">{updated:,}</td>
          </tr>
          <tr>
            <td style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.08em;padding:4px 0;">Stealth signals</td>
            <td style="font-size:14px;font-weight:700;color:#052EF0;text-align:right;">{stealth_count}</td>
          </tr>
          {errors_row}
        </table>
      </div>
      {stealth_section}
    </div>
    <div style="padding:16px 32px;border-top:1px solid #E5E5E0;">
      <p style="color:#999;font-size:11px;margin:0;">Bullish Brand Fund III · Confidential · bullish.co</p>
    </div>
  </div>
</body>
</html>"""

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    })
