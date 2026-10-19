"""Typefully API v2: drafts only.

    base https://api.typefully.com, header Authorization: Bearer <TYPEFULLY_API_KEY from .env>

Shapes checked against the live API on 2026-10-18:
  * GET /v2/social-sets -> {results: [{id, username, name, profile_image_url, team}], count,
    limit, offset, next, previous}
  * GET /v2/social-sets/{id}/ -> the set plus platforms.linkedin (null when not connected)
    and publishing_quota {used, remaining, resets_at}. Drafts don't use the quota; publishing
    does (free plan: 10 a month).
  * POST /v2/social-sets/{id}/drafts with {platforms: {linkedin: {enabled: true, posts:
    [{text}]}}} -> 201 {id, status: "draft", private_url, created_at, ...}.

The client never publishes: every request goes through _request(), which refuses a body or
query with publish_at or plan_at, any method but GET and POST, and any POST but draft
creation. A created draft whose status isn't "draft" raises.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
LOCAL_CONFIG = ROOT / "config" / "local.yaml"
API_BASE = "https://api.typefully.com"
CACHE_KEY = "typefully_social_set_id"
FORBIDDEN_KEYS = ("publish_at", "plan_at")
TIMEOUT_S = 30
RETRY_DELAYS_S = (5, 20)
_DRAFTS_PATH = re.compile(r"^/v2/social-sets/\d+/drafts$")


class TypefullyError(RuntimeError):
    pass


class ForbiddenRequest(RuntimeError):
    """A request that could publish, schedule or change something other than a new draft."""


@dataclass
class Quota:
    used: int
    remaining: int
    resets_at: str


@dataclass
class Draft:
    id: int
    status: str
    private_url: str
    created_at: str


def api_key(env_path: Path = ENV_PATH) -> str:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise TypefullyError(f"no {env_path.name}; add TYPEFULLY_API_KEY=... to it") from None
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == "TYPEFULLY_API_KEY" and value.strip().strip("'\""):
            return value.strip().strip("'\"")
    raise TypefullyError(f"{env_path.name} has no TYPEFULLY_API_KEY")


def refuse_publishing(params: Any, body: Any) -> None:
    """Raise if publish_at or plan_at appears anywhere in the query or the JSON body."""
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in FORBIDDEN_KEYS:
                    raise ForbiddenRequest(f"{k} is never sent: drafts only, publishing is manual")
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
    walk(params)
    walk(body)


class Typefully:
    def __init__(self, key: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {key}"
        self.sleep = sleep

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        refuse_publishing(params, body)
        if method == "POST" and not _DRAFTS_PATH.match(path):
            raise ForbiddenRequest(f"POST {path}: only draft creation is allowed")
        if method not in ("GET", "POST"):
            raise ForbiddenRequest(f"{method} {path}: only GET and draft creation are allowed")
        for attempt, delay in enumerate((*RETRY_DELAYS_S, None), 1):
            try:
                resp = self.session.request(method, API_BASE + path, params=params, json=body, timeout=TIMEOUT_S)
            except requests.RequestException as exc:
                if delay is None or method == "POST":  # a POST may have gone through: never resend it
                    raise TypefullyError(f"{method} {path}: {exc}") from exc
                log.warning("Typefully %s %s attempt %d failed (%s); retrying in %ds", method, path, attempt, exc, delay)
                self.sleep(delay)
                continue
            if resp.status_code == 429 or (resp.status_code >= 500 and method == "GET"):
                if delay is not None:
                    log.warning("Typefully %s %s answered %d; retrying in %ds", method, path, resp.status_code, delay)
                    self.sleep(delay)
                    continue
            if resp.status_code >= 400:
                raise TypefullyError(f"{method} {path}: HTTP {resp.status_code}: {_error_message(resp)}")
            return resp.json()
        raise AssertionError("unreachable")

    def social_sets(self) -> list[dict]:
        return list(self._request("GET", "/v2/social-sets", params={"limit": 50}).get("results") or [])

    def social_set(self, social_set_id: int) -> dict:
        return self._request("GET", f"/v2/social-sets/{int(social_set_id)}/")

    def quota(self, social_set_id: int) -> Quota:
        q = self.social_set(social_set_id).get("publishing_quota") or {}
        return Quota(used=int(q.get("used") or 0), remaining=int(q.get("remaining") or 0),
                     resets_at=str(q.get("resets_at") or ""))

    def create_draft(self, social_set_id: int, text: str) -> Draft:
        body = {"platforms": {"linkedin": {"enabled": True, "posts": [{"text": text}]}}}
        data = self._request("POST", f"/v2/social-sets/{int(social_set_id)}/drafts", body=body)
        draft = Draft(id=int(data["id"]), status=str(data.get("status")), private_url=str(data.get("private_url") or ""),
                      created_at=str(data.get("created_at") or ""))
        if draft.status != "draft":
            raise TypefullyError(f"draft {draft.id} came back with status {draft.status!r}, not 'draft'; check Typefully now")
        return draft


def _error_message(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{err.get('code', '')} {err.get('message', '')}".strip() or resp.text[:200]
    except (ValueError, AttributeError):
        return resp.text[:200]


def social_set_id(client: Typefully, config_path: Path = LOCAL_CONFIG) -> int:
    """The LinkedIn social set's ID, cached in config/local.yaml as typefully_social_set_id."""
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError:
        config = {}
    cached = config.get(CACHE_KEY) if isinstance(config, dict) else None
    if cached:
        return int(cached)
    sets = client.social_sets()
    with_linkedin = [s for s in sets if (client.social_set(s["id"]).get("platforms") or {}).get("linkedin")]
    if len(with_linkedin) != 1:
        names = ", ".join(f"{s['id']} ({s.get('name')})" for s in with_linkedin) or "none"
        raise TypefullyError(f"expected one social set with LinkedIn connected, found {names}; "
                             f"set {CACHE_KEY} in {config_path.name}")
    found = int(with_linkedin[0]["id"])
    _append_setting(config_path, CACHE_KEY, found)
    log.info("Typefully social set %d cached in %s", found, config_path.name)
    return found


def _append_setting(config_path: Path, key: str, value: int) -> None:
    """Add `key: value` to the YAML file, keeping everything already in it."""
    raw = config_path.read_bytes().decode("utf-8") if config_path.exists() else ""
    newline = "\r\n" if "\r\n" in raw else "\n"
    if raw and not raw.endswith(("\n", "\r\n")):
        raw += newline
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes((raw + f"{key}: {int(value)}{newline}").encode("utf-8"))
