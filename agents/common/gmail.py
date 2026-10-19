"""Read-only Gmail access.

The only scope ever requested is gmail.readonly. A stored token with any other
scope is refused, never used. OAuth client: secrets/credentials.json (a Desktop
app client from Google Cloud). Token: secrets/token.json, written after the
first consent in the browser.

    python -m agents.common.gmail auth    # one-time browser consent
    python -m agents.common.gmail check   # sender, subject, date of the 10 newest JobAlerts
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from contextlib import contextmanager
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = ROOT / "secrets"
CREDENTIALS_PATH = SECRETS_DIR / "credentials.json"
TOKEN_PATH = SECRETS_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PAGE_SIZE = 100
CHECK_QUERY = "label:JobAlerts"
CHECK_COUNT = 10


class GmailAuthError(RuntimeError):
    """Credentials are missing, or the stored token isn't read-only."""


class GmailAuthExpired(GmailAuthError):
    """The saved login expired or was revoked. Only the user can fix it, by running `auth`."""


EXPIRED_HINT = "Run `python -m agents.common.gmail auth` to sign in again."


def get_service(
    credentials_path: Path = CREDENTIALS_PATH,
    token_path: Path = TOKEN_PATH,
    *,
    interactive: bool = True,
) -> Any:
    """An authorized Gmail API client. Opens a browser for consent only when needed and allowed."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_path.exists():
        stored = json.loads(token_path.read_text(encoding="utf-8"))
        if sorted(stored.get("scopes") or []) != SCOPES:
            raise GmailAuthError(
                f"{token_path} has scopes {stored.get('scopes')}, expected only {SCOPES}. "
                "Delete it and run `python -m agents.common.gmail auth` again."
            )
        creds = Credentials.from_authorized_user_info(stored, SCOPES)

    if creds and not creds.valid:
        try:
            if not creds.refresh_token:
                raise RefreshError("token has no refresh token")
            creds.refresh(Request())
            _save_token(creds, token_path)
        except RefreshError as exc:
            # Typically invalid_grant: revoked, or the 7-day limit of an app in Testing mode.
            if not interactive:
                raise GmailAuthExpired(f"Gmail login expired or was revoked ({exc}). {EXPIRED_HINT}") from None
            log.info("Saved Gmail login no longer works (%s); asking for consent again", exc)
            creds = None

    if not creds:
        if not interactive:
            raise GmailAuthError(f"No Gmail token at {token_path}. {EXPIRED_HINT}")
        if not credentials_path.exists():
            raise GmailAuthError(f"OAuth client file not found: {credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        granted = sorted(creds.granted_scopes or creds.scopes or [])
        if granted != SCOPES:
            raise GmailAuthError(f"Google granted {granted}, expected only {SCOPES}; token not saved")
        _save_token(creds, token_path)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _save_token(creds: Any, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


class Mailbox:
    """list_messages/get_message bound to one client, built on first use without a browser."""

    def __init__(self, credentials_path: Path = CREDENTIALS_PATH, token_path: Path = TOKEN_PATH) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = None

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = get_service(self.credentials_path, self.token_path, interactive=False)
        return self._service

    def list_messages(self, query: str) -> list[str]:
        with _expiry_as_error():
            return list_messages(query, service=self.service)

    def get_message(self, message_id: str) -> dict[str, Any]:
        with _expiry_as_error():
            return get_message(message_id, service=self.service)

    def get_headers(self, message_id: str) -> dict[str, Any]:
        with _expiry_as_error():
            return get_headers(message_id, service=self.service)


@contextmanager
def _expiry_as_error() -> Iterator[None]:
    """A login that dies mid-run (refresh fails, or the API answers 401) is GmailAuthExpired."""
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    try:
        yield
    except RefreshError as exc:
        raise GmailAuthExpired(f"Gmail login expired or was revoked ({exc}). {EXPIRED_HINT}") from None
    except HttpError as exc:
        if exc.resp.status == 401:
            raise GmailAuthExpired(f"Gmail rejected the saved login (HTTP 401). {EXPIRED_HINT}") from None
        raise


def list_messages(query: str, *, service: Any = None, limit: int | None = None) -> list[str]:
    """IDs of messages matching a Gmail search query, newest first."""
    service = service or get_service()
    ids: list[str] = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=min(PAGE_SIZE, limit or PAGE_SIZE), pageToken=page_token)
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token or (limit is not None and len(ids) >= limit):
            return ids[:limit] if limit is not None else ids


def get_message(message_id: str, *, service: Any = None) -> dict[str, Any]:
    """One message as {id, sender, sender_name, subject, date, html, text}."""
    service = service or get_service()
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return parse_message(raw)


def get_headers(message_id: str, *, service: Any = None) -> dict[str, Any]:
    """Sender, subject and date only. Fetched as metadata, so the body is never downloaded."""
    service = service or get_service()
    raw = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )
    m = parse_message(raw)
    return {k: m[k] for k in ("id", "sender", "sender_name", "subject", "date")}


def parse_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a Gmail API `format=full` message into the shape get_message returns."""
    payload = raw.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
    sender_name, sender = parseaddr(headers.get("from", ""))
    bodies: dict[str, list[str]] = {"text/html": [], "text/plain": []}
    _collect_bodies(payload, bodies)
    return {
        "id": raw.get("id"),
        "sender": sender.lower(),
        "sender_name": sender_name,
        "subject": headers.get("subject", ""),
        "date": _iso_datetime(headers.get("date")),
        "html": "\n".join(bodies["text/html"]),
        "text": "\n".join(bodies["text/plain"]),
    }


def _collect_bodies(part: dict[str, Any], out: dict[str, list[str]]) -> None:
    mime = (part.get("mimeType") or "").lower()
    if mime in out and not (part.get("filename") or ""):
        data = (part.get("body") or {}).get("data")
        if data:
            out[mime].append(_decode(data, part))
    for child in part.get("parts") or []:
        _collect_bodies(child, out)


def _decode(data: str, part: dict[str, Any]) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset = "utf-8"
    for h in part.get("headers") or []:
        if h["name"].lower() == "content-type" and "charset=" in h["value"].lower():
            charset = h["value"].lower().split("charset=", 1)[1].split(";")[0].strip().strip('"')
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _iso_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        log.warning("Unparseable Date header %r", value)
        return None


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.common.gmail", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="run the one-time browser consent (read-only scope)")
    sub.add_parser("check", help=f"show sender, subject and date of the {CHECK_COUNT} newest {CHECK_QUERY} messages")
    args = parser.parse_args(argv)

    try:
        service = get_service()  # run by hand, so a browser consent is fine
    except GmailAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.command == "auth":
        print(f"Gmail read-only access is set up. Token: {TOKEN_PATH}")
        return 0
    return check(service)


def check(service: Any) -> int:
    """Print sender, subject and date of the newest alert emails. Never prints bodies."""
    ids = list_messages(CHECK_QUERY, service=service, limit=CHECK_COUNT)
    if not ids:
        print(f"No messages match {CHECK_QUERY!r}. Does the label exist, with alerts filtered into it?")
        return 1
    print(f"{len(ids)} newest messages in {CHECK_QUERY}:")
    for mid in ids:
        m = get_headers(mid, service=service)
        print(f"{m['date'] or '?':<25}  {m['sender']:<40}  {m['subject']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
