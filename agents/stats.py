"""Weekly stats: what the agent saw and did, the input for tuning.

    python -m agents.stats [--days 7] [--json] [--db PATH] [--log-dir DIR]

Over the last N days:
  * alert emails per sender: read, jobs found, skipped (not job lists), unparsed (no
    parser could read them; unparsed_emails) and how many of those are still unparsed;
  * jobs found (first seen), filtered out at fetch by reason (filtered_postings) and by
    refilter (closes with detail "filtered: ..."), and scored;
  * scores of the jobs scored: average and the top ones;
  * jobs shortlisted, applied and skipped (with the skip reasons);
  * ratings: up/down given in the window, and, over every rated job, the average
    sub-scores of liked vs disliked jobs;
  * Claude usage per day and model, read from the `usage` lines in logs/*.log (kept 30
    days; a line copied into several logs counts once).

Sender addresses, unparsed emails, filtered postings and rating events are recorded
since migration 16; older emails show their parser type instead of an address.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents import db
from agents.common import runlog

DEFAULT_DAYS = 7
TOP_SCORES = 5
SUBSCORES = ("skills_match", "seniority_fit", "role_interest", "employer_quality", "company_pref", "location",
             "compensation")
_USAGE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ INFO agents\.common\.claude: usage (.*)$")
_FIELD = re.compile(r"\b(model|input|cache_creation|cache_read|output|web_searches|cost_usd)=(\S+)")


def collect(days: int = DEFAULT_DAYS, *, db_path: Path | str | None = None, log_dir: Path | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso = since.isoformat(timespec="seconds")
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        data = {
            "days": days,
            "since": since_iso,
            "until": now.isoformat(timespec="seconds"),
            "emails": _emails(conn, since_iso),
            "jobs": _jobs(conn, since_iso),
            "scores": _scores(conn, since_iso),
            "pipeline": _pipeline(conn, since_iso),
            "ratings": _ratings(conn, since_iso),
        }
    finally:
        conn.close()
    data["usage"] = usage_by_day(log_dir or runlog.DEFAULT_LOG_DIR, since)
    return data


def _emails(conn, since: str) -> list[dict[str, Any]]:
    senders: dict[str, dict[str, int]] = defaultdict(lambda: {"read": 0, "jobs_found": 0, "skipped": 0,
                                                              "unparsed": 0, "still_unparsed": 0})
    for r in conn.execute("SELECT COALESCE(sender, COALESCE(source, 'no parser') || ' (address not recorded)') AS who,"
                          " source, jobs_found FROM processed_emails WHERE processed_at >= ?", (since,)):
        s = senders[r["who"]]
        s["read"] += 1
        s["jobs_found"] += r["jobs_found"]
        s["skipped"] += r["source"] is None or r["jobs_found"] == 0
    for r in conn.execute("SELECT u.sender, EXISTS (SELECT 1 FROM processed_emails p WHERE p.message_id = u.message_id)"
                          " AS parsed FROM unparsed_emails u WHERE u.last_seen_at >= ?", (since,)):
        s = senders[r["sender"]]
        s["unparsed"] += 1
        s["still_unparsed"] += not r["parsed"]
        if not r["parsed"]:
            s["read"] += 1  # read but not in processed_emails
    return [{"sender": k, **v} for k, v in sorted(senders.items(), key=lambda kv: (-kv[1]["read"], kv[0]))]


def _jobs(conn, since: str) -> dict[str, Any]:
    found = Counter(r["source"] for r in conn.execute("SELECT source FROM jobs WHERE first_seen_at >= ?", (since,)))
    by_reason: dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute("SELECT reason FROM filtered_postings WHERE first_seen_at >= ?", (since,)):
        by_reason[r["reason"].split(":", 1)[0].strip()][r["reason"]] += 1
    refiltered = Counter(r["detail"][len(db.FILTERED_DETAIL_PREFIX):].strip() for r in conn.execute(
        "SELECT detail FROM events WHERE event = 'status_change' AND to_status = 'closed' AND detail LIKE 'filtered:%'"
        " AND at >= ?", (since,)))
    scored = conn.execute("SELECT COUNT(DISTINCT job_id) FROM events WHERE event = 'status_change'"
                          " AND from_status = 'new' AND to_status = 'scored' AND at >= ?", (since,)).fetchone()[0]
    return {
        "found": sum(found.values()),
        "found_by_source": dict(found.most_common()),
        "filtered_at_fetch": sum(sum(c.values()) for c in by_reason.values()),
        "filtered_by_reason": {k: {"count": sum(c.values()), "reasons": dict(c.most_common(5))}
                               for k, c in sorted(by_reason.items(), key=lambda kv: -sum(kv[1].values()))},
        "closed_by_refilter": dict(refiltered.most_common()),
        "scored": scored,
    }


def _scored_jobs(conn, since: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE score IS NOT NULL AND id IN (SELECT job_id FROM events WHERE event = 'status_change'"
        " AND to_status = 'scored' AND at >= ?) ORDER BY score DESC, id", (since,))]


def _scores(conn, since: str) -> dict[str, Any]:
    jobs = _scored_jobs(conn, since)
    values = [j["score"] for j in jobs]
    pass2 = sum(1 for j in jobs if _detail(j).get("pass") == 2)
    return {
        "count": len(values),
        "average": round(sum(values) / len(values), 1) if values else None,
        "pass2": pass2,
        "top": [{"id": j["id"], "company": j["company"], "title": j["title"], "score": j["score"],
                 "status": j["status"]} for j in jobs[:TOP_SCORES]],
    }


def _pipeline(conn, since: str) -> dict[str, Any]:
    def moved(to: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT j.id, j.company, j.title, j.score, e.at, e.detail FROM events e JOIN jobs j ON j.id = e.job_id"
            " WHERE e.event = 'status_change' AND e.to_status = ? AND e.at >= ? ORDER BY e.at", (to, since))]
    applied = [dict(r) for r in conn.execute(
        "SELECT j.id, j.company, j.title, j.score, a.applied_at AS at, a.method AS detail FROM applications a"
        " JOIN jobs j ON j.id = a.job_id WHERE a.applied_at >= ? ORDER BY a.applied_at", (since,))]
    skipped = [{**r, "reason": (r["detail"] or "")[len("skipped:"):].strip()} for r in moved("closed")
               if (r["detail"] or "").startswith("skipped:")]
    return {"shortlisted": moved("shortlisted"), "applied": applied, "skipped": skipped}


def _detail(job: dict[str, Any]) -> dict[str, Any]:
    return json.loads(job["score_detail"]) if job.get("score_detail") else {}


def _ratings(conn, since: str) -> dict[str, Any]:
    given = Counter(r["detail"] for r in conn.execute(
        "SELECT detail FROM events WHERE event = 'rated' AND at >= ?", (since,)))
    groups: dict[str, list[dict[str, Any]]] = {"liked": [], "disliked": []}
    for r in conn.execute("SELECT * FROM jobs WHERE user_rating IS NOT NULL"):
        groups["liked" if r["user_rating"] == 1 else "disliked"].append(dict(r))
    averages = {}
    for name, jobs in groups.items():
        row: dict[str, float | int | None] = {"jobs": len(jobs)}
        for key in (*SUBSCORES, "final"):
            vals = [v for v in (_subscore(j, key) for j in jobs) if v is not None]
            row[key] = round(sum(vals) / len(vals), 1) if vals else None
        averages[name] = row
    return {"up": given.get("up", 0), "down": given.get("down", 0), "all_rated": averages}


def _subscore(job: dict[str, Any], key: str) -> float | None:
    if key == "final":
        return job.get("score")
    value = (_detail(job).get("components") or {}).get(key, {}).get("value")
    return value if isinstance(value, (int, float)) else None


def usage_by_day(log_dir: Path, since: datetime) -> list[dict[str, Any]]:
    """[{day, model, calls, input, cache_creation, cache_read, output, web_searches, cost_usd}]
    from every `usage` line logged by agents.common.claude (one per call and model)."""
    since_local = since.astimezone().replace(tzinfo=None)
    seen: set[str] = set()
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(log_dir.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            m = _USAGE.match(line)
            if not m or line in seen:
                continue
            seen.add(line)
            if datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}") < since_local:
                continue
            fields = dict(_FIELD.findall(m.group(3)))
            row = rows.setdefault((m.group(1), fields.get("model", "unknown")), {
                "day": m.group(1), "model": fields.get("model", "unknown"), "calls": 0, "input": 0,
                "cache_creation": 0, "cache_read": 0, "output": 0, "web_searches": 0, "cost_usd": 0.0})
            row["calls"] += 1
            for key in ("input", "cache_creation", "cache_read", "output", "web_searches"):
                row[key] += int(fields.get(key, 0) or 0)
            row["cost_usd"] = round(row["cost_usd"] + float(fields.get("cost_usd", 0) or 0), 4)
    return [rows[k] for k in sorted(rows)]


def render(data: dict[str, Any]) -> str:
    since = datetime.fromisoformat(data["since"]).astimezone()
    until = datetime.fromisoformat(data["until"]).astimezone()
    out = [f"# Stats: last {data['days']} days ({since:%Y-%m-%d %H:%M} – {until:%Y-%m-%d %H:%M})", ""]

    out += ["## Alert emails", ""]
    if data["emails"]:
        out += ["| Sender | Read | Jobs found | Skipped | Unparsed | Still unparsed |", "|---|---:|---:|---:|---:|---:|"]
        out += [f"| {e['sender']} | {e['read']} | {e['jobs_found']} | {e['skipped']} | {e['unparsed']} | "
                f"{e['still_unparsed']} |" for e in data["emails"]]
    else:
        out.append("No alert emails read.")
    out.append("")

    j = data["jobs"]
    out += ["## Jobs", "", f"- Found: {j['found']}" + (" (" + ", ".join(f"{k} {v}" for k, v in
                                                                     j["found_by_source"].items()) + ")"
                                                          if j["found_by_source"] else ""),
            f"- Filtered out at fetch: {j['filtered_at_fetch']}"]
    for key, g in j["filtered_by_reason"].items():
        out.append(f"  - {key}: {g['count']}")
        out += [f"    - {reason}: {n}" for reason, n in g["reasons"].items() if reason != key]
    if j["closed_by_refilter"]:
        out.append(f"- Closed by refilter: {sum(j['closed_by_refilter'].values())}")
        out += [f"  - {reason}: {n}" for reason, n in j["closed_by_refilter"].items()]
    out += [f"- Scored: {j['scored']}", ""]

    s = data["scores"]
    out += ["## Scores", ""]
    if s["count"]:
        out += [f"- {s['count']} jobs scored, average {s['average']}, {s['pass2']} rescored by pass 2", "- Top:"]
        out += [f"  {n}. #{t['id']} {t['score']:.1f} **{t['company']}**: {t['title']} ({t['status']})"
                for n, t in enumerate(s["top"], 1)]
    else:
        out.append("No jobs scored.")
    out.append("")

    p = data["pipeline"]
    out += ["## Pipeline", ""]
    for key, title in (("shortlisted", "Shortlisted"), ("applied", "Applied"), ("skipped", "Skipped")):
        out.append(f"- {title}: {len(p[key])}")
        for r in p[key]:
            extra = f": {r['reason']}" if key == "skipped" else ""
            out.append(f"  - #{r['id']} **{r['company']}**: {r['title']}{extra}")
    out.append("")

    r = data["ratings"]
    out += ["## Your ratings", "", f"- In these {data['days']} days: {r['up']} up, {r['down']} down", ""]
    liked, disliked = r["all_rated"]["liked"], r["all_rated"]["disliked"]
    if liked["jobs"] or disliked["jobs"]:
        out += [f"Average sub-scores of every rated job ({liked['jobs']} liked, {disliked['jobs']} disliked):", "",
                "| Sub-score | Liked | Disliked | Gap |", "|---|---:|---:|---:|"]
        for key in (*SUBSCORES, "final"):
            a, b = liked.get(key), disliked.get(key)
            gap = f"{a - b:+.1f}" if a is not None and b is not None else ""
            out.append(f"| {key} | {_num(a)} | {_num(b)} | {gap} |")
    else:
        out.append("No rated jobs yet (`.\\run.ps1 rate <id> up|down`).")
    out.append("")

    out += ["## Claude usage per day", ""]
    if data["usage"]:
        out += ["| Day | Model | Calls | Input | Cache write | Cache read | Output | Web searches | Cost |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for u in data["usage"]:
            out.append(f"| {u['day']} | {u['model']} | {u['calls']} | {u['input']} | {u['cache_creation']} | "
                       f"{u['cache_read']} | {u['output']} | {u['web_searches']} | ${u['cost_usd']:.4f} |")
        out.append(f"| **Total** | | {sum(u['calls'] for u in data['usage'])} | | | | | | "
                   f"**${sum(u['cost_usd'] for u in data['usage']):.4f}** |")
    else:
        out.append("No Claude calls logged.")
    out.append("")
    return "\n".join(out)


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.stats", description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=_positive, default=DEFAULT_DAYS)
    parser.add_argument("--json", action="store_true", help="print the data as JSON instead of Markdown")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    args = parser.parse_args(argv)
    data = collect(args.days, db_path=args.db, log_dir=args.log_dir)
    print(json.dumps(data, indent=1, ensure_ascii=False) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
