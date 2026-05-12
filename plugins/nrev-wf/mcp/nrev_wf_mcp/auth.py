"""In-memory JWT store.

The token never touches disk. It's held in module-level state for the lifetime of
the MCP server process and lost on restart — by design (see README).
"""
from __future__ import annotations

import base64
import json
import time
from typing import Optional


_jwt: Optional[str] = None
_decoded_exp: Optional[int] = None


class AuthError(RuntimeError):
    pass


def _decode_exp(token: str) -> Optional[int]:
    """Pull the `exp` claim out of a JWT without verifying the signature.

    We don't verify because we don't have the signing key — and we don't need to.
    The server we're calling will reject expired/invalid tokens; we just want to
    surface "expires in N minutes" to the user as a courtesy.
    """
    try:
        payload_b64 = token.split(".")[1]
        # JWT base64url uses no padding; restore it.
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def set_jwt(token: str) -> dict:
    """Store the JWT. Accepts the bare token or a full `Bearer <token>` header value."""
    global _jwt, _decoded_exp
    token = (token or "").strip()
    if not token:
        raise AuthError("token is empty")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    _jwt = token
    _decoded_exp = _decode_exp(token)
    return status()


def get_jwt() -> str:
    if _jwt is None:
        raise AuthError(
            "JWT not set — call set_jwt(token) first. "
            "Grab a fresh token from app.nrev.ai DevTools."
        )
    return _jwt


def status() -> dict:
    if _jwt is None:
        return {"status": "unset"}
    out: dict = {"status": "set", "last4": _jwt[-4:]}
    if _decoded_exp is not None:
        now = int(time.time())
        out["expires_at_unix"] = _decoded_exp
        out["expires_in_minutes"] = max(0, (_decoded_exp - now) // 60)
        out["expired"] = _decoded_exp < now
    return out


def reset() -> None:
    """For tests."""
    global _jwt, _decoded_exp
    _jwt = None
    _decoded_exp = None
