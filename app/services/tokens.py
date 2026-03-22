from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature


def generate_reset_token(secret_key: str, user_id: int) -> str:
    """Generate a signed, time-limited password-reset token."""
    s = URLSafeTimedSerializer(secret_key)
    return s.dumps({"user_id": user_id}, salt="password-reset")


def verify_reset_token(secret_key: str, token: str, max_age: int = 3600) -> int:
    """Verify a reset token and return the user_id.

    Raises:
        SignatureExpired: if the token is older than max_age seconds.
        BadSignature:     if the token is tampered with or otherwise invalid.
    """
    s = URLSafeTimedSerializer(secret_key)
    data = s.loads(token, salt="password-reset", max_age=max_age)
    return data["user_id"]


def generate_invite_token(secret_key: str, email: str) -> str:
    """Generate a signed, time-limited team invite token (7-day TTL)."""
    s = URLSafeTimedSerializer(secret_key)
    return s.dumps({"email": email}, salt="team-invite")


def verify_invite_token(secret_key: str, token: str, max_age: int = 604800) -> str:
    """Verify an invite token and return the invited email.

    Raises:
        SignatureExpired: if the token is older than 7 days.
        BadSignature:     if the token is tampered with or otherwise invalid.
    """
    s = URLSafeTimedSerializer(secret_key)
    data = s.loads(token, salt="team-invite", max_age=max_age)
    return data["email"]
