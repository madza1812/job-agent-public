"""Digest: the morning summary, written to digest/YYYY-MM-DD.md.

    python -m agents.digest [--db PATH] [--out-dir DIR]

A "Last run" line under the title gives today's latest daily run (start, finish, each
step's status), or "No run today yet". Sections, in order: warnings (active notices such as an expired Gmail login, failed
steps, source errors), application updates (agents/tracker/report.py), today's top 3 (jobs shortlisted today), worth checking
yourself (unverified jobs, with no full posting found, that would have made the top 3;
shown with their alert link), recruiter pools (talent pools and general applications,
one line each, only when there are any), still waiting (jobs shortlisted on earlier days that are
still shortlisted or researched), runners-up 4-10 (the best remaining scored jobs), run
stats, and a command cheat sheet.

Apply links carry their source_trust: an official posting's link is shown as is, an
aggregator's is labeled "aggregator, verify before applying".

Run stats come from `.\\run.ps1 daily`; a digest built on its own says they're missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents import db
from agents.common import lastrun
from agents.common.claude import UsageTotals
from agents.scout import shortlist
from agents.scout import age
from agents.scout import waiting as stale_cleanup
from agents.tracker import report as tracker_report
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "digest"
TOP_COUNT = 3
RUNNERS_UP = 7  # ranks 4-10
WAITING_STATUSES = ("shortlisted", "researched")
POOL_STATUSES = ("new", "scored")  # pools are never shortlisted

SUBSCORE_LABELS = (
    ("skills_match", "skills"),
    ("seniority_fit", "seniority"),
    ("role_interest", "role"),
    ("employer_quality", "employer"),
    ("company_pref", "company"),
    ("location", "location"),
    ("compensation", "comp"),
)

CHEAT_SHEET = """\
| Command | What it does |
|---|---|
| `.\\run.ps1 daily` | Whole morning run: fetch, track, score, details, pass 2, cleanup (stale, too old), shortlist, research, digest |
| `.\\run.ps1 track --since-days 7` | Read application emails again and update applied jobs |
| `.\\run.ps1 review` | The review app: tabs, Read panel (brief, posting, paste the official URL), rate, hold, skip, shortlist now, "I applied" (127.0.0.1 only) |
| `review\\index.html` | Read-only copy for your phone: every job in play with its brief rendered (`.\\run.ps1 review --static` rebuilds it) |
| `.\\run.ps1 research <id>` | (Re)write one job's brief |
| `.\\run.ps1 top 15` | Highest-scoring open jobs |
| `.\\run.ps1 show shortlisted` | Jobs by status |
| `.\\run.ps1 rate <id> up "note"` | Thumbs up; teaches the scorer (also `down`) |
| `.\\run.ps1 skip <id> "reason"` | Close a job you won't pursue |
| `.\\run.ps1 reopen <id> "reason"` | Take back a close, e.g. one under "Cleaned up today" (closed -> scored) |
| `.\\run.ps1 hold <id> "reason" --until YYYY-MM-DD` | Out of the shortlist and Still waiting until then (`unhold <id>` to undo) |
| `.\\run.ps1 set-posting <id> <url>` | You found the official posting: store it, rescore, rewrite the brief |
| `.venv\\Scripts\\python.exe -m agents.db mark-applied <id> --method ats` | You applied (only you do this) |
| `.\\run.ps1 rescore` | Rescore everything after prompt or weight changes |
| `.\\run.ps1 refilter --dry-run` | Preview hard-filter changes after editing profile.yaml |
| `.\\run.ps1 posting-age --dry-run` | Which open jobs have a posting too old (daily closes them) |
| `.\\run.ps1 gmail-check` | Check the Gmail connection |"""


@dataclass
class RunStats:
    emails_read: int = 0
    jobs_found: int = 0
    filtered: int = 0
    scored: int = 0
    details_found: int = 0
    details_official: int = 0
    pass2_rescored: int = 0
    briefs_written: int = 0
    tracked: int = 0  # application emails classified by the track step
    shortlisted: int = 0  # picked by this run's shortlist step: the toast's "new picks"
    cleaned_up: int = 0  # jobs this run closed as stale or with a posting too old
    usage: UsageTotals = field(default_factory=UsageTotals)
    steps: list[tuple[str, str]] = field(default_factory=list)  # (step, "ok" or error)


def build(
    today: date | None = None,
    *,
    stats: RunStats | None = None,
    warnings: list[str] | None = None,
    min_score: float = shortlist.DEFAULT_MIN_SCORE,
    profile: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> str:
    today = today or date.today()
    profile = profile or {}  # only the posting-age limits are read; missing ones use the defaults
    db.init_db(db_path)
    shortlisted_on = {jid: _local_date(at) for jid, at in db.shortlisted_at(db_path=db_path).items()}
    jobs = {j["id"]: j for j in db.get_jobs(db_path=db_path)}

    # A job moved back to scored (possibly closed) gives up its place in the top 3.
    top = sorted((jobs[jid] for jid, d in shortlisted_on.items()
                  if d == today and jid in jobs and jobs[jid]["status"] != "scored"),
                 key=lambda j: -(j["score"] or 0))
    waiting = sorted(
        (jobs[jid] for jid, d in shortlisted_on.items()
         if d < today and jid in jobs and jobs[jid]["status"] in WAITING_STATUSES and not db.is_held(jobs[jid])),
        key=lambda j: shortlisted_on[j["id"]],
    )
    worth_checking = shortlist.would_have_made_it(top, min_score, db_path=db_path, count=TOP_COUNT, profile=profile)
    held_old = age.held_too_old(profile, db_path=db_path, today=today)
    pools = sorted((j for j in jobs.values() if j["status"] in POOL_STATUSES and shortlist.is_recruiter_pool(j)),
                   key=lambda j: (-(j["score"] or 0), j["id"]))
    skip_ids = {j["id"] for j in worth_checking} | {j["id"] for j in pools}
    runners_up = [j for j in db.get_top_jobs(RUNNERS_UP + len(skip_ids), "scored", db_path=db_path)
                  if j["id"] not in skip_ids][:RUNNERS_UP]

    out = [f"# Job digest {today.isoformat()}", "", f"**Last run:** {lastrun.summary(db_path, today)[0]}", ""]

    notices = [n["message"] for n in db.get_active_notices(db_path=db_path)]
    all_warnings = notices + list(warnings or [])
    out += ["## Warnings", ""]
    out += [f"- ⚠️ {w}" for w in all_warnings] if all_warnings else ["None."]
    out.append("")
    out += tracker_report.digest_lines(db_path=db_path, today=today)

    out += [f"## Today's top {TOP_COUNT}", ""]
    if not top:
        out += ["No new jobs cleared the shortlist bar today.", ""]
    for rank, job in enumerate(top, 1):
        out += _top_entry(rank, job, today)

    out += ["## Worth checking yourself", ""]
    if not worth_checking:
        out += ["None: every job good enough for the top 3 had a verified posting.", ""]
    else:
        out += ["These would have made the top 3, but no open official posting is confirmed, "
                "so they weren't shortlisted.", ""]
        for job in worth_checking:
            moved = " (moved back from the shortlist)" if job["id"] in shortlisted_on else ""
            found, note = _link(job)
            if shortlist.possibly_closed(job):
                why = f"possibly closed{moved}; source {job.get('source_trust') or 'unknown'}, posting: {found}{note}"
            elif job.get("source_trust") == "aggregator":
                why = (f"found only on aggregator — run set-posting if you find the official page{moved}; "
                       f"aggregator posting: {found}")
            elif job.get("source_trust") == "not_found":
                why = "posting not found"
            else:
                why = "not yet checked against a full posting"
            out.append(f"- #{job['id']} {_score(job)} **{job['company']}**: {job['title']} "
                       f"({job.get('location') or 'location unknown'}), {why}. {age.label(job, today)}. "
                       f"Alert link: {job.get('url') or 'none'}")
        out.append("")

    if pools:
        out += ["## Recruiter pools (optional, low effort)", ""]
        out += [f"- #{job['id']} **{job['company']}**: {job['title']}, {_link(job)[0]}" for job in pools]
        out.append("")

    out += ["## Still waiting", ""]
    if not waiting:
        out += ["Nothing from earlier days is waiting on you.", ""]
    else:
        for job in waiting:
            days = (today - shortlisted_on[job["id"]]).days
            out.append(f"- #{job['id']} **{job['company']}**: {job['title']} ({_score(job)}), "
                       f"waiting {days} day{'s' if days != 1 else ''}, {job['status']}. {age.label(job, today)}. "
                       f"{_link(job)[0]}")
        out.append("")
    if held_old:
        out += ["On hold, with a posting past the age limit (held jobs are never closed):", ""]
        out += [f"- #{job['id']} **{job['company']}**: {job['title']} ({_score(job)}), {age.note(job, profile, today)}"
                for job in held_old]
        out.append("")

    out += ["## Cleaned up today", ""]
    stale = stale_cleanup.cleaned_up(today, db_path=db_path)
    too_old = age.closed_on(today, db_path=db_path)
    if not stale and not too_old:
        out += ["Nothing went stale today, and no posting was too old.", ""]
    else:
        out += ["Closed today: shortlisted jobs with no action from you (no rating, hold, skip or apply), and "
                f"postings too old ({age.limits_text(profile)}). Take one back with `.\\run.ps1 reopen <id> \"reason\"`.", ""]
        out += [f"- #{job['id']} **{job['company']}**: {job['title']} ({_score(job)}), {job['closed_detail']}"
                for job in sorted([*stale, *too_old], key=lambda j: (-(j["score"] or 0), j["id"]))]
        out.append("")

    out += [f"## Runners-up {TOP_COUNT + 1}-{TOP_COUNT + RUNNERS_UP}", ""]
    if not runners_up:
        out += ["None.", ""]
    for rank, job in enumerate(runners_up, TOP_COUNT + 1):
        out.append(f"{rank}. #{job['id']} {_score(job)} **{job['company']}**: {job['title']} "
                   f"({job.get('location') or 'location unknown'})")
    out.append("")

    out += ["## Run stats", ""]
    out += _stats_lines(stats)
    out += ["## Commands", "", CHEAT_SHEET, ""]
    return "\n".join(out)


def write(text: str, today: date | None = None, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{(today or date.today()).isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _top_entry(rank: int, job: dict[str, Any], today: date) -> list[str]:
    detail = json.loads(job["score_detail"]) if job.get("score_detail") else {}
    why = detail.get("why") or detail.get("reason") or "No reason recorded."
    if detail.get("pass") == 2:
        gaps = "; ".join(detail.get("gaps") or []) or "none noted"
    else:
        gaps = "not checked: no full posting was found"
    link, link_note = _link(job)
    lines = [
        f"### {rank}. {job['company']}: {job['title']} ({_score(job)})",
        "",
        f"- **Location:** {job.get('location') or 'unknown'}",
        f"- **Posted:** {age.label(job, today)}",
        f"- **Salary:** {_salary(job)}",
        f"- **Why:** {why}",
        f"- **Gaps:** {gaps}",
    ]
    if detail.get("red_flags"):
        lines.append(f"- **Red flags:** {'; '.join(detail['red_flags'])}")
    lines += [
        f"- **Apply:** {link}{link_note}",
        f"- **Source:** {job.get('source_trust') or 'unknown'}",
        f"- **Brief:** {_brief_link(job)}",
        f"- **Sub-scores:** {_subscores(detail)}",
        f"- Job #{job['id']}. `.\\run.ps1 rate {job['id']} up` / `down`, `.\\run.ps1 skip {job['id']} \"reason\"`",
        "",
    ]
    return lines


def _brief_link(job: dict[str, Any]) -> str:
    if not job.get("brief_path"):
        return f"not written yet; research retries it next run (`.\\run.ps1 research {job['id']}`)"
    path = Path(job["brief_path"])
    target = path.as_posix() if path.is_absolute() else "../" + path.as_posix()  # digest/ sits next to briefs/
    return f"[Read brief]({target})"


def _subscores(detail: dict[str, Any]) -> str:
    comps = detail.get("components") or {}
    parts = [f"{label} {comps[key]['value']}" for key, label in SUBSCORE_LABELS if key in comps]
    extra = [f"{k} {v}" for a in detail.get("adjustments") or [] for k, v in a.items()]
    if detail.get("dealbreaker"):
        extra.append(f"dealbreaker: {detail['dealbreaker']} (capped at {detail.get('capped_at') or 20})")
    text = " · ".join(parts) or "n/a"
    if extra:
        text += " · " + " · ".join(extra)
    if detail.get("pass"):
        text += f" (pass {detail['pass']})"
    return text


def _link(job: dict[str, Any]) -> tuple[str, str]:
    trust = job.get("source_trust")
    found = job.get("apply_url") or job.get("source_url")
    if found and trust == "aggregator":
        return found, " (aggregator, verify before applying)"
    if found:
        return found, "" if job.get("apply_url") else " (posting page)"
    if job.get("url"):
        return job["url"], " (alert link; official posting not found)"
    return "no link", ""


def _salary(job: dict[str, Any]) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return "not stated"
    cur = job.get("salary_currency") or "USD"
    sym = "$" if cur == "USD" else f"{cur} "
    text = f"{sym}{lo:,}–{sym}{hi:,}" if lo and hi and lo != hi else f"{sym}{(hi or lo):,}"
    return text + (" (job site estimate)" if job.get("salary_is_predicted") else "")


def _score(job: dict[str, Any]) -> str:
    return "unscored" if job.get("score") is None else f"{job['score']:.1f}"


def _stats_lines(stats: RunStats | None) -> list[str]:
    if stats is None:
        return ["Not available: this digest wasn't built by `.\\run.ps1 daily`.", ""]
    lines = [
        f"- Emails read: {stats.emails_read}",
        f"- Jobs found: {stats.jobs_found}",
        f"- Application emails classified: {stats.tracked}",
        f"- Filtered out: {stats.filtered}",
        f"- Scored (pass 1): {stats.scored}",
        f"- Postings found: {stats.details_found} ({stats.details_official} official, "
        f"{stats.details_found - stats.details_official} aggregator); rescored in pass 2: {stats.pass2_rescored}",
        f"- Briefs written: {stats.briefs_written}",
    ]
    if stats.steps:
        lines.append("- Steps: " + ", ".join(f"{name} {state}" for name, state in stats.steps))
    total = stats.usage.total()
    lines.append(f"- Claude usage: {stats.usage.calls} calls, {total.input_tokens + total.cache_creation_tokens + total.cache_read_tokens:,} input tokens "
                 f"({total.cache_read_tokens:,} from cache), {total.output_tokens:,} output tokens, "
                 f"{total.web_searches} web searches (≈ ${total.cost_usd:.2f} at API prices)")
    for u in sorted(stats.usage.by_model.values(), key=lambda u: u.model):
        lines.append(f"  - {u.model}: {u.fields()}")
    lines.append("")
    return lines


def _local_date(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone().date()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.digest", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    min_score = profile.get("shortlist_min_score", shortlist.DEFAULT_MIN_SCORE)
    path = write(build(db_path=args.db, min_score=min_score, profile=profile), out_dir=args.out_dir)
    print(f"Digest: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
