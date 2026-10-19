"""What the digest and review page show about applications.

  * digest_lines(): the "Application updates" section. Offers first, flagged; then new
    events (messages processed today), needs review, "Did you apply?", recruiter
    outreach (the last LIST_DAYS days) and staleness.
  * Staleness: an applied job with no reply (anything but a confirmation or "other")
    for FOLLOW_UP_DAYS (14) since applying or the last reply -> "Consider a follow-up";
    an interviewing job with no status change or email for CHECK_IN_DAYS (7) -> "Check in".
  * timeline(): one application's events and emails, oldest first, for the review page.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents import db

FOLLOW_UP_DAYS = 14
CHECK_IN_DAYS = 7
LIST_DAYS = 14
NOT_A_REPLY = ("confirmation", "other")
GMAIL_LINK = "https://mail.google.com/mail/u/0/#all/{}"
LABELS = {"confirmation": "confirmation", "assessment": "assessment", "interview_invite": "interview invite",
          "scheduling": "scheduling", "rejection": "rejection", "offer": "offer",
          "recruiter_outreach": "recruiter outreach", "other": "other"}


def _dt(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso).astimezone(timezone.utc) if iso else None


def _day(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().date().isoformat() if iso else "date unknown"


def stale(applications: list[dict[str, Any]], messages: list[dict[str, Any]],
          now: datetime | None = None, db_path: Path | str | None = None) -> list[tuple[str, dict[str, Any], int]]:
    """[(kind, job, days)] with kind "follow_up" or "check_in"."""
    now = now or datetime.now(timezone.utc)
    by_job: dict[int, list[dict[str, Any]]] = {}
    for m in messages:
        if m["job_id"] is not None:
            by_job.setdefault(m["job_id"], []).append(m)
    out = []
    for job in applications:
        mails = by_job.get(job["id"], [])
        if job["status"] == "applied":
            times = [_dt(job.get("applied_at"))] + [_dt(m["msg_date"]) for m in mails if m["category"] not in NOT_A_REPLY]
            kind, limit = "follow_up", FOLLOW_UP_DAYS
        elif job["status"] == "interviewing":
            entered = [_dt(e["at"]) for e in db.get_job_events(job["id"], db_path=db_path)
                       if e["event"] == "status_change" and e["to_status"] == "interviewing"]
            times = entered + [_dt(m["msg_date"]) for m in mails]
            kind, limit = "check_in", CHECK_IN_DAYS
        else:
            continue
        times = [t for t in times if t]
        if times and (days := (now - max(times)).days) >= limit:
            out.append((kind, job, days))
    return out


def digest_lines(db_path: Path | str | None = None, today: date | None = None,
                 now: datetime | None = None) -> list[str]:
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    messages = db.get_tracker_messages(db_path=db_path)
    applications = db.get_applications(db_path=db_path)
    jobs = {j["id"]: j for j in db.get_jobs(db_path=db_path)}
    out = ["## Application updates", ""]
    if not applications and not messages:
        return out + ["Nothing to track yet: no job is marked applied.", ""]

    recent_cut = now - timedelta(days=LIST_DAYS)
    recent = [m for m in messages if (_dt(m["processed_at"]) or now) >= recent_cut]
    today_msgs = [m for m in messages if _dt(m["processed_at"]).astimezone().date() == today
                  and m["outcome"] in ("event", "status_change")]

    def who(m: dict[str, Any]) -> str:
        job = jobs.get(m["job_id"]) if m["job_id"] else None
        if job:
            return f"#{job['id']} **{job['company']}**: {job['title']}"
        return f"**{m['company'] or m['sender_domain']}**" + (f": {m['role']}" if m["role"] else "")

    def extras(m: dict[str, Any]) -> str:
        parts = [f"next: {m['next_action']}"] if m["next_action"] else []
        if m["deadline"]:
            parts.append(f"deadline **{m['deadline']}**")
        return f" ({'; '.join(parts)})" if parts else ""

    lines: list[str] = []
    for m in (m for m in recent if m["outcome"] == "offer"):
        lines.append(f"- 🎉 **OFFER** {_day(m['msg_date'])} {who(m)}: {m['summary']}{extras(m)}. "
                     f"No status was changed; reply yourself. {GMAIL_LINK.format(m['message_id'])}")
    if today_msgs:
        lines += ["", "**New today**", ""] if lines else ["**New today**", ""]
        for m in today_msgs:
            change = ""
            if m["outcome"] == "status_change":
                job = jobs.get(m["job_id"])
                change = f" → now **{job['status']}**" if job else ""
            lines.append(f"- {_day(m['msg_date'])} {who(m)}, {LABELS[m['category']]}: {m['summary']}{extras(m)}{change}")
    sections = (
        ("Needs review", "needs_review", lambda m: f"- {_day(m['msg_date'])} {who(m)} from {m['sender_domain']}: "
                                                   f"{m['summary']} ({LABELS[m['category']]}?, confidence "
                                                   f"{m['confidence']}%; no status changed). "
                                                   f"{GMAIL_LINK.format(m['message_id'])}"),
        ("Did you apply?", "did_you_apply", lambda m: _did_you_apply(m, jobs, who)),
        ("Recruiter outreach", "outreach", lambda m: f"- {_day(m['msg_date'])} {who(m)} from {m['sender_domain']}: "
                                                     f"{m['summary']}{extras(m)}. {GMAIL_LINK.format(m['message_id'])}"),
    )
    for title, outcome, fmt in sections:
        items = [m for m in recent if m["outcome"] == outcome]
        if outcome == "did_you_apply":  # resolved once the job is marked applied
            items = [m for m in items if jobs.get(m["job_id"], {}).get("status") not in ("applied", "interviewing")]
        if items:
            lines += ["", f"**{title}**", ""] if lines else [f"**{title}**", ""]
            lines += [fmt(m) for m in items]
    for kind, title, hint in (("follow_up", "Consider a follow-up", "no reply for"),
                              ("check_in", "Check in", "no update for")):
        items = [(job, days) for k, job, days in stale(applications, messages, now, db_path) if k == kind]
        if items:
            lines += ["", f"**{title}**", ""] if lines else [f"**{title}**", ""]
            lines += [f"- #{job['id']} **{job['company']}**: {job['title']}, {hint} {days} days" for job, days in items]
    if not lines:
        active = sum(1 for a in applications if a["status"] in ("applied", "interviewing"))
        lines = [f"No new application emails. {active} application{'s' if active != 1 else ''} in progress."]
    return out + lines + [""]


def _did_you_apply(m: dict[str, Any], jobs: dict[int, dict[str, Any]], who) -> str:
    job = jobs.get(m["job_id"])
    status = job["status"] if job else "unknown"
    if job and status in (*db.USER_APPLY_FROM, "closed"):  # closed -> applied: allowed by this item
        how = f"mark it with `.venv\\Scripts\\python.exe -m agents.db mark-applied {job['id']} --method ats`"
    else:
        how = (f"the job is {status}, so it can't be marked applied yet; "
               f"`.\\run.ps1 skip {job['id']} \"applied directly\"` then mark-applied works" if job
               else "it isn't in the database")
    return (f"- {_day(m['msg_date'])} {who(m)}: {LABELS[m['category']]} email ({m['summary']}), "
            f"but the job isn't marked applied. If you applied, {how}.")


def timeline(job: dict[str, Any], db_path: Path | str | None = None) -> list[dict[str, str]]:
    """[{at, kind, text}] oldest first: status changes and application emails."""
    items = []
    for e in db.get_job_events(job["id"], db_path=db_path):
        if e["event"] == "status_change" and e["to_status"] in ("applied", "interviewing", "closed"):
            text = f"{e['from_status']} → {e['to_status']} ({e['actor']}{', ' + e['detail'] if e['detail'] else ''})"
            items.append({"at": e["at"], "kind": "status", "text": text})
    for m in db.get_tracker_messages(job_id=job["id"], db_path=db_path):
        if m["outcome"] == "ignored":
            continue
        text = f"{LABELS.get(m['category'] or '', 'email')} from {m['sender_domain']}: {m['summary'] or ''}"
        if m["next_action"]:
            text += f" Next: {m['next_action']}."
        if m["deadline"]:
            text += f" Deadline {m['deadline']}."
        if m["outcome"] == "needs_review":
            text += f" (needs review, confidence {m['confidence']}%)"
        items.append({"at": m["msg_date"] or m["processed_at"], "kind": m["outcome"], "text": text})
    return sorted(items, key=lambda i: _dt(i["at"]) or datetime.min.replace(tzinfo=timezone.utc))
