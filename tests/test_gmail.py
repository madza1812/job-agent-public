import base64
import json

import pytest

from agents.common import gmail


def b64(text, encoding="utf-8"):
    return base64.urlsafe_b64encode(text.encode(encoding)).decode("ascii").rstrip("=")


RAW = {
    "id": "18f0a1",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "From", "value": '"LinkedIn Job Alerts" <JobAlerts-noreply@LinkedIn.com>'},
            {"name": "Subject", "value": "30+ new jobs for data analyst"},
            {"name": "Date", "value": "Tue, 13 Oct 2026 21:05:00 -0700"},
        ],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("Data Analyst – Acme")}},
                    {
                        "mimeType": "text/html",
                        "headers": [{"name": "Content-Type", "value": 'text/html; charset="iso-8859-1"'}],
                        "body": {"data": b64("<p>Café</p>", "iso-8859-1")},
                    },
                ],
            },
            {"mimeType": "text/html", "filename": "attached.html", "body": {"attachmentId": "x"}},
        ],
    },
}


def test_parse_message_headers_and_bodies():
    m = gmail.parse_message(RAW)
    assert m["id"] == "18f0a1"
    assert m["sender"] == "jobalerts-noreply@linkedin.com"
    assert m["sender_name"] == "LinkedIn Job Alerts"
    assert m["subject"] == "30+ new jobs for data analyst"
    assert m["date"] == "2026-10-13T21:05:00-07:00"
    assert m["text"] == "Data Analyst – Acme"
    assert m["html"] == "<p>Café</p>"  # decoded with the part's charset; attachment ignored


def test_parse_message_single_part_and_missing_headers():
    raw = {"id": "1", "payload": {"mimeType": "text/plain", "headers": [], "body": {"data": b64("hi")}}}
    m = gmail.parse_message(raw)
    assert (m["sender"], m["subject"], m["date"], m["text"], m["html"]) == ("", "", None, "hi", "")


def test_only_readonly_scope_is_requested():
    assert gmail.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_token_with_broader_scope_is_refused(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"],
    }), encoding="utf-8")
    with pytest.raises(gmail.GmailAuthError, match="expected only"):
        gmail.get_service(tmp_path / "credentials.json", token, interactive=False)


def test_missing_token_never_opens_a_browser_when_not_interactive(tmp_path):
    with pytest.raises(gmail.GmailAuthError, match="gmail auth"):
        gmail.get_service(tmp_path / "credentials.json", tmp_path / "token.json", interactive=False)


class FakeService:
    """Mimics service.users().messages().list(...).execute() with two pages."""

    def __init__(self):
        self.calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        self._kwargs = kwargs
        return self

    def execute(self):
        if self._kwargs.get("pageToken") is None:
            return {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}
        return {"messages": [{"id": "c"}]}


def test_list_messages_follows_pages():
    service = FakeService()
    assert gmail.list_messages("label:JobAlerts", service=service) == ["a", "b", "c"]
    assert [c["q"] for c in service.calls] == ["label:JobAlerts"] * 2
    assert {c["userId"] for c in service.calls} == {"me"}


class FakeCheckService:
    """list() returns 2 ids; get() returns headers, or a body if one were (wrongly) requested."""

    def __init__(self, ids=("a", "b")):
        self.ids = list(ids)
        self.gets = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        self._result = {"messages": [{"id": i} for i in self.ids]}
        return self

    def get(self, **kwargs):
        self.gets.append(kwargs)
        payload = {"headers": [
            {"name": "From", "value": f"Alerts <alerts-{kwargs['id']}@example.com>"},
            {"name": "Subject", "value": f"New jobs {kwargs['id']}"},
            {"name": "Date", "value": "Wed, 14 Oct 2026 08:00:00 +0000"},
        ]}
        if kwargs.get("format") != "metadata":
            payload["mimeType"] = "text/plain"
            payload["body"] = {"data": b64("SECRET BODY")}
        self._result = {"id": kwargs["id"], "payload": payload}
        return self

    def execute(self):
        return self._result


def test_check_prints_headers_only(capsys):
    service = FakeCheckService()
    assert gmail.check(service) == 0
    assert service.list_kwargs["q"] == "label:JobAlerts"
    assert service.list_kwargs["maxResults"] == 10
    assert {g["format"] for g in service.gets} == {"metadata"}
    out = capsys.readouterr().out
    assert "alerts-a@example.com" in out and "New jobs b" in out and "2026-10-14T08:00:00+00:00" in out
    assert "SECRET BODY" not in out


def test_check_with_empty_label(capsys):
    assert gmail.check(FakeCheckService(ids=())) == 1
    assert "Does the label exist" in capsys.readouterr().out


def expired_token(tmp_path, refresh_token="r"):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "t", "refresh_token": refresh_token, "client_id": "c", "client_secret": "s",
        "scopes": gmail.SCOPES, "expiry": "2020-01-01T00:00:00Z",
    }), encoding="utf-8")
    return token


def test_expired_login_is_reported_not_reauthorized(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refuse)
    with pytest.raises(gmail.GmailAuthExpired, match="gmail auth"):
        gmail.get_service(tmp_path / "credentials.json", expired_token(tmp_path), interactive=False)


def test_expired_login_without_refresh_token(tmp_path):
    with pytest.raises(gmail.GmailAuthExpired):
        gmail.get_service(tmp_path / "credentials.json", expired_token(tmp_path, refresh_token=None), interactive=False)


def test_mailbox_turns_mid_run_401_into_expired():
    from googleapiclient.errors import HttpError

    class Resp(dict):
        status = 401
        reason = "Unauthorized"

    class Service(FakeService):
        def execute(self):
            raise HttpError(Resp(), b"{}")

    box = gmail.Mailbox()
    box._service = Service()
    with pytest.raises(gmail.GmailAuthExpired, match="HTTP 401"):
        box.list_messages("label:JobAlerts")
