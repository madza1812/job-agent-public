"""Daily: the whole morning run in one command.

    python -m agents.daily [--db PATH] [--since-days N] [--log-dir DIR] [--out-dir DIR]
                           [--lock PATH] [--no-toast]

Steps, in order: fetch -> track (application emails) -> score (pass 1) -> details -> pass 2 -> shortlist -> research
-> digest, then the review page (review/index.html).
A step that fails (or stops at the Claude usage limit) is logged and reported in the
digest's warnings, and the run goes on with the next step. The digest is always written.

Logs: everything goes to logs/daily-YYYY-MM-DD.log; fetch also to scout-YYYY-MM-DD.log,
the scoring steps (with their usage lines) also to score-YYYY-MM-DD.log, and research
to research-YYYY-MM-DD.log. A job whose brief fails stays shortlisted for the next run.
Exit code 0 when every step succeeded, 1 otherwise (also when another run holds the lock).

Unattended (Task Scheduler):
  * A lock file (data/daily.lock, runlock.py) keeps two runs from overlapping; one left
    untouched for 2 hours is stale and taken over. The run touches it before each step.
  * Claude is found only through claude_path in config/local.yaml (claude.claude_exe). If
    it's missing, the Claude steps (score, details, pass 2, research) fail with one clear
    warning and the rest still runs.
  * Each run is recorded in daily_runs (start, finish, per-step status) for the "Last run"
    line at the top of the digest and the review page.
  * Logs older than 30 days are deleted (runlog.prune_logs).
  * The track step shows a reply toast for new replies (track.reply_toast).
  * A Windows toast says "Job digest ready: N new picks" (N = jobs this run shortlisted),
    or "Daily run had problems — see digest" (toast.py; skipped silently if it fails).
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from agents import db, digest, review
from agents.common import claude, gmail, runlock, runlog, toast
from agents.digest import RunStats
from agents.research import brief
from agents.scout import age, details, email_alerts, fetch, pass2, score, shortlist, waiting
from agents.scout.companies import DEFAULT_PREFERRED_PATH, load_preferred
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile
from agents.tracker import track

log = logging.getLogger(__name__)


STEP_LOGS = {"fetch": "scout", "track": "tracker", "research": "research"}  # the rest log to score-*.log
CLAUDE_STEPS = ("track", "score", "details", "pass2", "research")
TOAST_TITLE = "Job agent"
PROBLEMS = "Daily run had problems — see digest"


class StepWarning(Exception):
    """A step finished but with problems worth a warning (not a crash)."""


def run_daily(
    *,
    db_path: Path | str | None = None,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    preferred_path: Path = DEFAULT_PREFERRED_PATH,
    gmail_token: Path = gmail.TOKEN_PATH,
    since_days: int = email_alerts.DEFAULT_SINCE_DAYS,
    log_dir: Path | None = None,
    out_dir: Path = digest.DEFAULT_OUT_DIR,
    review_out: Path = review.DEFAULT_OUT,
    steps: dict[str, Callable[..., Any]] | None = None,
    claude_check: Callable[[], Any] = claude.claude_exe,
    heartbeat: Callable[[], None] = lambda: None,
    notify: Callable[[str, str], Any] | None = None,
) -> tuple[Path, RunStats, list[str]]:
    """Run every step; returns (digest path, stats, warnings). claude_check raises
    ClaudeNotConfigured when claude.exe can't be found; heartbeat keeps the lock live;
    notify shows the track step's reply toast."""
    stats = RunStats()
    warnings: list[str] = []
    ctx: dict[str, Any] = {}
    db.init_db(db_path)
    run_id = db.start_daily_run(db_path=db_path)
    try:
        claude_check()
        no_claude = None
    except claude.ClaudeNotConfigured as exc:
        no_claude = str(exc)
        log.error("Daily: %s", no_claude)
        warnings.append(f"{', '.join(CLAUDE_STEPS)} not run: {no_claude}")

    def load() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if "profile" not in ctx:
            ctx["profile"] = load_profile(profile_path)
            ctx["preferred"] = load_preferred(preferred_path)
        return ctx["profile"], ctx["preferred"]

    def do_fetch() -> None:
        profile, preferred = load()
        mailbox = gmail.Mailbox(token_path=gmail_token) if gmail_token.exists() else None
        summary = fetch.run(profile, preferred, mailbox=mailbox, since_days=since_days, db_path=db_path)
        stats.emails_read = sum(s.emails_read for s in summary.senders.values())
        stats.jobs_found = summary.fetched
        stats.filtered = sum(summary.rejections.values())
        _raise_if(summary.errors, "source errors")

    def do_track() -> None:
        if not gmail_token.exists():
            raise StepWarning("no Gmail token, so application emails were not read")
        result = track.track(gmail.Mailbox(token_path=gmail_token), db_path=db_path, notify=notify)
        stats.tracked = result.classified
        stats.usage.merge(result.usage)
        _raise_if(result.errors, "errors", result.stopped)

    def do_score() -> None:
        profile, preferred = load()
        result = score.score_jobs(profile, preferred, db_path=db_path)
        stats.scored = result.scored
        stats.usage.merge(result.usage)
        _raise_if(result.errors, "errors", result.stopped)

    def do_details() -> None:
        result = details.fetch_details(db_path=db_path)
        stats.details_found = result.found
        stats.details_official = result.official
        stats.usage.merge(result.usage)
        _raise_if(result.errors, "errors", result.stopped)

    def do_pass2() -> None:
        profile, preferred = load()
        result = pass2.rescore(profile, preferred, db_path=db_path)
        stats.pass2_rescored = result.rescored
        stats.usage.merge(result.usage)
        _raise_if(result.errors, "errors", result.stopped)

    def do_cleanup() -> None:
        profile, _ = load()
        stats.cleaned_up = len(waiting.expire(profile, db_path=db_path))
        stats.cleaned_up += len(age.close_too_old(profile, db_path=db_path))

    def do_shortlist() -> None:
        profile, _ = load()
        result = shortlist.shortlist(profile, db_path=db_path)
        stats.shortlisted = len(result.picked)
        _raise_if(result.errors, "errors")

    def do_research() -> None:
        profile, _ = load()
        result = brief.research(profile=profile, db_path=db_path)
        stats.briefs_written = len(result.written)
        stats.usage.merge(result.usage)
        _raise_if(result.errors, "briefs failed (jobs stay shortlisted)", result.stopped)

    all_steps: dict[str, Callable[[], None]] = {
        "fetch": do_fetch,
        "track": do_track,
        "score": do_score,
        "details": do_details,
        "pass2": do_pass2,
        "cleanup": do_cleanup,
        "shortlist": do_shortlist,
        "research": do_research,
    }
    if steps:  # tests swap in fakes
        all_steps.update(steps)

    for name, step in all_steps.items():
        heartbeat()
        if no_claude and name in CLAUDE_STEPS:
            stats.steps.append((name, "failed"))
            continue
        step_log = runlog.log_path(STEP_LOGS.get(name, "score"), log_dir)
        with runlog.log_to(step_log) if name != "shortlist" else nullcontext():
            log.info("Daily: step %s starting", name)
            try:
                step()
            except StepWarning as exc:
                log.warning("Daily: step %s finished with problems: %s", name, exc)
                stats.steps.append((name, "with problems"))
                warnings.append(f"{name}: {exc}")
            except Exception as exc:  # one failed step must not stop the morning run
                log.exception("Daily: step %s failed", name)
                stats.steps.append((name, "failed"))
                warnings.append(f"{name} failed: {exc}")
            else:
                stats.steps.append((name, "ok"))
        _record(run_id, stats, db_path, finished=False)
    # The steps are done: this is the finish time the "Last run" line shows.
    _record(run_id, stats, db_path, finished=True)

    try:
        try:
            profile = load()[0]
        except Exception:  # an unreadable profile is already a step warning
            profile = {}
        min_score = profile.get("shortlist_min_score", shortlist.DEFAULT_MIN_SCORE)
        text = digest.build(stats=stats, warnings=warnings, min_score=min_score, profile=profile, db_path=db_path)
    except Exception as exc:  # still leave a digest behind that says what broke
        log.exception("Daily: building the digest failed")
        warnings.append(f"digest failed: {exc}")
        text = "# Job digest\n\nBuilding the digest failed:\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
        stats.steps.append(("digest", "failed"))
    else:
        stats.steps.append(("digest", "ok"))
    path = digest.write(text, out_dir=out_dir)
    log.info("Daily: digest written to %s", path)
    try:
        log.info("Daily: review page written to %s", review.write(review.build(db_path=db_path), review_out))
    except Exception as exc:  # the digest is out already; just report it
        log.exception("Daily: writing the review page failed")
        warnings.append(f"review page failed: {exc}")
        stats.steps.append(("review", "failed"))
    else:
        stats.steps.append(("review", "ok"))
    _record(run_id, stats, db_path, finished=False)  # adds digest and review, keeps the finish time
    return path, stats, warnings


def _record(run_id: int, stats: RunStats, db_path: Path | str | None, finished: bool) -> None:
    try:
        db.finish_daily_run(run_id, stats.steps, finished=finished, db_path=db_path)
    except Exception:  # the run history is a nicety; never fail the run over it
        log.exception("Daily: recording the run failed")


def toast_message(stats: RunStats) -> str:
    if all(state == "ok" for _, state in stats.steps):
        return f"Job digest ready: {stats.shortlisted} new pick{'' if stats.shortlisted == 1 else 's'}"
    return PROBLEMS


def _raise_if(errors: list[str], what: str, stopped: str | None = None) -> None:
    problems = []
    if stopped:
        problems.append(stopped)
    if errors:
        shown = "; ".join(errors[:3]) + (f"; and {len(errors) - 3} more" if len(errors) > 3 else "")
        problems.append(f"{len(errors)} {what}: {shown}")
    if problems:
        raise StepWarning(" | ".join(problems))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.daily", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--since-days", type=int, default=email_alerts.DEFAULT_SINCE_DAYS)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--out-dir", type=Path, default=digest.DEFAULT_OUT_DIR)
    parser.add_argument("--lock", type=Path, default=runlock.DEFAULT_LOCK)
    parser.add_argument("--no-toast", action="store_true", help="don't show a Windows notification")
    args = parser.parse_args(argv)
    notify = (lambda title, message: False) if args.no_toast else toast.notify

    with runlog.log_to(runlog.log_path("daily", args.log_dir)) as log_file:
        try:
            with runlock.hold(args.lock) as lock:
                for old in runlog.prune_logs(args.log_dir):
                    log.info("Daily: deleted old log %s", old.name)
                path, stats, warnings = run_daily(db_path=args.db, since_days=args.since_days, log_dir=args.log_dir,
                                                  out_dir=args.out_dir, heartbeat=lock.touch, notify=notify)
        except runlock.LockBusy as exc:
            log.error("Daily: not started: %s", exc)
            print(f"Not started: {exc}")
            return 1
        except Exception as exc:  # unattended: say so in the log and the toast, then fail
            log.exception("Daily: run failed")
            notify(TOAST_TITLE, PROBLEMS)
            print(f"Daily run failed: {exc}")
            print(f"Log: {log_file}")
            return 1
    notify(TOAST_TITLE, toast_message(stats))
    print("Steps: " + ", ".join(f"{n} {s}" for n, s in stats.steps))
    for w in warnings:
        print(f"WARNING: {w}")
    print(f"Digest: {path}")
    print(f"Log: {log_file}")
    return 0 if all(s == "ok" for _, s in stats.steps) else 1


if __name__ == "__main__":
    sys.exit(main())
