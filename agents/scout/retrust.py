"""Retrust: re-derive source_trust for stored postings, e.g. after editing config/source_trust.yaml.

    python -m agents.scout.retrust [--db PATH] [--log-dir DIR] [--dry-run]

  * Every job that isn't closed and has a found posting (source_trust official or
    aggregator) is classified again from both its links (source_trust.classify_posting):
    official when source_url or apply_url is on the company's own domain or an approved
    job site.
  * An official job whose apply_url isn't official gets the official link as apply_url.
  * A newly official job caches the company's board. Salary and still_open aren't
    revisited; they came from the page that was read.
  * Postings the user gave (set-posting) stay official.
  * Each change logs a trust_changed event (actor scout). A job that drops to aggregator
    while shortlisted or researched is moved back by the next shortlist run.

--dry-run prints what would change and writes nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agents import db
from agents.common import runlog
from agents.scout import posting, source_trust

log = logging.getLogger(__name__)


@dataclass
class Change:
    job_id: int
    status: str
    company: str
    title: str
    old_trust: str
    new_trust: str
    old_apply: str | None
    new_apply: str | None


@dataclass
class Result:
    dry_run: bool
    checked: int = 0
    changes: list[Change] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        would = "would change" if self.dry_run else "changed"
        out = [f"Retrust{' (dry run, nothing changed)' if self.dry_run else ''}: "
               f"{self.checked} jobs checked, {len(self.changes)} {would}"]
        for ch in self.changes:
            out.append(f"  #{ch.job_id:<5} {ch.status:<11} {ch.company[:24]:<24}  {ch.title}")
            if ch.old_trust != ch.new_trust:
                out.append(f"  {'':<6} {'':<11} trust: {ch.old_trust} -> {ch.new_trust}")
            if ch.old_apply != ch.new_apply:
                out.append(f"  {'':<6} {'':<11} apply_url: {ch.old_apply or 'none'} -> {ch.new_apply}")
        if self.errors:
            out.append("Errors:")
            out += [f"  {e}" for e in self.errors]
        return out


def retrust(*, db_path: Path | str | None = None, dry_run: bool = False) -> Result:
    result = Result(dry_run=dry_run)
    if not dry_run:
        db.init_db(db_path)
    config = source_trust.load_config()
    conn = db.connect(db_path)
    try:
        user_set = {r[0] for r in conn.execute("SELECT DISTINCT job_id FROM events WHERE event = 'posting_set'")}
        for job in db.get_jobs(conn=conn):
            if job["status"] == "closed" or job.get("source_trust") not in (source_trust.OFFICIAL, source_trust.AGGREGATOR):
                continue
            result.checked += 1
            if job["id"] in user_set:
                continue
            trust, official_url = source_trust.classify_posting(job.get("source_url"), job.get("apply_url"),
                                                                job["company"], config)
            apply_url = job.get("apply_url")
            if official_url and source_trust.classify(apply_url, job["company"], config) != source_trust.OFFICIAL:
                apply_url = official_url
            if trust == job["source_trust"] and apply_url == job.get("apply_url"):
                continue
            change = Change(job["id"], job["status"], job["company"], job["title"],
                            job["source_trust"], trust, job.get("apply_url"), apply_url)
            if not dry_run:
                detail = f"{change.old_trust} -> {trust}" + (f" ({official_url})" if official_url else "")
                try:
                    with conn:
                        db.set_source_trust(job["id"], trust, apply_url, detail, conn=conn)
                        if official_url and change.old_trust != source_trust.OFFICIAL:
                            posting.cache_board(job, official_url, "lookup", conn=conn)
                except LookupError as exc:
                    log.error("Retrust: job %d: %s", job["id"], exc)
                    result.errors.append(f"job {job['id']}: {exc}")
                    continue
            result.changes.append(change)
    finally:
        conn.close()
    for line in result.lines():
        log.info(line)
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.scout.retrust", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    args = parser.parse_args(argv)

    with runlog.log_to(runlog.log_path("scout", args.log_dir)) as log_file:
        result = retrust(db_path=args.db, dry_run=args.dry_run)
    print("\n".join(result.lines()))
    print(f"Log: {log_file}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
