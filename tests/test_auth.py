"""Tests for in-memory JWT store."""
import base64
import json

import pytest

from nrev_wf_mcp import auth


def _fake_jwt(exp: int) -> str:
    """Build a syntactically-valid JWT (no real signature) with the given exp."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def test_status_unset_initially():
    assert auth.status() == {"status": "unset"}


def test_set_jwt_returns_status():
    token = _fake_jwt(9999999999)
    s = auth.set_jwt(token)
    assert s["status"] == "set"
    assert s["last4"] == token[-4:]
    assert s["expired"] is False


def test_strips_bearer_prefix():
    token = _fake_jwt(9999999999)
    auth.set_jwt(f"Bearer {token}")
    assert auth.get_jwt() == token


def test_strips_whitespace_and_newlines():
    token = _fake_jwt(9999999999)
    auth.set_jwt(f"  {token}\n")
    assert auth.get_jwt() == token


def test_get_jwt_raises_when_unset():
    with pytest.raises(auth.AuthError):
        auth.get_jwt()


def test_empty_token_raises():
    with pytest.raises(auth.AuthError):
        auth.set_jwt("")


def test_expired_jwt_flagged():
    s = auth.set_jwt(_fake_jwt(1))  # exp = epoch second 1, way in the past
    assert s["expired"] is True
    assert s["expires_in_minutes"] == 0


def test_jwt_without_exp_claim():
    """A JWT with no `exp` — we still accept it and just don't surface expiry info."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).rstrip(b"=").decode()
    token = f"{header}.{payload}.sig"
    s = auth.set_jwt(token)
    assert s["status"] == "set"
    assert "expires_in_minutes" not in s


def test_garbage_token_still_stored():
    """If we can't parse exp, we still store the token — the API will reject if invalid."""
    s = auth.set_jwt("not-a-real-jwt")
    assert s["status"] == "set"
    assert "expires_in_minutes" not in s
