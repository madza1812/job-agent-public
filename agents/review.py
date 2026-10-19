"""Review page: a static, read-only review/index.html built from the database.

    python -m agents.review [--db PATH] [--out PATH]

Lists the jobs in play (shortlisted, researched, applied, interviewing), best first, each
with its score, sub-scores, why and gaps, the apply link (labeled when it's an
aggregator's), and its brief rendered inline. No server, no scripts that fetch anything:
open the file in a browser. `.\\run.ps1 daily` rewrites it after the digest. The top
shows the "Last run" line, as in the digest (lastrun.py).
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import db
from agents.common import lastrun, mdhtml
from agents.tracker import report as tracker_report

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "review" / "index.html"
STATUSES = ("shortlisted", "researched", "applied", "interviewing")

CSS = """
:root { --bg:#fafaf9; --fg:#1c1917; --muted:#57534e; --card:#fff; --line:#e7e5e4; --accent:#1d4ed8; --warn:#b45309; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#171717; --fg:#f5f5f4; --muted:#a8a29e; --card:#1f1f1f; --line:#333; --accent:#93c5fd; --warn:#fbbf24; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:900px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:1.6rem; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 24px; }
nav ol { padding-left:20px; }
article { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin:20px 0; }
article > h2 { margin:0 0 6px; font-size:1.25rem; }
.meta { color:var(--muted); font-size:.92rem; margin:0 0 10px; }
.badge { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:0 8px; font-size:.8rem; margin-right:6px; }
.warn { color:var(--warn); }
a { color:var(--accent); overflow-wrap:anywhere; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 12px; margin:8px 0; }
dt { color:var(--muted); }
dd { margin:0; }
details { margin-top:12px; border-top:1px solid var(--line); padding-top:8px; }
summary { cursor:pointer; font-weight:600; }
.brief h3 { font-size:1.1rem; margin:1.2em 0 .4em; }
.brief h2 { font-size:1.2rem; }
.table-wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:.92rem; }
th, td { border:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
code { font-size:.9em; }
.timeline { padding-left:20px; margin:8px 0 0; }
.timeline li { margin:4px 0; }
.timeline time { color:var(--muted); font-size:.88rem; margin-right:6px; }
article.app > h3 { margin:0 0 6px; }
"""


def build(db_path: Path | str | None = None, root: Path = ROOT) -> str:
    db.init_db(db_path)
    jobs = [j for s in STATUSES for j in db.get_jobs(status=s, db_path=db_path)]
    jobs.sort(key=lambda j: -(j["score"] or 0))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    last_run, run_ok = lastrun.summary(db_path)
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Job review</title>",
        f"<style>{CSS}</style></head><body><main>",
        "<h1>Job review</h1>",
        f'<p class="sub{"" if run_ok else " warn"}" id="last-run">Last run: {_e(last_run)}</p>',
        f'<p class="sub">{len(jobs)} jobs in play · built {now} · read-only · '
        '<a href="#applications">Applications</a></p>',
    ]
    out.append(_applications(db_path))
    out.append('<h2 id="in-play">Jobs in play</h2>')
    if jobs:
        out.append("<nav><ol>" + "".join(
            f'<li><a href="#job-{j["id"]}">{_e(j["company"])}: {_e(j["title"])}</a> '
            f'({_score(j)}, {j["status"]})</li>' for j in jobs) + "</ol></nav>")
    else:
        out.append("<p>Nothing shortlisted yet.</p>")
    for job in jobs:
        out.append(_card(job, root))
    out.append("</main></body></html>")
    return "\n".join(out)


def _applications(db_path: Path | str | None) -> str:
    """The Applications view: every job applied to, with its timeline of status changes
    and application emails (agents/tracker/report.py)."""
    apps = db.get_applications(db_path=db_path)
    out = ['<section id="applications"><h2>Applications</h2>']
    if not apps:
        out.append("<p>No applications yet. Mark one with <code>python -m agents.db mark-applied</code>.</p>")
    stale = {job["id"]: (kind, days) for kind, job, days in tracker_report.stale(apps, db.get_tracker_messages(
        db_path=db_path), db_path=db_path)}
    for job in apps:
        note = ""
        if job["id"] in stale:
            kind, days = stale[job["id"]]
            note = (f' <span class="warn">{"consider a follow-up" if kind == "follow_up" else "check in"}: '
                    f'{days} days quiet</span>')
        out.append(f'<article class="app" id="app-{job["id"]}"><h3>{_e(job["company"])}: {_e(job["title"])}</h3>'
                   f'<p class="meta"><span class="badge">{_e(job["status"])}</span>#{job["id"]} · applied '
                   f'{_e((job.get("applied_at") or "")[:10] or "date unknown")}{note}</p><ol class="timeline">')
        for item in tracker_report.timeline(job, db_path=db_path):
            cls = ' class="warn"' if item["kind"] in ("needs_review", "offer") else ""
            when = datetime.fromisoformat(item["at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            out.append(f"<li{cls}><time>{when}</time> {_e(item['text'])}</li>")
        out.append("</ol></article>")
    out.append("</section>")
    return "\n".join(out)


def _card(job: dict[str, Any], root: Path) -> str:
    detail = json.loads(job["score_detail"]) if job.get("score_detail") else {}
    link = job.get("apply_url") or job.get("source_url") or job.get("url")
    trust = job.get("source_trust")
    link_note = {"aggregator": ' <span class="warn">(aggregator, verify before applying)</span>',
                 "not_found": ' <span class="warn">(alert link; official posting not found)</span>'}.get(trust, "")
    comps = detail.get("components") or {}
    subs = " · ".join(f"{k.replace('_', ' ')} {v['value']}" for k, v in comps.items())
    rows = [
        ("Location", _e(job.get("location") or "unknown")),
        ("Salary", _e(_salary(job))),
        ("Why", _e(detail.get("why") or detail.get("reason") or "")),
        ("Gaps", _e("; ".join(detail.get("gaps") or []) or "not checked")),
    ]
    if detail.get("red_flags"):
        rows.append(("Red flags", _e("; ".join(detail["red_flags"]))))
    rows.append(("Apply", (f'<a href="{_e(link)}">{_e(link)}</a>' if _safe(link) else "no link") + link_note))
    rows.append(("Sub-scores", _e(subs or "n/a")))
    parts = [
        f'<article id="job-{job["id"]}">',
        f'<h2>{_e(job["company"])}: {_e(job["title"])}</h2>',
        f'<p class="meta"><span class="badge">{job["status"]}</span><span class="badge">score {_score(job)}</span>'
        f'job #{job["id"]}</p>',
        "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>",
    ]
    brief = _read_brief(job, root)
    if brief is None:
        parts.append('<p class="meta">No brief yet.</p>')
    else:
        if brief.startswith("# "):  # the card already shows company and title
            brief = brief.partition("\n")[2]
        body = mdhtml.to_html(brief, heading_offset=1).replace("<table>", '<div class="table-wrap"><table>') \
            .replace("</table>", "</table></div>")
        parts.append(f'<details open><summary>Brief ({_e(job["brief_path"])})</summary>'
                     f'<div class="brief">{body}</div></details>')
    parts.append("</article>")
    return "\n".join(parts)


def _read_brief(job: dict[str, Any], root: Path) -> str | None:
    if not job.get("brief_path"):
        return None
    path = Path(job["brief_path"])
    path = path if path.is_absolute() else root / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return f"*Brief file missing: {job['brief_path']}*"


def _safe(url: str | None) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))


def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _score(job: dict[str, Any]) -> str:
    return "unscored" if job.get("score") is None else f"{job['score']:.1f}"


def _salary(job: dict[str, Any]) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return "not stated"
    return f"${lo:,}–${hi:,}" if lo and hi and lo != hi else f"${(hi or lo):,}"


def write(text: str, out: Path = DEFAULT_OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.review", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    path = write(build(db_path=args.db), args.out)
    print(f"Review page: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
