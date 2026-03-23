import os
import resend


def _resend_client():
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY environment variable is not set")
    resend.api_key = api_key
    return os.environ.get("MAIL_FROM", "noreply@mail.bullish.co")


def send_hot_alert(to_email: str, hot_brands: list, scan_name: str) -> None:
    """Send a HOT signal alert email via Resend when new HOT brands are discovered."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    count = len(hot_brands)
    subject = f"🔵 {count} HOT Signal{'s' if count != 1 else ''} — Stealth Finder"

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
          {f'<p style="font-size:12px;color:#052EF0;font-weight:600;margin:8px 0;">2026 Theme: {b["theme"]}</p>' if b.get('theme') else ''}
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">

        <!-- Header -->
        <div style="padding:32px 40px 24px;border-bottom:1px solid #222;">
          <div style="font-family:monospace;font-size:11px;color:#666;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">
            Bullish Intelligence · Stealth Finder
          </div>
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
            View in Stealth Finder →
          </a>
        </div>

        <!-- Footer -->
        <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:11px;">
            Bullish Brand Fund III · Stealth Finder · Automated Signal Detection
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    resend.Emails.send({
        "from":    from_address,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    })


def send_invite_email(to_email: str, invite_url: str, invited_by: str) -> None:
    """Send a team invite email via Resend."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    # Use a display name for better deliverability
    from_with_name = f"Bullish <{from_address}>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>You're invited to join Bullish Stealth Startup Finder</title>
</head>
<body style="margin:0;padding:0;background:#F5F0EB;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:40px auto;background:#000;border-radius:12px;overflow:hidden;">

    <!-- Logo / Wordmark lockup -->
    <div style="padding:36px 40px 28px;border-bottom:1px solid #222;text-align:center;">
      <!-- Bullish icon SVG -->
      <svg width="28" height="28" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg"
           style="display:block;margin:0 auto 14px;">
        <rect x="1.5" y="1.5" width="33" height="33" stroke="rgba(255,255,255,0.5)" stroke-width="3"/>
        <polygon points="8.5,10.5 18,10.5 17,13 8.5,13" fill="rgba(255,255,255,0.5)"/>
        <polygon points="8.5,22 27,22 26,25 8.5,25" fill="rgba(255,255,255,0.5)"/>
      </svg>
      <!-- Wordmark -->
      <div style="font-family:Georgia,serif;font-style:italic;color:#fff;font-size:22px;
                  letter-spacing:2px;line-height:1.3;margin-bottom:6px;">
        Bullish Stealth Startup Finder
      </div>
      <div style="font-family:monospace;font-size:10px;color:#555;letter-spacing:3px;
                  text-transform:uppercase;">
        Bullish Brand Fund III
      </div>
    </div>

    <!-- YOU'RE INVITED heading -->
    <div style="padding:32px 40px 0;">
      <h1 style="margin:0 0 8px;color:#fff;font-family:monospace;font-size:24px;
                 font-weight:bold;letter-spacing:3px;text-transform:uppercase;">
        YOU'RE INVITED
      </h1>
      <p style="margin:0;color:#888;font-size:14px;">
        {invited_by} has invited you to join the Bullish Stealth Startup Finder team.
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
        Accept Invite &amp; Set Password →
      </a>
      <p style="color:#555;font-size:11px;margin:20px 0 0;">
        This invite link expires in 7 days. If you weren't expecting this, you can safely ignore it.
      </p>
    </div>

    <!-- Footer -->
    <div style="padding:16px 40px;border-top:1px solid #222;text-align:center;">
      <p style="margin:0;color:#444;font-size:11px;">
        Bullish Brand Fund III · Stealth Startup Finder
      </p>
    </div>
  </div>
</body>
</html>"""

    plain_text = f"""You're invited to join Bullish Stealth Startup Finder

{invited_by} has invited you to join the Bullish Stealth Startup Finder team.

Stealth Startup Finder tracks early-stage consumer brand signals — trademark filings, EDGAR incorporations, and domain registrations — enriched with Bullish AI to surface the next Bubble, Hu, or Nom Nom before anyone else.

Accept your invite and set your password here:
{invite_url}

This invite link expires in 7 days. If you weren't expecting this, you can safely ignore it.

—
Bullish Brand Fund III · Stealth Startup Finder
"""

    resend.Emails.send({
        "from":    from_with_name,
        "to":      [to_email],
        "subject": "You're invited to join Bullish Stealth Startup Finder",
        "html":    html,
        "text":    plain_text,
    })


def send_weekly_digest_email(to_email: str, hot_signals: list, warm_signals: list, week_label: str) -> None:
    """Send a weekly top-signals digest email via Resend."""
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    from_address = _resend_client()
    app_url = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")

    def brand_card(b, is_hot):
        border = "#052EF0" if is_hot else "#000"
        score_bg = "#052EF0" if is_hot else "#000"
        label = "HOT" if is_hot else "WARM"
        return f"""
        <div style="border:2px solid {border};border-radius:8px;padding:16px;margin:10px 0;background:#fff;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <div style="background:{score_bg};color:#fff;border-radius:5px;padding:6px 10px;font-family:monospace;font-weight:bold;font-size:18px;min-width:44px;text-align:center;">
              {b.get('score','—')}
            </div>
            <div>
              <div style="font-family:monospace;font-weight:bold;font-size:15px;letter-spacing:2px;text-transform:uppercase;color:#000;">
                {b.get('name','')}
              </div>
              <div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">
                {label} · {b.get('category','')}
              </div>
            </div>
          </div>
          {f'<p style="font-style:italic;color:#444;margin:6px 0;border-left:3px solid {border};padding-left:10px;font-size:13px;">{b["thesis"]}</p>' if b.get('thesis') else ''}
          {f'<p style="font-size:11px;color:{border};font-weight:600;margin:4px 0;">Theme: {b["theme"]}</p>' if b.get('theme') else ''}
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
          <div style="font-family:monospace;font-size:10px;color:#666;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px;">
            Bullish Intelligence · Weekly Digest
          </div>
          <h1 style="margin:0;color:#fff;font-family:monospace;font-size:22px;font-weight:bold;letter-spacing:3px;">
            WEEK OF {week_label.upper()}
          </h1>
          <p style="margin:6px 0 0;color:#888;font-size:13px;">
            {total} HOT/WARM signal{'' if total == 1 else 's'} this week · Stealth Finder
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
            View in Stealth Finder →
          </a>
        </div>
        <div style="padding:14px 36px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:10px;">
            Bullish Brand Fund III · Stealth Finder · Weekly Signal Digest
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    resend.Emails.send({
        "from":    from_address,
        "to":      [to_email],
        "subject": f"Stealth Finder Weekly — {len(hot_signals)} HOT, {len(warm_signals)} WARM · {week_label}",
        "html":    html,
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
          <div style="font-family:monospace;font-size:10px;color:#666;letter-spacing:2px;
                      text-transform:uppercase;margin-bottom:6px;">
            Bullish Intelligence · Stealth Finder
          </div>
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
            Open in Stealth Finder →
          </a>
        </div>

        <!-- Footer -->
        <div style="padding:14px 36px;border-top:1px solid #222;text-align:center;">
          <p style="margin:0;color:#555;font-size:10px;">
            Bullish Brand Fund III · Stealth Finder · Confluence Detection
          </p>
        </div>
      </div>
    </body>
    </html>
    """

    resend.Emails.send({
        "from":    from_address,
        "to":      [to_email],
        "subject": f"⚡ {brand_name} — {signal_count} signals in {span_days}d · Stealth Finder",
        "html":    html,
    })


def send_password_reset_email(to_email: str, reset_url: str) -> None:
    """Send a password-reset email via Resend.

    Suppressed when MAIL_SUPPRESS_SEND=true (set automatically in TestingConfig).
    Raises RuntimeError if RESEND_API_KEY is not configured.
    """
    if os.environ.get("MAIL_SUPPRESS_SEND", "false").lower() == "true":
        return

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY environment variable is not set")

    resend.api_key = api_key
    from_address = os.environ.get("MAIL_FROM", "noreply@yourdomain.com")

    resend.Emails.send({
        "from": from_address,
        "to": [to_email],
        "subject": "Reset your password",
        "html": (
            f"<p>You requested a password reset for your account.</p>"
            f"<p><a href='{reset_url}'>Click here to reset your password</a></p>"
            f"<p>This link expires in 1 hour. If you didn't request this, "
            f"you can safely ignore this email.</p>"
        ),
    })
