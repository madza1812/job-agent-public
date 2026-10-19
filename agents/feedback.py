"""Feedback from the user: rate a job up or down, skip it, or reopen it.

    python -m agents.feedback rate <job_id> up|down ["optional note"]
    python -m agents.feedback skip <job_id> "reason"
    python -m agents.feedback reopen <job_id> "reason"
    python -m agents.feedback hold <job_id> "reason" [--until YYYY-MM-DD]
    python -m agents.feedback unhold <job_id>

rate sets jobs.user_rating (1 / -1) and appends {title, company, note} to
profile.yaml feedback.liked_examples or disliked_examples, keeping the last
MAX_EXAMPLES of each. Pass 1 and pass 2 send these examples to Claude. Rating a job
again moves it to the other list instead of adding it twice. Only the feedback block
of profile.yaml is rewritten; comments elsewhere in the file are kept.

skip closes the job as the user, with the detail "skipped: <reason>". reopen takes a close
back: closed -> scored as the user, detail "reopened: <reason>"; the score stays.

hold puts a job on hold (db.set_hold): its status stays, but it's left out of the
shortlist and "Still waiting" until --until arrives (or until unhold, without --until).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from agents import db
from agents.scout.filters import DEFAULT_PROFILE_PATH

MAX_EXAMPLES = 20
LISTS = {"up": "liked_examples", "down": "disliked_examples"}
SKIPPED_PREFIX = "skipped:"
REOPENED_PREFIX = "reopened:"

_FEEDBACK_LINE = re.compile(r"^feedback:\s*(#.*)?$")


def rate(job_id: int, direction: str, note: str = "", *,
         db_path: Path | str | None = None, profile_path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    if direction not in LISTS:
        raise ValueError(f"direction must be 'up' or 'down', not {direction!r}")
    db.init_db(db_path)
    job = db.get_job(job_id, db_path=db_path)
    if job is None:
        raise LookupError(f"no job with id {job_id}")
    entry = {"title": job["title"], "company": job["company"]}
    if note.strip():
        entry["note"] = note.strip()
    add_feedback_example(profile_path, LISTS[direction], entry)
    db.set_user_rating(job_id, 1 if direction == "up" else -1, db_path=db_path)
    return entry


def skip(job_id: int, reason: str, *, db_path: Path | str | None = None) -> str:
    if not reason.strip():
        raise ValueError("a reason is required")
    db.init_db(db_path)
    return db.set_status(job_id, "closed", "user", f"{SKIPPED_PREFIX} {reason.strip()}", db_path=db_path)


def reopen(job_id: int, reason: str, *, db_path: Path | str | None = None) -> str:
    if not reason.strip():
        raise ValueError("a reason is required")
    db.init_db(db_path)
    return db.set_status(job_id, "scored", "user", f"{REOPENED_PREFIX} {reason.strip()}", db_path=db_path)


def hold(job_id: int, reason: str, until: str | None = None, *, db_path: Path | str | None = None) -> None:
    db.init_db(db_path)
    db.set_hold(job_id, reason, until or None, db_path=db_path)


def unhold(job_id: int, *, db_path: Path | str | None = None) -> None:
    db.init_db(db_path)
    db.clear_hold(job_id, db_path=db_path)


def add_feedback_example(profile_path: Path, list_key: str, entry: dict[str, Any]) -> None:
    """Append entry to feedback.<list_key> (dropping the same job from both lists first)
    and keep the last MAX_EXAMPLES. Rewrites only the top-level feedback block."""
    raw = profile_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    data = yaml.safe_load(raw) or {}
    feedback = dict(data.get("feedback") or {})
    same = (entry["title"], entry["company"])
    for key in LISTS.values():
        items = [e for e in (feedback.get(key) or []) if (e.get("title"), e.get("company")) != same]
        feedback[key] = items
    feedback[list_key] = (feedback[list_key] + [entry])[-MAX_EXAMPLES:]

    block = yaml.safe_dump({"feedback": feedback}, sort_keys=False, allow_unicode=True, width=1000).splitlines()
    lines = raw.splitlines()
    start = next((i for i, line in enumerate(lines) if _FEEDBACK_LINE.match(line)), None)
    if start is None:
        lines = lines + [""] + block if lines and lines[-1].strip() else lines + block
    else:
        end = start + 1
        # The block runs until the next line at column 0 (a key or a comment).
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():  # keep separating blank lines
            end -= 1
        lines = lines[:start] + block + lines[end:]
    text = newline.join(lines) + newline

    expected = {**data, "feedback": feedback}
    if yaml.safe_load(text) != expected:  # never write a profile that changed elsewhere
        raise RuntimeError("rewriting the feedback block would change other parts of profile.yaml; not saved")
    profile_path.write_bytes(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.feedback", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    p_rate = sub.add_parser("rate", help="thumbs up or down; teaches the scorer your taste")
    p_rate.add_argument("job_id", type=int)
    p_rate.add_argument("direction", choices=sorted(LISTS))
    p_rate.add_argument("note", nargs="?", default="")
    p_skip = sub.add_parser("skip", help="close a job you won't pursue")
    p_skip.add_argument("job_id", type=int)
    p_skip.add_argument("reason")
    p_reopen = sub.add_parser("reopen", help="take back a close: closed -> scored")
    p_reopen.add_argument("job_id", type=int)
    p_reopen.add_argument("reason")
    p_hold = sub.add_parser("hold", help="put a job on hold: out of the shortlist and Still waiting")
    p_hold.add_argument("job_id", type=int)
    p_hold.add_argument("reason")
    p_hold.add_argument("--until", help="YYYY-MM-DD, the day it comes back (default: until unhold)")
    p_unhold = sub.add_parser("unhold", help="take a job off hold")
    p_unhold.add_argument("job_id", type=int)
    args = parser.parse_args(argv)

    try:
        if args.command == "rate":
            entry = rate(args.job_id, args.direction, args.note, db_path=args.db, profile_path=args.profile)
            print(f"Job {args.job_id} rated {args.direction}: added to feedback.{LISTS[args.direction]} "
                  f"({entry['company']} | {entry['title']})")
        elif args.command == "hold":
            hold(args.job_id, args.reason, args.until, db_path=args.db)
            print(f"Job {args.job_id} on hold until {args.until or 'unheld'}: {args.reason}")
        elif args.command == "unhold":
            unhold(args.job_id, db_path=args.db)
            print(f"Job {args.job_id} is off hold")
        elif args.command == "reopen":
            old = reopen(args.job_id, args.reason, db_path=args.db)
            print(f"Job {args.job_id} reopened ({old} -> scored): {args.reason}")
        else:
            old = skip(args.job_id, args.reason, db_path=args.db)
            print(f"Job {args.job_id} skipped ({old} -> closed): {args.reason}")
    except (ValueError, LookupError, db.TransitionError, db.ActorNotAllowed, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
