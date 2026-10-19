"""Review app: the review page as a small local web app, with buttons.

    python -m agents.review_app [--db PATH] [--profile PATH] [--port N] [--no-browser]
    python -m agents.review_app --static [--out PATH]     # the read-only review/index.html

`.\\run.ps1 review` starts it: a standard-library HTTP server bound to 127.0.0.1 only, on
a free port (or --port), and opens the browser. Ctrl+C stops it. One page, five tabs
with counts (tabs()): Today (shortlisted/researched, shortlisted today), Near misses
(scored, score NEAR_MISS_FLOOR or more: official or not_found below shortlist_min_score,
aggregator-only at any score, and official ones at the bar that "Shortlist now" can
take), On hold, Waiting (shortlisted/researched, shortlisted before today) and Applied
(applied/interviewing, with the tracker timeline). A Rated filter shows unrated jobs.

Each card has one Read button: a side panel with a Brief tab (when there's a brief, with
Rewrite brief) and a Posting tab (the stored description, links, source trust, salary;
Write brief when there's no brief yet; Find posting when there's no description; and a
box to paste the official posting URL).

Every button goes through the same functions as the CLI, so db.set_status() still
enforces the transitions: rate and skip (agents.feedback), hold and unhold
(feedback.hold, db.set_hold), "I applied" (db.mark_applied, actor user, the user's own
click; on any open job, see db.USER_APPLY_FROM) and Shortlist now
(shortlist.shortlist_now, within the 3 a day, then a brief). Three run in a background
thread, one per job at a time, with progress on the card and in the panel: research
(agents.research.brief.research), find (details.fetch_details for that job, then pass 2)
and posting (posting.set_posting: fetch, store, official, pass 2, brief rewrite). Before it
applies a pending migration the app backs up the database (db.backup:
data/backups/jobs-YYYYMMDD-HHMM.db, the last 10 kept). Each action answers with the job's
new card and the tab counts, so the page updates in place.

Safety: the server never listens beyond 127.0.0.1; it answers only requests whose Host
is 127.0.0.1 or localhost on its port (no DNS rebinding); every /api request must carry
the per-run token embedded in the page (no cross-site POSTs); one action per job at a
time (a second click gets 409). Briefs are rendered by mdhtml and postings by
posting_html(), both escaping all HTML and linking only http(s) URLs.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import secrets
import sys
import threading
import webbrowser
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import requests

from agents import db, feedback, review
from agents.common import lastrun, mdhtml, runlog
from agents.research import brief
from agents.scout import age, details, pass2, posting, shortlist
from agents.scout.companies import DEFAULT_PREFERRED_PATH, load_preferred
from agents.scout.filters import DEFAULT_PROFILE_PATH, load_profile
from agents.tracker import report as tracker_report

log = logging.getLogger(__name__)

HOST = "127.0.0.1"  # never anything else
NEAR_MISS_FLOOR = 55
NEAR_MISS_TRUST = ("official", "not_found")  # below the bar; aggregator-only at any score
IN_PLAY = ("shortlisted", "researched")
APPLIED = ("applied", "interviewing")
APPLY_METHODS = ("ats", "referral", "email")
FINDABLE = ("new", "scored", "shortlisted", "researched")  # Find posting / paste a URL
TABS = {"today": "Today", "near": "Near misses", "hold": "On hold", "waiting": "Waiting", "applied": "Applied"}
MAX_BODY = 16_384
_API = re.compile(r"^/api/jobs/(\d+)/(card|read|task|rate|hold|unhold|skip|applied|research|find|posting|shortlist)$")
_GETS = ("card", "read", "task")
_URL = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]")

# Background tasks, one per job: kind -> (running, done, failed prefix)
TASKS = {
    "research": ("Writing brief… (about 30–60 s)", "Brief updated ✓", "Brief failed"),
    "find": ("Finding posting… (about 30–60 s)", "Posting found ✓", "Lookup failed"),
    "posting": ("Fetching posting and rescoring… (about 30–60 s)", "Posting stored ✓", "Posting failed"),
}
WRITING, BRIEF_DONE = TASKS["research"][:2]
REWRITE_CONFIRM = "Write a new brief? Takes about a minute and uses some Claude allowance."
NEEDS_OFFICIAL = "Needs official posting"
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           b'<rect x="3" y="9" width="26" height="19" rx="3" fill="#1d4ed8"/>'
           b'<path d="M11 9V6a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v3" fill="none" stroke="#1d4ed8" stroke-width="2.5"/>'
           b'<path d="M10 18l4 4 8-8" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>')

# A task function returns (good, message): good=False is a plain outcome such as
# "No posting found"; a failure raises. Tests pass fakes.
ResearchFn = Callable[[int, "Path | str | None"], Any]  # -> brief.Result
FindFn = Callable[[int, "Path | str | None"], "tuple[bool, str]"]
PostingFn = Callable[[int, str, "Path | str | None"], "tuple[bool, str]"]


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class TaskFailed(Exception):
    pass


# --- which tab a job is on ------------------------------------------------------------

def tab_for(job: dict[str, Any], shortlisted_on: dict[int, date], min_score: float,
            today: date | None = None) -> str | None:
    today = today or date.today()
    status = job["status"]
    if status == "closed":
        return None
    if db.is_held(job, today):
        return "hold"
    if status in APPLIED:
        return "applied"
    if status in IN_PLAY:
        return "today" if shortlisted_on.get(job["id"]) == today else "waiting"
    if status == "scored" and job.get("score") is not None and job["score"] >= NEAR_MISS_FLOOR:
        trust = job.get("source_trust")
        if (trust in NEAR_MISS_TRUST and job["score"] < min_score) or trust == "aggregator" \
                or shortlist.ready(job, min_score, today):
            return "near"
    return None


def tabs(db_path: Path | str | None, min_score: float, today: date | None = None) -> dict[str, list[dict[str, Any]]]:
    """{tab: jobs}, best score first; Applied newest application first (with applied_at)."""
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in TABS}
    shortlisted_on = _shortlisted_on(db_path)
    applied_at = {j["id"]: j.get("applied_at") for j in db.get_applications(db_path=db_path)}
    for job in db.get_jobs(db_path=db_path):
        tab = tab_for(job, shortlisted_on, min_score, today)
        if tab:
            out[tab].append({**job, "applied_at": applied_at.get(job["id"])})
    out["applied"].sort(key=lambda j: (j.get("applied_at") or "", j["id"]), reverse=True)
    return out


def _shortlisted_on(db_path: Path | str | None) -> dict[int, date]:
    return {jid: datetime.fromisoformat(at).astimezone().date()
            for jid, at in db.shortlisted_at(db_path=db_path).items()}


# --- rendering -----------------------------------------------------------------------

_e = review._e


def _task_html(task: dict[str, Any] | None, inline: bool = True) -> str:
    """A background task's state: spinner while running, the outcome after."""
    if not task:
        return ""
    if task["state"] == "running":
        return (f'<span class="state running"><span class="spinner" aria-hidden="true"></span>'
                f'{_e(task["message"])}</span>')
    if task["state"] == "failed":
        return f'<p class="error" role="alert">{_e(task["message"])}</p>'
    out = f'<span class="state{" ok" if task.get("good") else ""}">{_e(task["message"])}</span>'
    return out if inline else f"<p>{out}</p>"


def card(job: dict[str, Any], tab: str, *, task: dict[str, Any] | None = None, min_score: float = 0,
         db_path: Path | str | None = None, today: date | None = None, root: Path = review.ROOT,
         profile: dict[str, Any] | None = None) -> str:
    """task: this job's background task in the app ({kind, state, message, good}), if any.
    profile: for the posting-age limits (a held job past its limit is flagged)."""
    running = bool(task and task["state"] == "running")
    detail = json.loads(job["score_detail"]) if job.get("score_detail") else {}
    jid = job["id"]
    link = job.get("apply_url") or job.get("source_url") or job.get("url")
    trust = job.get("source_trust") or "unknown"
    trust_note = {"aggregator": "aggregator, verify before applying",
                  "not_found": "alert link; official posting not found"}.get(trust, "")
    rating = job.get("user_rating") or 0
    held = db.is_held(job, today)

    posted = age.label(job, today)
    if held and age.over_limit(job, profile or {}, today):
        posted = f'{_e(posted)} <span class="warn">{_e(age.HELD_NOTE)}</span>'
    else:
        posted = _e(posted)
    rows = [("Location", _e(job.get("location") or "unknown")), ("Posted", posted),
            ("Salary", _e(review._salary(job))),
            ("Why", _e(detail.get("why") or detail.get("reason") or "")),
            ("Gaps", _e("; ".join(detail.get("gaps") or []) or "not checked"))]
    if detail.get("red_flags"):
        rows.append(("Red flags", f'<span class="warn">{_e("; ".join(detail["red_flags"]))}</span>'))
    rows.append(("Source", f'<span class="badge trust-{_e(trust)}">{_e(trust)}</span>'
                 + (f' <span class="warn">{trust_note}</span>' if trust_note else "")))
    rows.append(("Apply", _link(link)))
    if held:
        rows.append(("On hold", _e(f"{job['hold_reason']} (until {job['held_until'] or 'you unhold it'})")))

    bars = "".join(
        f'<div class="bar"><span>{_e(k.replace("_", " "))}</span>'
        f'<span class="track"><span class="fill" style="width:{_pct(v.get("value"))}%"></span></span>'
        f'<span class="num">{_e(v.get("value"))}</span></div>'
        for k, v in (detail.get("components") or {}).items())

    buttons = [
        '<div class="row rate">'
        f'<input name="note" maxlength="300" placeholder="note (optional)" aria-label="rating note">'
        f'<button data-act="rate" data-dir="up" class="{"on" if rating == 1 else ""}" title="thumbs up">👍</button>'
        f'<button data-act="rate" data-dir="down" class="{"on" if rating == -1 else ""}" title="thumbs down">👎</button>'
        "</div>",
        '<div class="row">',
        '<button data-act="read">Read</button>',
    ]
    if shortlist.ready(job, min_score, today) and not running:
        buttons.append('<button data-act="shortlist" class="primary">Shortlist now</button>')
    if job["status"] in db.USER_APPLY_FROM:
        buttons.append('<button data-act="open" data-form="applied">✅ I applied</button>')
    if held:
        buttons.append('<button data-act="unhold">Unhold</button>')
    elif job["status"] in db.HOLDABLE:
        buttons.append('<button data-act="open" data-form="hold">Hold</button>')
    if job["status"] in db.HOLDABLE:
        buttons.append('<button data-act="open" data-form="skip">Skip</button>')
    if task and task["state"] != "failed":
        buttons.append(_task_html(task))
    buttons.append("</div>")
    if task and task["state"] == "failed":
        buttons.append(_task_html(task))

    forms = [
        '<form data-form="hold" hidden><label>Until <input type="date" name="until"></label>'
        '<input name="reason" required maxlength="300" placeholder="reason (required)">'
        '<button type="submit">Hold</button> <span class="hint">no date: until you unhold it</span></form>',
        '<form data-form="skip" hidden><input name="reason" required maxlength="300" placeholder="why skip it?">'
        '<button type="submit">Skip</button></form>',
        '<form data-form="applied" hidden><label>Method <select name="method">'
        + "".join(f'<option value="{m}">{m}</option>' for m in APPLY_METHODS)
        + '</select></label><input name="notes" maxlength="1000" placeholder="notes (optional)">'
        '<button type="submit">Mark applied</button></form>',
    ]

    busy = f' data-task="{_e(task["kind"])}"' if running else ""
    badge = f' <span class="badge needs">{NEEDS_OFFICIAL}</span>' if job.get("source_trust") == "aggregator" else ""
    parts = [
        f'<article class="job" id="job-{jid}" data-id="{jid}" data-tab="{tab}" data-rated="{rating}" '
        f'data-score="{job.get("score") or 0}"{busy}>',
        f'<h3><span class="score">{review._score(job)}</span> {_e(job["company"])}: {_e(job["title"])}{badge}</h3>',
        f'<p class="meta"><span class="badge">{_e(job["status"])}</span>#{jid}'
        + (f' · applied {_e(job["applied_at"][:10])}' if job.get("applied_at") else "")
        + _brief_date(job, root)
        + {1: " · 👍 rated", -1: " · 👎 rated"}.get(rating, " · not rated") + "</p>",
        "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>",
        f'<div class="bars">{bars}</div>' if bars else "",
        *buttons, *forms,
    ]
    if tab == "applied":
        parts.append(_timeline(job, db_path))
    parts.append("</article>")
    return "\n".join(p for p in parts if p)


def read_panel(job: dict[str, Any], *, task: dict[str, Any] | None = None, min_score: float = 0,
               root: Path = review.ROOT, today: date | None = None) -> tuple[str, str]:
    """(html, default tab) for the Read panel: Brief (when there's a brief) and Posting."""
    has_brief = bool(job.get("brief_path"))
    description = (job.get("description") or "").strip()
    running = bool(task and task["state"] == "running")
    status_line = _task_html(task, inline=False)
    out = []
    if has_brief:
        out.append('<div class="ptabs" role="tablist"><button data-ptab="brief">Brief</button>'
                   '<button data-ptab="posting">Posting</button></div>')
        rewrite = (f'<button data-act="research" data-confirm="{_e(REWRITE_CONFIRM)}">Rewrite brief</button>'
                   if job["status"] in brief.RESEARCHABLE and not running else "")
        out.append(f'<div data-pane="brief"><div class="row">{_brief_date(job, root).lstrip(" ·")}{rewrite}</div>'
                   f'{status_line}<div class="brief">{brief_html(job, root)}</div></div>')

    pane = ['<div data-pane="posting">']
    if not has_brief and description and job["status"] in brief.RESEARCHABLE and not running:
        pane.append('<div class="row"><button data-act="research" class="primary">Write brief</button></div>')
    if shortlist.ready(job, min_score, today) and not running:
        pane.append('<div class="row"><button data-act="shortlist" class="primary">Shortlist now</button></div>')
    pane.append(status_line)
    if description:
        trust = job.get("source_trust") or "unknown"
        pane.append("<dl>"
                    f"<dt>Apply</dt><dd>{_link(job.get('apply_url'))}</dd>"
                    f"<dt>Source</dt><dd>{_link(job.get('source_url'))}</dd>"
                    f'<dt>Trust</dt><dd><span class="badge trust-{_e(trust)}">{_e(trust)}</span></dd>'
                    f"<dt>Posted</dt><dd>{_e(age.label(job, today))}</dd>"
                    f"<dt>Salary</dt><dd>{_e(review._salary(job))}</dd></dl>")
        pane.append(f'<div class="posting">{posting_html(description)}</div>')
    else:
        pane.append(f'<p><strong>No posting found yet.</strong> Alert link: {_link(job.get("url"))}</p>')
        if job["status"] in FINDABLE and not running:
            pane.append('<div class="row"><button data-act="find" class="primary">Find posting</button>'
                        '<span class="hint">a web search; uses some Claude allowance</span></div>')
    if job["status"] in FINDABLE and not running:
        pane.append('<form data-form="posting"><label for="paste-url">Paste official posting URL</label>'
                    '<input id="paste-url" type="url" name="url" required maxlength="2000" placeholder="https://…">'
                    '<button type="submit">Use this posting</button></form>')
    pane.append("</div>")
    out.append("".join(pane))
    return "".join(out), ("brief" if has_brief else "posting")


def posting_html(text: str) -> str:
    """Plain posting text: escaped, paragraphs and line breaks kept, http(s) URLs linked."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]
    return "".join(f"<p>{_linkify(p)}</p>" for p in paras)


def _linkify(text: str) -> str:
    out, pos = [], 0
    for m in _URL.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        url = html.escape(m.group(0), quote=True)
        out.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def _link(url: str | None) -> str:
    return (f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(url)}</a>'
            if review._safe(url) else "no link")


def _brief_date(job: dict[str, Any], root: Path) -> str:
    """ · Brief: Oct 18 (when the brief file was last written; the time is in the tooltip)."""
    if not job.get("brief_path"):
        return ""
    path = Path(job["brief_path"])
    try:
        written = datetime.fromtimestamp((path if path.is_absolute() else root / path).stat().st_mtime)
    except OSError:
        return ' · <span class="warn">Brief: file missing</span>'
    return (f' · <span class="brief-date" title="written {written:%Y-%m-%d %H:%M}">'
            f'Brief: {written:%b} {written.day}</span>')


def _timeline(job: dict[str, Any], db_path: Path | str | None) -> str:
    items = []
    for item in tracker_report.timeline(job, db_path=db_path):
        cls = ' class="warn"' if item["kind"] in ("needs_review", "offer") else ""
        when = datetime.fromisoformat(item["at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        items.append(f"<li{cls}><time>{when}</time> {_e(item['text'])}</li>")
    return '<ol class="timeline">' + ("".join(items) or "<li>No events yet.</li>") + "</ol>"


def _pct(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def brief_html(job: dict[str, Any], root: Path = review.ROOT) -> str:
    text = review._read_brief(job, root)
    if text is None:
        return "<p>No brief yet.</p>"
    body = mdhtml.to_html(text, heading_offset=1)
    return body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")


_ON = ' class="on"'


def page(app: "App") -> str:
    min_score = app.min_score()
    profile = app.profile()
    groups = tabs(app.db_path, min_score, app.today())
    last_run, run_ok = lastrun.summary(app.db_path)
    nav = "".join(f'<button role="tab" data-tab="{t}"{_ON if t == "today" else ""}>{label} '
                  f'<span class="count" id="count-{t}">{len(groups[t])}</span></button>'
                  for t, label in TABS.items())
    sections = []
    for t, label in TABS.items():
        cards = "\n".join(card(j, t, task=app.task(j["id"]), min_score=min_score, db_path=app.db_path,
                               today=app.today(), root=app.root, profile=profile) for j in groups[t])
        sections.append(f'<section id="tab-{t}" data-tab="{t}"{"" if t == "today" else " hidden"}>'
                        f'<h2>{label}</h2><p class="empty">Nothing here.</p>{cards}</section>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job review</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{review.CSS}{CSS}</style></head>
<body data-token="{_e(app.token)}"><main>
<h1>Job review</h1>
<p class="sub{"" if run_ok else " warn"}" id="last-run">Last run: {_e(last_run)}</p>
<nav class="tabs" role="tablist">{nav}</nav>
<p class="filter"><label>Rated <select id="rated-filter"><option value="all">all jobs</option>
<option value="unrated">not rated yet</option><option value="rated">rated</option></select></label></p>
{"".join(sections)}
</main>
<aside id="panel" hidden><div class="panel-head"><strong id="panel-title"></strong>
<button id="panel-close" aria-label="close">✕</button></div><div id="panel-body"></div></aside>
<dialog id="confirm"><p id="confirm-text"></p><div class="row end">
<button type="button" id="confirm-no">Cancel</button><button type="button" id="confirm-yes" class="primary">Write new brief</button>
</div></dialog>
<div id="toast" role="status" hidden></div>
<script>{JS}</script></body></html>"""


CSS = """
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
.tabs button { border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:999px;
  padding:4px 12px; cursor:pointer; font:inherit; }
.tabs button.on { border-color:var(--accent); color:var(--accent); font-weight:600; }
.count { font-size:.8rem; color:var(--muted); }
.filter { color:var(--muted); font-size:.92rem; }
section > h2 { font-size:1.2rem; margin:16px 0 0; }
section .empty { display:none; color:var(--muted); }
section.is-empty .empty { display:block; }
article.job { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:14px 0; }
article.job h3 { margin:0 0 4px; font-size:1.1rem; }
article.job.filtered { display:none; }
article.job.flash { outline:2px solid var(--accent); }
.score { color:var(--accent); }
.trust-official { color:#15803d; }
.trust-aggregator, .trust-not_found { color:var(--warn); }
.badge.needs { color:var(--warn); border-color:var(--warn); font-size:.75rem; font-weight:600; vertical-align:middle; }
.bars { display:grid; grid-template-columns:repeat(auto-fill, minmax(230px, 1fr)); gap:2px 16px; margin:6px 0 10px; }
.bar { display:grid; grid-template-columns:120px 1fr 28px; gap:6px; align-items:center; font-size:.8rem; color:var(--muted); }
.track { height:6px; background:var(--line); border-radius:3px; overflow:hidden; }
.fill { display:block; height:100%; background:var(--accent); }
.num { text-align:right; }
.row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:6px 0; }
.row.rate input { flex:1 1 160px; }
button, input, select { font:inherit; font-size:.9rem; }
article.job button, form button, #panel-body button { border:1px solid var(--line); background:var(--bg); color:var(--fg);
  border-radius:6px; padding:3px 10px; cursor:pointer; }
article.job button.on, button.primary { border-color:var(--accent); background:var(--accent); color:var(--bg); }
#panel-body button.primary, article.job button.primary { background:var(--accent); color:var(--bg); }
button:disabled { opacity:.5; cursor:wait; }
input, select { border:1px solid var(--line); background:var(--bg); color:var(--fg); border-radius:6px; padding:3px 6px; }
form { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:6px 0; padding:8px;
  border:1px dashed var(--line); border-radius:8px; }
form[hidden] { display:none; }
form input[type=url] { flex:1 1 220px; }
.hint, .state { color:var(--muted); font-size:.85rem; }
.state.running { display:inline-flex; align-items:center; gap:6px; color:var(--fg); }
.state.ok { color:#15803d; font-weight:600; }
.spinner { width:14px; height:14px; border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%;
  animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation-duration:3s; } }
p.error { color:var(--warn); font-size:.9rem; margin:4px 0; }
.brief-date { color:var(--muted); }
dialog#confirm { border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--fg);
  max-width:min(420px, calc(100vw - 32px)); padding:16px 18px; }
dialog#confirm::backdrop { background:rgba(0,0,0,.35); }
dialog#confirm p { margin:0 0 12px; }
.row.end { justify-content:flex-end; }
dialog button { border:1px solid var(--line); background:var(--bg); color:var(--fg); border-radius:6px; padding:4px 12px; cursor:pointer; }
dialog button.primary { background:var(--accent); border-color:var(--accent); color:var(--bg); }
#panel { position:fixed; top:0; right:0; bottom:0; width:min(640px, 100vw); background:var(--card);
  border-left:1px solid var(--line); overflow-y:auto; padding:12px 16px 40px; box-shadow:-4px 0 16px rgba(0,0,0,.15); }
.panel-head { display:flex; justify-content:space-between; gap:8px; position:sticky; top:-12px;
  background:var(--card); padding:8px 0; z-index:1; }
#panel-close { border:1px solid var(--line); background:var(--bg); color:var(--fg); border-radius:6px; cursor:pointer; }
.ptabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin:4px 0 8px; }
.ptabs button { border:none !important; border-bottom:2px solid transparent !important; border-radius:0 !important;
  background:none !important; padding:6px 10px !important; color:var(--muted) !important; }
.ptabs button.on { border-bottom-color:var(--accent) !important; color:var(--fg) !important; font-weight:600; }
.posting { overflow-wrap:anywhere; }
#toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:var(--fg); color:var(--bg);
  padding:8px 16px; border-radius:8px; max-width:calc(100vw - 32px); z-index:2; }
#toast.err { background:var(--warn); }
"""

JS = r"""
const TOKEN = document.body.dataset.token;
const panel = document.getElementById("panel");
async function api(method, path, body) {
  const r = await fetch(path, {method, headers: {"Content-Type": "application/json", "X-Review-Token": TOKEN},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  let data;
  try { data = await r.json(); } catch (e) { data = {ok: false, error: "HTTP " + r.status}; }
  if (!r.ok || !data.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
let toastTimer;
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = err ? "err" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, err ? 6000 : 2500);
}
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("main section").forEach(s => { s.hidden = s.dataset.tab !== name; });
  try { localStorage.setItem("review-tab", name); } catch (e) {}
}
function applyFilter() {
  const f = document.getElementById("rated-filter").value;
  document.querySelectorAll("article.job").forEach(a => {
    const rated = a.dataset.rated !== "0";
    a.classList.toggle("filtered", (f === "rated" && !rated) || (f === "unrated" && rated));
  });
  document.querySelectorAll("main section").forEach(s =>
    s.classList.toggle("is-empty", !s.querySelector("article.job:not(.filtered)")));
}
function setBusy(el, busy) {
  el.dataset.busy = busy ? "1" : "";
  el.querySelectorAll("button:not(#panel-close), input, select").forEach(x => { x.disabled = busy; });
}
function place(html, tab, old) {
  // Put the new card where it belongs: in place on the same tab, else by score (Applied: on top).
  const tmp = document.createElement("div"); tmp.innerHTML = html.trim();
  const fresh = tmp.firstElementChild;
  if (old && old.dataset.tab === tab) { old.replaceWith(fresh); }
  else {
    if (old) old.remove();
    const sec = document.getElementById("tab-" + tab);
    const score = parseFloat(fresh.dataset.score);
    const after = tab === "applied" ? sec.querySelector("article.job")
      : [...sec.querySelectorAll("article.job")].find(a => parseFloat(a.dataset.score) < score);
    if (after) sec.insertBefore(fresh, after); else sec.appendChild(fresh);
  }
  fresh.classList.add("flash"); setTimeout(() => fresh.classList.remove("flash"), 1500);
  if (fresh.dataset.task) poll(fresh.dataset.id);
  return fresh;
}
function applyResult(id, data, quiet) {
  const old = document.getElementById("job-" + id);
  if (data.card && data.tab) place(data.card, data.tab, old);
  else if (old) old.remove();
  for (const [t, n] of Object.entries(data.counts || {})) {
    const el = document.getElementById("count-" + t); if (el) el.textContent = n;
  }
  applyFilter();
  if (data.message && !quiet) toast(data.message + (data.tab && old && old.dataset.tab !== data.tab
    ? " (now in " + document.querySelector('.tabs button[data-tab="' + data.tab + '"]').firstChild.textContent.trim() + ")" : ""));
}
function panelOpenFor(id) { return !panel.hidden && panel.dataset.id === String(id); }
async function act(el, id, path, body) {
  if (el.dataset.busy) return;  // a click while the last one is still running
  setBusy(el, true);
  try {
    const data = await api("POST", path, body);
    applyResult(id, data);
    if (panelOpenFor(id)) await openRead(id, currentPane());
    if (data.task && data.task.state === "running") poll(id);
  } catch (e) { toast(e.message, true); }
  finally { if (el.isConnected) setBusy(el, false); }
}
const polling = new Set();
async function poll(id) {
  if (polling.has(String(id))) return;
  polling.add(String(id));
  try {
    for (;;) {
      await new Promise(r => setTimeout(r, 3000));
      let data;
      try { data = await api("GET", "/api/jobs/" + id + "/task"); }
      catch (e) { toast(e.message, true); return; }
      if (data.state === "running") continue;
      applyResult(id, data, true);
      toast(data.message, data.state === "failed");
      if (data.kind === "research" && data.state === "done") await openRead(id, "brief");  // the new brief, right away
      else if (panelOpenFor(id)) await openRead(id, data.kind === "research" ? currentPane() : "posting");
      return;
    }
  } finally { polling.delete(String(id)); }
}
function currentPane() {
  const on = panel.querySelector(".ptabs button.on");
  return on ? on.dataset.ptab : null;
}
function showPane(name) {
  const panes = [...panel.querySelectorAll("[data-pane]")];
  if (!panes.some(p => p.dataset.pane === name)) name = panes[0] && panes[0].dataset.pane;
  panes.forEach(p => { p.hidden = p.dataset.pane !== name; });
  panel.querySelectorAll(".ptabs button").forEach(b => b.classList.toggle("on", b.dataset.ptab === name));
}
async function openRead(id, pane) {
  try {
    const data = await api("GET", "/api/jobs/" + id + "/read");
    panel.dataset.id = id;
    document.getElementById("panel-title").textContent = data.title;
    const body = document.getElementById("panel-body");
    body.innerHTML = data.html;  // escaped server-side (mdhtml, posting_html)
    body.querySelectorAll("a").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    showPane(pane || data.tab);
    panel.hidden = false;
    if (data.task && data.task.state === "running") poll(id);
  } catch (e) { toast(e.message, true); }
}
function confirmDialog(text) {
  // An in-page dialog (not window.confirm); resolves true for "Write new brief".
  const dlg = document.getElementById("confirm");
  document.getElementById("confirm-text").textContent = text;
  return new Promise(resolve => {
    const done = ok => { dlg.removeEventListener("close", onClose); if (dlg.open) dlg.close(); resolve(ok); };
    const onClose = () => done(false);  // Escape
    dlg.addEventListener("close", onClose);
    document.getElementById("confirm-yes").onclick = () => done(true);
    document.getElementById("confirm-no").onclick = () => done(false);
    dlg.showModal();
    document.getElementById("confirm-no").focus();
  });
}
document.addEventListener("click", ev => {
  const tabBtn = ev.target.closest(".tabs button");
  if (tabBtn) { showTab(tabBtn.dataset.tab); return; }
  if (ev.target.closest("#panel-close")) { panel.hidden = true; return; }
  const ptab = ev.target.closest(".ptabs button");
  if (ptab) { showPane(ptab.dataset.ptab); return; }
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const box = btn.closest("article.job, #panel");  // a card, or the Read panel
  if (!box) return;
  const id = box.dataset.id, a = btn.dataset.act, base = "/api/jobs/" + id + "/";
  if (a === "open") {
    box.querySelectorAll("form").forEach(f => { f.hidden = f.dataset.form !== btn.dataset.form || !f.hidden; });
    const f = box.querySelector('form[data-form="' + btn.dataset.form + '"]');
    if (!f.hidden) f.querySelector("input, select").focus();
  } else if (a === "rate") {
    act(box, id, base + "rate", {direction: btn.dataset.dir, note: box.querySelector('input[name="note"]').value});
  } else if (a === "read") { openRead(id); }
  else if (a === "research") {
    const go = () => act(box, id, base + "research", {});
    if (btn.dataset.confirm) confirmDialog(btn.dataset.confirm).then(ok => { if (ok) go(); }); else go();
  } else if (["unhold", "find", "shortlist"].includes(a)) { act(box, id, base + a, {}); }
});
document.addEventListener("submit", ev => {
  const form = ev.target.closest("article.job form, #panel form");
  if (!form) return;
  ev.preventDefault();
  const box = form.closest("article.job, #panel");
  act(box, box.dataset.id, "/api/jobs/" + box.dataset.id + "/" + form.dataset.form, Object.fromEntries(new FormData(form)));
});
document.addEventListener("keydown", ev => { if (ev.key === "Escape" && !document.getElementById("confirm").open) panel.hidden = true; });
document.getElementById("rated-filter").addEventListener("change", applyFilter);
try { const t = localStorage.getItem("review-tab"); if (t && document.getElementById("tab-" + t)) showTab(t); } catch (e) {}
applyFilter();
document.querySelectorAll("article.job[data-task]").forEach(a => poll(a.dataset.id));
"""


# --- the app: actions over the existing functions ------------------------------------

class App:
    def __init__(self, db_path: Path | str | None = None, profile_path: Path = DEFAULT_PROFILE_PATH, *,
                 preferred_path: Path = DEFAULT_PREFERRED_PATH, root: Path = review.ROOT,
                 research_fn: ResearchFn | None = None, find_fn: FindFn | None = None,
                 posting_fn: PostingFn | None = None, log_dir: Path | None = None,
                 today: Callable[[], date] = date.today):
        self.db_path = db_path
        self.profile_path = profile_path
        self.preferred_path = preferred_path
        self.root = root
        self.log_dir = log_dir
        self.today = today
        self.research_fn = research_fn or self._write_brief
        self.find_fn = find_fn or self._find_posting
        self.posting_fn = posting_fn or self._set_posting
        self.token = secrets.token_urlsafe(32)
        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()
        self._tasks: dict[int, dict[str, Any]] = {}  # job_id -> {"kind", "state", "message", "good"}
        self.threads: list[threading.Thread] = []
        self.backup = db.backup(db_path) if db.pending_migrations(db_path) else None
        db.init_db(db_path)

    def profile(self) -> dict[str, Any]:
        return load_profile(self.profile_path)

    def min_score(self) -> float:
        return self.profile().get("shortlist_min_score", shortlist.DEFAULT_MIN_SCORE)

    def task(self, job_id: int) -> dict[str, Any] | None:
        return self._tasks.get(job_id)

    def is_running(self, job_id: int) -> bool:
        return (self._tasks.get(job_id) or {}).get("state") == "running"

    def _lock(self, job_id: int) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(job_id, threading.Lock())

    def _job(self, job_id: int) -> dict[str, Any]:
        job = db.get_job(job_id, db_path=self.db_path)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no job with id {job_id}")
        return job

    def card_payload(self, job_id: int, message: str = "") -> dict[str, Any]:
        """The job's card and tab (None once it's on no tab, e.g. closed) and every tab's count."""
        min_score = self.min_score()
        groups = tabs(self.db_path, min_score, self.today())
        tab = next((t for t, jobs in groups.items() if any(j["id"] == job_id for j in jobs)), None)
        html_ = None
        if tab:
            job = next(j for j in groups[tab] if j["id"] == job_id)
            html_ = card(job, tab, task=self.task(job_id), min_score=min_score, db_path=self.db_path,
                         today=self.today(), root=self.root, profile=self.profile())
        return {"ok": True, "id": job_id, "message": message, "tab": tab, "card": html_,
                "counts": {t: len(jobs) for t, jobs in groups.items()}, "task": self.task(job_id)}

    def get(self, job_id: int, what: str) -> dict[str, Any]:
        job = self._job(job_id)
        if what == "card":
            return self.card_payload(job_id)
        if what == "read":
            body, tab = read_panel(job, task=self.task(job_id), min_score=self.min_score(), root=self.root,
                                   today=self.today())
            return {"ok": True, "id": job_id, "title": f"{job['company']}: {job['title']}", "html": body, "tab": tab,
                    "task": self.task(job_id)}
        if what == "task":
            task = dict(self.task(job_id) or {"kind": None, "state": "idle", "message": "", "good": False})
            return {**self.card_payload(job_id, task["message"]), **task}
        raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, f"use POST for {what}")

    def post(self, job_id: int, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action in _GETS:
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, f"use GET for {action}")
        lock = self._lock(job_id)
        if not lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "still working on the last click for this job")
        try:
            self._job(job_id)
            message = getattr(self, f"_do_{action}")(job_id, body)
        except (ValueError, LookupError, db.TransitionError, db.ActorNotAllowed, RuntimeError,
                posting.PostingError, shortlist.Refused) as exc:
            status = HTTPStatus.NOT_FOUND if isinstance(exc, LookupError) else HTTPStatus.BAD_REQUEST
            prefix = "Not shortlisted: " if isinstance(exc, shortlist.Refused) else ""
            raise ApiError(status, prefix + str(exc)) from exc
        finally:
            lock.release()
        return self.card_payload(job_id, message)

    # --- quick actions ---

    def _do_rate(self, job_id: int, body: dict[str, Any]) -> str:
        direction = _text(body, "direction")
        feedback.rate(job_id, direction, _text(body, "note"), db_path=self.db_path, profile_path=self.profile_path)
        return f"Rated {'👍' if direction == 'up' else '👎'}"

    def _do_hold(self, job_id: int, body: dict[str, Any]) -> str:
        until = _text(body, "until") or None
        if until and date.fromisoformat(until) <= self.today():
            raise ValueError("the hold date must be in the future")
        feedback.hold(job_id, _text(body, "reason"), until, db_path=self.db_path)
        return f"On hold until {until}" if until else "On hold until you unhold it"

    def _do_unhold(self, job_id: int, body: dict[str, Any]) -> str:
        feedback.unhold(job_id, db_path=self.db_path)
        return "Off hold"

    def _do_skip(self, job_id: int, body: dict[str, Any]) -> str:
        reason = _text(body, "reason")
        feedback.skip(job_id, reason, db_path=self.db_path)
        return f"Skipped: {reason.strip()}"

    def _do_applied(self, job_id: int, body: dict[str, Any]) -> str:
        method = _text(body, "method")
        if method not in APPLY_METHODS:
            raise ValueError(f"method must be one of {', '.join(APPLY_METHODS)}")
        db.mark_applied(job_id, method, _text(body, "notes").strip() or None, db_path=self.db_path)
        return f"Marked applied ({method})"

    def _do_shortlist(self, job_id: int, body: dict[str, Any]) -> str:
        if self.is_running(job_id):
            raise ValueError(f"wait for this job's task to finish: {self.task(job_id)['message']}")
        shortlist.shortlist_now(job_id, self.profile(), db_path=self.db_path, today=self.today())
        job = db.get_job(job_id, db_path=self.db_path)
        if job.get("brief_path") and _exists(job["brief_path"], self.root):  # as research() does
            db.set_status(job_id, "researched", "research", f"brief: {job['brief_path']} (already written)",
                          db_path=self.db_path)
            return "Shortlisted ✓ (its brief is already written)"
        self._start(job_id, "research", lambda: self._research_task(job_id))
        return "Shortlisted ✓ Writing its brief…"

    # --- background tasks ---

    def _do_research(self, job_id: int, body: dict[str, Any]) -> str:
        brief.candidates(job_id, self.db_path)  # ValueError/LookupError now, not in the thread
        return self._start(job_id, "research", lambda: self._research_task(job_id))

    def _do_find(self, job_id: int, body: dict[str, Any]) -> str:
        job = self._job(job_id)
        if job["status"] not in FINDABLE:
            raise ValueError(f"job {job_id} is {job['status']}; Find posting is for {', '.join(FINDABLE)} jobs")
        return self._start(job_id, "find", lambda: self.find_fn(job_id, self.db_path))

    def _do_posting(self, job_id: int, body: dict[str, Any]) -> str:
        job = self._job(job_id)
        if job["status"] not in FINDABLE:
            raise ValueError(f"job {job_id} is {job['status']}; a posting URL is for {', '.join(FINDABLE)} jobs")
        url = _text(body, "url").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("paste a full http(s) link to the posting")
        posting.check_url(url)  # a blocked domain (LinkedIn) is refused now
        return self._start(job_id, "posting", lambda: self.posting_fn(job_id, url, self.db_path))

    def _start(self, job_id: int, kind: str, fn: Callable[[], tuple[bool, str]]) -> str:
        running_msg = TASKS[kind][0]
        with self._guard:
            current = self._tasks.get(job_id)
            if current and current["state"] == "running":
                if current["kind"] == kind:
                    return "Already writing this brief" if kind == "research" else f"Already running: {running_msg}"
                raise ValueError(f"wait for this job's task to finish: {current['message']}")
            self._tasks[job_id] = {"kind": kind, "state": "running", "message": running_msg, "good": False}
        thread = threading.Thread(target=self._run, args=(job_id, kind, fn), name=f"{kind}-{job_id}", daemon=True)
        self.threads.append(thread)
        thread.start()
        return running_msg

    def _run(self, job_id: int, kind: str, fn: Callable[[], tuple[bool, str]]) -> None:
        failed = TASKS[kind][2]
        try:
            with runlog.log_to(runlog.log_path("research" if kind == "research" else "score", self.log_dir)):
                good, message = fn()
            task = {"kind": kind, "state": "done", "message": message, "good": good}
        except TaskFailed as exc:
            task = {"kind": kind, "state": "failed", "message": f"{failed}: {exc}", "good": False}
        except Exception as exc:  # the thread must always leave a final state
            log.exception("Review app: %s for job %d failed", kind, job_id)
            task = {"kind": kind, "state": "failed", "message": f"{failed}: {exc}", "good": False}
        self._tasks[job_id] = task

    def _research_task(self, job_id: int) -> tuple[bool, str]:
        result = self.research_fn(job_id, self.db_path)
        problems = list(result.errors) + ([result.stopped] if result.stopped else [])
        if problems:
            raise TaskFailed("; ".join(problems))
        return True, BRIEF_DONE

    # --- the real work behind each task (tests pass fakes) ---

    def _write_brief(self, job_id: int, db_path: Path | str | None) -> Any:
        return brief.research(job_id, profile=self.profile(), db_path=db_path)

    def _find_posting(self, job_id: int, db_path: Path | str | None) -> tuple[bool, str]:
        """The details lookup for this one job; a find is rescored by pass 2 right away."""
        result = details.fetch_details(db_path=db_path, job_ids=[job_id])
        problems = list(result.errors) + ([result.stopped] if result.stopped else [])
        if problems:
            raise TaskFailed("; ".join(problems))
        if result.closed:
            return False, "The posting says it's closed, so the job was closed"
        if not result.found:
            return False, "No posting found"
        p2 = pass2.rescore(self.profile(), load_preferred(self.preferred_path), db_path=db_path, job_ids=[job_id])
        closed = age.recheck(job_id, self.profile(), db_path=db_path, today=self.today())
        job = db.get_job(job_id, db_path=db_path)
        if closed:
            return False, f"Posting found, but it's too old, so the job was closed ({closed}; {age.label(job)})"
        message = f"{TASKS['find'][1]} ({job.get('source_trust')}, score {review._score(job)}; {age.label(job)})"
        if p2.errors or p2.stopped:
            message += "; pass 2 failed, run .\\run.ps1 pass2 later"
        return True, message

    def _set_posting(self, job_id: int, url: str, db_path: Path | str | None) -> tuple[bool, str]:
        """set-posting: fetch, store as official, cache the board, pass 2, rewrite the brief."""
        before = db.get_job(job_id, db_path=db_path)
        try:
            lines = posting.set_posting(job_id, url, profile=self.profile(),
                                        preferred=load_preferred(self.preferred_path), db_path=db_path)
        except (posting.PostingError, requests.RequestException) as exc:
            raise TaskFailed(str(exc)) from exc
        after = db.get_job(job_id, db_path=db_path)
        if after["status"] == "closed" and any(line.startswith(f"  closed: {age.TOO_OLD_PREFIX}") for line in lines):
            return False, (f"Posting stored, but it's too old, so the job was closed ({age.label(after)}); "
                           f".\\run.ps1 reopen {job_id} \"reason\" takes it back")
        message = (f"{TASKS['posting'][1]} — score {review._score(before)} → {review._score(after)}; "
                   f"{age.label(after)}")
        if any("pass 2 failed" in line for line in lines):
            message += "; pass 2 failed, run .\\run.ps1 pass2 later"
        return True, message


def _exists(path: str, root: Path) -> bool:
    p = Path(path)
    return (p if p.is_absolute() else root / p).exists()


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


# --- HTTP ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server: "ReviewServer"
    server_version = "JobReview"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("Review app: " + fmt, *args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        app = self.server.app
        try:
            if not self._host_ok():
                raise ApiError(HTTPStatus.FORBIDDEN, "wrong Host")
            path = self.path.split("?", 1)[0]
            if path == "/" and method == "GET":
                return self._send(HTTPStatus.OK, page(app).encode("utf-8"), "text/html; charset=utf-8")
            if path in ("/favicon.ico", "/favicon.svg") and method == "GET":
                return self._send(HTTPStatus.OK, FAVICON, "image/svg+xml")
            m = _API.match(path)
            if not m:
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            if not secrets.compare_digest(self.headers.get("X-Review-Token", ""), app.token):
                raise ApiError(HTTPStatus.FORBIDDEN, "missing or wrong token")
            job_id, action = int(m.group(1)), m.group(2)
            if method == "GET":
                payload = app.get(job_id, action)
            else:
                payload = app.post(job_id, action, self._body())
            self._json(HTTPStatus.OK, payload)
        except ApiError as exc:
            self._json(exc.status, {"ok": False, "error": str(exc)})
        except Exception as exc:  # never leave the browser hanging
            log.exception("Review app: %s %s failed", method, self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"server error: {exc}"})

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"{HOST}:{port}", f"localhost:{port}")

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"bad JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "send a JSON object")
        return body

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, port: int = 0):
        self.app = app
        super().__init__((HOST, port), Handler)  # the host is fixed: loopback only

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.server_address[1]}/"


def make_server(app: App, port: int = 0) -> ReviewServer:
    """Bound to 127.0.0.1; port 0 picks a free one."""
    return ReviewServer(app, port)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.review_app", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--preferred", type=Path, default=DEFAULT_PREFERRED_PATH)
    parser.add_argument("--port", type=int, default=0, help="default: a free port")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    parser.add_argument("--static", action="store_true", help="write the read-only review/index.html and exit")
    parser.add_argument("--out", type=Path, default=review.DEFAULT_OUT, help="with --static: where to write it")
    args = parser.parse_args(argv)
    if args.static:
        return review.main([*(["--db", str(args.db)] if args.db else []), "--out", str(args.out)])

    with runlog.log_to(runlog.log_path("review")) as log_file:
        app = App(args.db, args.profile, preferred_path=args.preferred)
        if app.backup:
            print(f"Database backed up before migrating: {app.backup}")
        server = make_server(app, args.port)
        print(f"Review app: {server.url} (only on this computer; Ctrl+C stops it)")
        print(f"Log: {log_file}")
        if not args.no_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopped.")
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
