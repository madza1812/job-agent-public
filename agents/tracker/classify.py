"""Classify application emails with one `claude -p` call per batch (Sonnet, no tools).

Instructions are in prompts/track_classify.md. The reply is validated here: a category
from CATEGORIES, confidence 0-100, a one-line summary, and optional deadline (a real
YYYY-MM-DD date), next action, company and role. A rating below CONFIDENCE_MIN, or one
that says the email isn't about the matched job, is for the user to review.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from agents.common.claude import extract_json
from agents.scout.score import plain_text

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "track_classify.md"
CATEGORIES = ("confirmation", "assessment", "interview_invite", "scheduling", "rejection", "offer",
              "recruiter_outreach", "other")
CONFIDENCE_MIN = 70
BATCH_SIZE = 5
TEXT_CHARS = 3000
SUMMARY_CHARS = 120
ACTION_CHARS = 100


def message_text(message: dict[str, Any]) -> str:
    """The body as plain text: the text part, else the HTML without tags."""
    text = (message.get("text") or "").strip()
    if not text:
        html = re.sub(r"<(style|script|head)\b.*?</\1>", " ", message.get("html") or "", flags=re.S | re.I)
        text = plain_text(html)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text.replace("\r", ""))).strip()


def payload(n: int, message: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": n,
        "from": f"{message.get('sender_name') or ''} <{message.get('sender') or ''}>".strip(),
        "subject": message.get("subject") or "",
        "date": (message.get("date") or "")[:10],
        "text": message_text(message)[:TEXT_CHARS],
        "job": None,
    }
    if job:
        out["job"] = {"company": job["company"], "title": job["title"], "status": job["status"],
                      "applied": (job.get("applied_at") or "")[:10] or None}
    return out


def build_user_prompt(items: list[dict[str, Any]]) -> str:
    lines = [json.dumps(i, ensure_ascii=False, separators=(",", ":")) for i in items]
    return f"Classify these {len(items)} emails:\n" + "\n".join(lines)


def _short(value: Any, limit: int) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] or None


def valid(item: dict[str, Any]) -> dict[str, Any] | None:
    category = item.get("category")
    confidence = item.get("confidence")
    if category not in CATEGORIES:
        return None
    if not isinstance(confidence, int) or isinstance(confidence, bool) or not 0 <= confidence <= 100:
        return None
    summary = _short(item.get("summary"), SUMMARY_CHARS)
    if not summary:
        return None
    deadline = item.get("deadline")
    try:
        deadline = date.fromisoformat(deadline).isoformat() if isinstance(deadline, str) else None
    except ValueError:
        deadline = None
    about = item.get("about_job")
    return {
        "category": category,
        "confidence": confidence,
        "about_job": about if isinstance(about, bool) else None,
        "summary": summary,
        "deadline": deadline,
        "next_action": _short(item.get("next_action"), ACTION_CHARS),
        "company": _short(item.get("company"), 100),
        "role": _short(item.get("role"), 150),
    }


def parse(text: str, ids: set[int]) -> dict[int, dict[str, Any]]:
    """{id: classification} for valid entries about emails in this batch."""
    out: dict[int, dict[str, Any]] = {}
    for item in extract_json(text, list):
        if not isinstance(item, dict):
            continue
        n = item.get("id")
        if isinstance(n, int) and n in ids and n not in out:
            rating = valid(item)
            if rating:
                out[n] = rating
    return out


def needs_review(c: dict[str, Any], has_job: bool) -> bool:
    return c["confidence"] < CONFIDENCE_MIN or (has_job and c["about_job"] is False)
