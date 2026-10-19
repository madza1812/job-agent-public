"""Save real job alert emails as sanitized test fixtures.

    python -m tools.sanitize_email "label:JobAlerts from:jobs-noreply@linkedin.com" linkedin_jobs --limit 2

Reads the matching messages (read-only Gmail, see agents.common.gmail), sanitizes
them in memory and writes tests/fixtures/email_<name>_<n>.json in the shape
gmail.get_message returns. Raw emails never touch the disk.

Sanitizing:
  * Every URL keeps scheme, host and path. Query values become "x" (except job
    IDs: Indeed's jk, Glassdoor's jobListingId), and long token-like path segments
    become "REDACTED". In HTML, href and src values are cleaned whole, up to the
    closing quote, because some senders leave raw spaces inside links.
  * Indeed cts.indeed.com redirects are rebuilt with only the cleaned target URL,
    dropping the accountId and tracking data inside the token.
  * The Gmail account's address, identity.name from profile/profile.yaml (and its
    first word, in greetings like "Hi <first>,"), and every pair in the profile's
    `sanitize.replace` map are replaced. The profile is gitignored, so personal
    details never land in this file:

        sanitize:
          replace:
            "Jane Doe": "Test User"
            "Springfield, CA 00000": "Somewhere, CA 00000"

Afterwards every original term is searched for again; any leftover aborts the
write, and so does any URL query value that isn't "x" or a kept job ID. Review
the fixtures before committing anyway.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import html
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "profile" / "profile.yaml"
FIXTURES = ROOT / "tests" / "fixtures"
NOTE = "Real alert email, sanitized: personal details replaced, tracking tokens redacted."

KEEP_PARAMS = {"jk", "jobListingId"}
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_URL_ATTR_RE = re.compile(r"""(\b(?:href|src)\s*=\s*)(["'])(https?://.*?)\2""", re.I | re.S)
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_~=%.-]{32,}$")


class LeakError(RuntimeError):
    """A personal term survived sanitizing."""


def load_replacements(profile_path: Path = DEFAULT_PROFILE, account_email: str | None = None) -> dict[str, str]:
    """Literal text -> placeholder, longest first so "Jane Doe" wins over "Jane"."""
    profile: dict[str, Any] = {}
    if profile_path.exists():
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    pairs: dict[str, str] = {}
    name = ((profile.get("identity") or {}).get("name") or "").strip()
    if name:
        pairs[name] = "Test User"
        pairs[f"Hi {name.split()[0]}"] = "Hi Test"
    if account_email:
        pairs[account_email] = "user@example.com"
    for old, new in ((profile.get("sanitize") or {}).get("replace") or {}).items():
        pairs[str(old)] = str(new)
    return dict(sorted(pairs.items(), key=lambda kv: -len(kv[0])))


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.netloc == "cts.indeed.com" and parts.path.startswith("/v3/"):
        segments = parts.path.split("/")
        try:
            inner = json.loads(gzip.decompress(_b64decode(segments[2])))["u"]
        except Exception:
            inner = "https://www.indeed.com/"
        payload = json.dumps({"u": clean_url(inner)}).encode("utf-8")
        token = base64.urlsafe_b64encode(gzip.compress(payload, mtime=0)).decode("ascii").rstrip("=")
        return f"https://cts.indeed.com/v3/{token}/sig"
    query = [(k, v if k in KEEP_PARAMS else "x") for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    path = "/".join("REDACTED" if _TOKEN_SEGMENT.match(s) else s for s in parts.path.split("/"))
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def sanitize_text(text: str, replacements: dict[str, str], *, is_html: bool) -> str:
    def sub_url(raw: str) -> str:
        cleaned = clean_url(html.unescape(raw) if is_html else raw)
        return html.escape(cleaned, quote=True) if is_html else cleaned

    def sub(match: re.Match[str]) -> str:
        return sub_url(match.group(0))

    if is_html:
        text = _URL_ATTR_RE.sub(lambda m: m.group(1) + m.group(2) + sub_url(m.group(3)) + m.group(2), text)
    text = _URL_RE.sub(sub, text)
    for old, new in replacements.items():
        text = text.replace(old, new)
        if is_html:
            text = text.replace(html.escape(old, quote=True), html.escape(new, quote=True))
    return text


def sanitize_message(message: dict[str, Any], name: str, replacements: dict[str, str]) -> dict[str, Any]:
    out = {
        "_fixture_note": NOTE,
        "id": f"fixture-{name}",
        "sender": message["sender"],
        "sender_name": message.get("sender_name", ""),
        "subject": sanitize_text(message.get("subject") or "", replacements, is_html=False),
        "date": message.get("date"),
        "html": sanitize_text(message.get("html") or "", replacements, is_html=True),
        "text": sanitize_text(message.get("text") or "", replacements, is_html=False),
    }
    check_no_leaks(out, replacements)
    return out


def check_no_leaks(fixture: dict[str, Any], replacements: dict[str, str]) -> None:
    blob = json.dumps(fixture, ensure_ascii=False)
    # Name what leaked, never the personal value itself.
    problems = []
    terms = sum(1 for old in replacements if old in blob or html.escape(old) in blob)
    if terms:
        problems.append(f"{terms} personal term(s)")
    for token in re.findall(r"cts\.indeed\.com/v3/([A-Za-z0-9_-]+)/", blob):
        if set(json.loads(gzip.decompress(_b64decode(token)))) != {"u"}:
            problems.append("data inside an Indeed redirect token")
            break
    urls = [html.unescape(m.group(3)) for m in _URL_ATTR_RE.finditer(fixture.get("html") or "")]
    urls += _URL_RE.findall(html.unescape(fixture.get("html") or "")) + _URL_RE.findall(fixture.get("text") or "")
    leaky = {
        k for url in urls
        for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)
        if v != "x" and k not in KEEP_PARAMS
    }
    if leaky:
        problems.append(f"unredacted URL parameters ({', '.join(sorted(leaky))})")
    if problems:
        raise LeakError(f"{fixture['id']}: still contains {' and '.join(problems)}; not written")


def _b64decode(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def main(argv: list[str] | None = None) -> int:
    from agents.common import gmail

    parser = argparse.ArgumentParser(prog="python -m tools.sanitize_email", description=__doc__.split("\n")[0])
    parser.add_argument("query", help='Gmail search, e.g. "label:JobAlerts from:jobs-noreply@linkedin.com"')
    parser.add_argument("name", help="fixture name; files are email_<name>_<n>.json")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--out", type=Path, default=FIXTURES)
    args = parser.parse_args(argv)

    service = gmail.get_service()
    account = service.users().getProfile(userId="me").execute().get("emailAddress")
    replacements = load_replacements(args.profile, account)

    ids = gmail.list_messages(args.query, service=service, limit=args.limit)
    if not ids:
        print(f"No messages match {args.query!r}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for n, message_id in enumerate(ids, start=1):
        name = f"{args.name}_{n}"
        try:
            fixture = sanitize_message(gmail.get_message(message_id, service=service), name, replacements)
        except LeakError as exc:
            print(f"Error: {exc}. Add the term to sanitize.replace in {args.profile}.", file=sys.stderr)
            return 1
        path = args.out / f"email_{name}.json"
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print("Review the files before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
