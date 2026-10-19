"""Refilter: re-apply the hard filters to stored jobs, e.g. after editing profile.yaml.

    python -m agents.scout.refilter [--db PATH] [--profile PATH] [--log-dir DIR] [--dry-run]

  * Jobs in `new` or `scored` that now fail a filter are closed by scout with the
    detail "filtered: <reason>".
  * Closed jobs whose latest close was such a filter rejection, and that now pass,
    go back to `new` (the only closed -> new transition db.set_status allows). One
    is left closed if another open job has the same dedupe_key.
  * Nothing else is touched: shortlisted and later jobs, and jobs closed for any
    other reason (posting removed, rejection, withdrawn), keep their status.
  * Every stored job's dedupe_key is recomputed first, so a change to company or
    title normalization also applies to jobs stored before it.

--dry-run prints what would close or reopen and changes nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents import db
from agents.scout.filters import DEFAULT_PROFILE_PATH, apply_hard_filters, dedupe_key, load_profile

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = ROOT / "logs"

REFILTER_STATUSES = ("new", "scored")


@dataclass
class Change:
    job_id: int
    company: str
    title: str
    status: str
    reason: str


@dataclass
class Result:
    dry_run: bool
    checked: int = 0
    rekeyed: int = 0  # stored dedupe keys recomputed
    closed: list[Change] = field(default_factory=list)
    reopened: list[Change] = field(default_factory=list)
    kept_closed: list[Change] = field(default_factory=list)  # pass now, but an open twin exists
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        would = "would " if self.dry_run else ""
        out = [
            f"Refilter{' (dry run, nothing changed)' if self.dry_run else ''}: "
            f"{self.checked} jobs checked, {len(self.closed)} {would}close, {len(self.reopened)} {would}reopen"
        ]
        if self.rekeyed:
            out.append(f"  {self.rekeyed} stored dedupe keys {'would be ' if self.dry_run else ''}refreshed")
        for label, changes in (("close", self.closed), ("reopen", self.reopened), ("keep closed", self.kept_closed)):
            for ch in changes:
                out.append(f"  {label:<11} #{ch.job_id:<5} {ch.status:<7} {ch.company[:24]:<24}  {ch.title}")
                out.append(f"  {'':<11} {'':<6} {ch.reason}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def refilter(
    profile: dict[str, Any],
    *,
    db_path: Path | str | None = None,
    dry_run: bool = False,
    today: date | None = None,
) -> Result:
    result = Result(dry_run=dry_run)
    if not dry_run:  # a dry run doesn't even apply pending migrations
        db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        for job in db.get_jobs(conn=conn):
            key = dedupe_key(job)
            if key != job["dedupe_key"]:
                result.rekeyed += 1
                if not dry_run:
                    with conn:
                        db.set_dedupe_key(job["id"], key, conn=conn)

        for status in REFILTER_STATUSES:
            for job in db.get_jobs(status, conn=conn):
                result.checked += 1
                passed, reason = apply_hard_filters(job, profile, today=today)
                if passed:
                    continue
                change = Change(job["id"], job["company"], job["title"], status, reason)
                if not dry_run and not _apply(conn, result, change, "closed", f"{db.FILTERED_DETAIL_PREFIX} {reason}"):
                    continue
                result.closed.append(change)

        closed_now = {ch.job_id for ch in result.closed}
        for job in db.get_filtered_closed_jobs(conn=conn):
            if job["id"] in closed_now:
                continue
            result.checked += 1
            passed, reason = apply_hard_filters(job, profile, today=today)
            if not passed:
                continue
            change = Change(job["id"], job["company"], job["title"], "closed", f"was {job['closed_detail']}; now {reason}")
            twin = db.find_open_job_by_dedupe_key(dedupe_key(job), conn=conn)
            if twin and twin["id"] != job["id"]:
                change.reason += f"; job {twin['id']} is the same posting and open"
                result.kept_closed.append(change)
                continue
            if not dry_run and not _apply(conn, result, change, "new", f"refilter: passes ({reason})"):
                continue
            result.reopened.append(change)
    finally:
        conn.close()

    for line in result.lines():
        log.info(line)
    return result


def _apply(conn, result: Result, change: Change, new_status: str, detail: str) -> bool:
    try:
        with conn:
            db.set_status(change.job_id, new_status, "scout", detail, conn=conn)
    except (db.TransitionError, db.ActorNotAllowed, LookupError) as exc:
        log.error("Refilter: job %d -> %s failed: %s", change.job_id, new_status, exc)
        result.errors.append(f"job {change.job_id} -> {new_status}: {exc}")
        return False
    log.info("Refilter: job %d %s -> %s (%s)", change.job_id, change.status, new_status, detail)
    return True


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="python -m agents.scout.refilter", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true", help="print what would close or reopen; change nothing")
    args = parser.parse_args(argv)

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"refilter-{datetime.now().date().isoformat()}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        result = refilter(load_profile(args.profile), db_path=args.db, dry_run=args.dry_run)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        handler.close()

    print("\n".join(result.lines()))
    print(f"Log: {log_path}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
