import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the internet: real HTTP fails like a dropped connection.
    Tests that need pages pass a fake session instead."""
    def refuse(self, method, url, *args, **kwargs):
        raise requests.ConnectionError(f"network disabled in tests: {method} {url}")
    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture(autouse=True)
def no_gmail(request, monkeypatch):
    """Tests never open the real mailbox (the Gmail client uses httplib2, not requests).
    test_gmail.py tests get_service itself, with its own fakes."""
    from agents.common import gmail

    if request.module.__name__.endswith("test_gmail"):
        return

    def refuse(*args, **kwargs):
        raise RuntimeError("Gmail disabled in tests: pass a fake mailbox")
    monkeypatch.setattr(gmail, "get_service", refuse)
