import os
import resend


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
