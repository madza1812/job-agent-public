"""One-time backfill of posting dates: stated in the description, ATS JSON, page metadata."""
import json
import sqlite3
from datetime import date, timedelta

import pytest

from agents import db
from agents.scout import backfill_dates as bf
from agents.scout import posting

TODAY = date.today()
PROFILE = {"hard_filters": {"max_posting_age_days": 10, "max_official_open_age_days": 30,
                            "max_official_open_age_days_preferred": 45}}
GH = "https://job-boards.greenhouse.io/acme/jobs/123"
GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"


class Resp:
    def __init__(self, status, text="", data=None):
        self.status_code, self.text, self.data = status, text, data

    def json(self):
        if self.data is None:
            raise ValueError("no JSON")
        return self.data


class FakeSession:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        r = self.routes.get(url)
        if r is None:
            return Resp(404)
        return Resp(200, data=r) if isinstance(r, (dict, list)) else Resp(200, text=r)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def add(db_path, ext, *, description="", source_url=GH, trust="official", status="scored", posted=1,
        preferred=None):
    job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": ext, "company": "Acme",
                               "title": "Engineer", "posted_at": ago(posted), "preferred_match": preferred,
                               "preferred_tier": "strong" if preferred else None}, db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, 70, {"pass": 2}, db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = ?, source_url = ?, apply_url = ?, description = ? WHERE id = ?",
                     (trust, source_url, source_url, description, job_id))
    conn.close()
    if status in ("shortlisted", "applied"):
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    if status == "applied":
        db.mark_applied(job_id, "ats", db_path=db_path)
    return job_id


def job(db_path, job_id):
    return db.get_job(job_id, db_path=db_path)


@pytest.mark.parametrize("text,expected", [
    ("Posted Aug 21, 2026", "2026-08-21"),
    ("Date posted: 2026-08-21. Apply now", "2026-08-21"),
    ("Published on 08/21/2026", "2026-08-21"),
    ("posted on 21 August 2026", "2026-08-21"),
    ("Posting date: Sept. 3rd, 2026", "2026-09-03"),
    ("Posted 2026-10-16. This is a summary", "2026-10-16"),
    ("Founded in 2012 and updated 2026-08-21", None),  # not a stated posting date
    ("Applications accepted until October 17, 2026", None),
    ("Posted Aug 21, 2999", None),  # in the future
    ("", None),
])
def test_description_dates(text, expected):
    assert bf.description_date(text, date(2026, 10, 18)) == expected


def test_page_dates_from_json_ld_and_metadata():
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "E", "datePosted": "2026-08-21T09:00:00Z"}
    assert posting.page_date(f'<script type="application/ld+json">{json.dumps(ld)}</script>') == "2026-08-21"
    assert posting.page_date('<meta property="article:published_time" content="2026-07-01T10:00:00Z">') == "2026-07-01"
    assert posting.page_date('<span itemprop="datePosted" content="2026-06-02"></span>') == "2026-06-02"
    assert posting.page_date('<time datetime="2026-06-02">June 2</time>') is None  # a bare <time> isn't a posting date
    assert posting.page_date("<p>Posted recently</p>") is None


def test_ats_json_dates():
    s = FakeSession({
        GH_API: {"id": 123, "first_published": "2026-08-21T12:00:00-04:00", "updated_at": "2026-10-01T00:00:00Z"},
        "https://api.lever.co/v0/postings/acme/0f8c9f7e-1111-2222-3333-444455556666": {"createdAt": 1787313600000},
        "https://api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [
            {"id": "aaaaaaaa-1111-2222-3333-444455556666", "publishedAt": "2026-09-30T00:00:00Z"}]},
    })
    assert bf.ats_json_date(GH, s) == "2026-08-21"
    assert bf.ats_json_date("https://jobs.lever.co/acme/0f8c9f7e-1111-2222-3333-444455556666", s) == "2026-08-21"
    assert bf.ats_json_date("https://jobs.ashbyhq.com/acme/aaaaaaaa-1111-2222-3333-444455556666", s) == "2026-09-30"
    assert bf.ats_json_date("https://jobs.ashbyhq.com/acme/bbbbbbbb-1111-2222-3333-444455556666", s) is None
    assert bf.ats_json_date("https://careers.acme.com/jobs/1", s) is None


def test_the_description_comes_first_then_the_ats_then_the_page(db_path):
    stated = add(db_path, "1", description="Engineer\nPosted Aug 21, 2026\nBuild things.")
    from_ats = add(db_path, "2")
    from_page = add(db_path, "3", source_url="https://careers.acme.com/jobs/3")
    nothing = add(db_path, "4", source_url="https://careers.acme.com/jobs/4")
    s = FakeSession({GH_API: {"first_published": "2026-09-01T00:00:00Z"},
                     "https://careers.acme.com/jobs/3": '<meta name="datePosted" content="2026-09-15">',
                     "https://careers.acme.com/jobs/4": "<html><p>No date here</p></html>"})
    rows = {r.job["id"]: r for r in bf.plan(db_path=db_path, session=s)}
    assert (rows[stated].new_date, rows[stated].new_source, rows[stated].how) == (
        "2026-08-21", "posting_page", "stated in the stored description")
    assert (rows[from_ats].new_date, rows[from_ats].new_source) == ("2026-09-01", "ats")
    assert (rows[from_page].new_date, rows[from_page].new_source) == ("2026-09-15", "posting_page")
    assert rows[nothing].new_date is None and "no date on the page" in rows[nothing].how
    assert GH_API in s.urls and s.urls.count(GH_API) == 1  # the described job made no request
    assert all(job(db_path, j)["posted_at"] == ago(1) for j in rows)  # planning writes nothing


def test_only_open_official_jobs_and_official_links(db_path):
    aggregator = add(db_path, "1", trust="aggregator", source_url="https://builtin.com/job/1")
    closed = add(db_path, "2")
    db.set_status(closed, "closed", "user", "skipped: no", db_path=db_path)
    mixed = add(db_path, "3", source_url="https://builtin.com/job/3")  # official by its apply link only
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET apply_url = ? WHERE id = ?", (GH, mixed))
    conn.close()
    s = FakeSession({GH_API: {"first_published": "2026-09-01T00:00:00Z"}})
    rows = bf.plan(db_path=db_path, session=s)
    assert [r.job["id"] for r in rows] == [mixed]
    assert "https://builtin.com/job/3" not in s.urls and rows[0].new_date == "2026-09-01"


def test_a_better_stored_date_is_not_replaced(db_path):
    job_id = add(db_path, "1", description="Posted Aug 21, 2026")
    db.offer_posted_at(job_id, "2026-09-01", "ats", db_path=db_path)
    db.offer_posted_at(job_id, "2026-09-01", "ats", db_path=db_path)
    row = bf.plan(db_path=db_path, session=FakeSession({}))[0]
    assert row.new_date == "2026-08-21" and row.stored  # a stated posting date is as good as the ATS's
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET posted_at = '2026-08-21', posted_at_source = 'posting_page' WHERE id = ?", (job_id,))
    conn.close()
    assert not bf.plan(db_path=db_path, session=FakeSession({}))[0].stored  # nothing new


def test_apply_stores_dates_logs_them_and_closes_too_old_jobs(db_path):
    old = add(db_path, "1", description=f"Posted {ago(40)}")  # official, open: limit 30
    preferred = add(db_path, "2", description=f"Posted {ago(40)}", preferred="Acme")  # limit 45
    held = add(db_path, "3", description=f"Posted {ago(90)}")
    db.set_hold(held, "later", None, db_path=db_path)
    applied = add(db_path, "4", description=f"Posted {ago(90)}", status="applied")
    fresh = add(db_path, "5", description=f"Posted {ago(5)}")
    rows = bf.plan(db_path=db_path, session=FakeSession({}))
    verdicts = {r.job["id"]: r.verdict(PROFILE, db_path, TODAY) for r in rows}
    assert verdicts == {old: "WOULD CLOSE", preferred: "ok",
                        held: "held, never closed: older posting, check it's still open",
                        applied: "never closed (applied)", fresh: "ok"}
    text = "\n".join(bf.lines(rows, PROFILE, db_path=db_path))
    assert f"old {ago(1)} (email_date) -> new {ago(40)} (posting_page)" in text and "WOULD CLOSE" in text
    closed = bf.apply(rows, PROFILE, db_path=db_path)
    assert [j["id"] for j in closed] == [old]
    assert [job(db_path, j)["status"] for j in (old, preferred, held, applied, fresh)] == [
        "closed", "scored", "scored", "applied", "scored"]
    assert (job(db_path, held)["posted_at"], job(db_path, held)["posted_at_source"]) == (ago(90), "posting_page")
    events = [e for e in db.get_job_events(fresh, db_path=db_path) if e["event"] == "posted_at_backfilled"]
    assert events and events[0]["detail"].startswith(f"{ago(1)} (email_date) -> {ago(5)} (posting_page)")


def test_dry_run_writes_nothing_not_even_the_migration(tmp_path, capsys):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    job_id = add(path, "1", description=f"Posted {ago(40)}")
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN posted_at_source")
        conn.execute("PRAGMA user_version = 17")
    conn.close()
    profile = tmp_path / "profile.yaml"
    profile.write_text("hard_filters:\n  max_posting_age_days: 10\n", encoding="utf-8")
    assert bf.main(["--db", str(path), "--profile", str(profile), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"old {ago(1)} (email_date) -> new {ago(40)} (posting_page)" in out and "WOULD CLOSE" in out
    assert db.pending_migrations(path) == 1 and job(path, job_id)["status"] == "scored"
    assert bf.main(["--db", str(path), "--profile", str(profile)]) == 0
    out = capsys.readouterr().out
    assert "Backed up the database" in out and "closed 1 job(s) as too old" in out
    assert job(path, job_id)["status"] == "closed" and list((tmp_path / "backups").iterdir())
