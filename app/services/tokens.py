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


def generate_invite_token(secret_key: str, email: str, role: str = "analyst") -> str:
    """Generate a signed, time-limited team invite token (7-day TTL)."""
    s = URLSafeTimedSerializer(secret_key)
    return s.dumps({"email": email, "role": role}, salt="team-invite")


def verify_invite_token(secret_key: str, token: str, max_age: int = 604800) -> dict:
    """Verify an invite token. Returns {"email": ..., "role": ...}.
    Backward-compatible: old tokens without role default to 'analyst'.
    """
    s = URLSafeTimedSerializer(secret_key)
    data = s.loads(token, salt="team-invite", max_age=max_age)
    return {"email": data["email"], "role": data.get("role", "analyst")}
