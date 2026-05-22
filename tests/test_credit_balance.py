"""Tests for v0.2.12 get_credit_balance tool."""
from unittest.mock import patch

from nrev_wf_mcp.server import get_credit_balance


def test_get_credit_balance_returns_int_credits():
    with patch("nrev_wf_mcp.server.api.credit_balance", return_value=1819):
        result = get_credit_balance()
    assert result["credits"] == 1819
    assert "note" in result


def test_get_credit_balance_handles_string_response():
    """The endpoint returns a bare integer body — but if a client happens to
    receive it as a stringified int (some httpx/json edge case), we still
    coerce safely rather than crash."""
    with patch("nrev_wf_mcp.server.api.credit_balance", return_value="42"):
        result = get_credit_balance()
    assert result["credits"] == 42


def test_get_credit_balance_surfaces_api_error_without_crashing():
    """A network or auth blip shouldn't take down the caller's session."""
    with patch(
        "nrev_wf_mcp.server.api.credit_balance",
        side_effect=Exception("network blip"),
    ):
        result = get_credit_balance()
    assert result["credits"] is None
    assert "network blip" in result["error"]
