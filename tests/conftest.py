"""Pytest config — keeps state isolated between tests."""
import pytest


@pytest.fixture(autouse=True)
def _reset_auth():
    """Auth state is module-level — reset before each test for isolation."""
    from nrev_wf_mcp import auth
    auth.reset()
    yield
    auth.reset()
