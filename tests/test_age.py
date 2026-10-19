"""Posting age: where dates come from, which wins, the limits, and closing old postings."""
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from agents import daily, db, digest, feedback, review_app
from agents.scout import age, details, email_alerts, posting, shortlist
from agents.scout.filters import apply_hard_filters

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date.today()
PROFILE = {"shortlist_min_score": 60, "hard_filters": {"max_posting_age_days": 10, "max_official_open_age_days": 30,
                                                       "max_official_open_age_days_preferred": 45}}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def add(db_path, ext, days=None, source="email_date", status="scored", trust=None, score=70, detail=None,
        preferred=None):
    job_id, _ = db.upsert_job({"source": "email:indeed", "external_id": ext, "company": f"Co {ext}",
                               "title": "Engineer", "url": f"https://www.indeed.com/viewjob?jk={ext}",
                               "preferred_match": preferred, "preferred_tier": "strong" if preferred else None,
                               "posted_at": ago(days) if days is not None else None,
                               "posted_at_source": source if days is not None else None}, db_path=db_path)
    if status != "new":
        db.set_status(job_id, "scored", "scout", db_path=db_path)
        db.set_score(job_id, score, detail or {"pass": 2}, db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = ? WHERE id = ?", (trust, job_id))
    conn.close()
    if status in ("shortlisted", "researched", "applied", "interviewing"):
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    if status in ("researched", "applied", "interviewing"):
        db.set_status(job_id, "researched", "research", db_path=db_path)
    if status in ("applied", "interviewing"):
        db.mark_applied(job_id, "ats", db_path=db_path)
    if status == "interviewing":
        db.set_status(job_id, "interviewing", "tracker", db_path=db_path)
    return job_id


def job(db_path, job_id):
    return db.get_job(job_id, db_path=db_path)


def message(name):
    return json.loads((FIXTURES / f"email_{name}.json").read_text(encoding="utf-8"))


# --- Alert badges -------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Just posted", timedelta(0)), ("Today", timedelta(0)), ("1 day ago", timedelta(days=1)),
    ("9 days ago", timedelta(days=9)), ("30+ days ago", timedelta(days=30)), ("10h", timedelta(hours=10)),
    ("3d", timedelta(days=3)), ("30d+", timedelta(days=30)),
    ("Actively recruiting", None), ("Easy Apply", None), ("Be first of 13 to apply", None), ("$35 - $40", None),
])
def test_age_badges(text, expected):
    assert email_alerts.age_badge(["Acme", text]) == expected


def test_indeed_digest_cards_get_dates_from_their_badges():
    m = message("indeed_jobalert_1")  # sent 2026-10-16
    jobs = [email_alerts.to_job(r, "indeed", m) for r in email_alerts.parse_indeed_digest(m)]
    assert jobs and {j["posted_at_source"] for j in jobs} == {"alert_badge"}
    assert {"2026-10-16", "2026-10-15", "2026-10-07"} <= {j["posted_at"] for j in jobs}  # just posted, 1 day, 9 days


def test_glassdoor_badges_count_back_from_the_email_time():
    m = message("glassdoor_rated_1")  # sent 2026-10-17 07:14 UTC
    jobs = {j["title"]: j for j in (email_alerts.to_job(r, "glassdoor", m) for r in email_alerts.parse_glassdoor(m))}
    assert jobs["Software Engineer (Venlum Garnexgar)"]["posted_at"] == "2026-10-16"  # "10h"
    assert jobs["Software Engineer, Tamjunlum Paxmar for Ratorka Engineering"]["posted_at"] == "2026-10-14"  # "3d"
    assert jobs["JB Engineering(Systems)"]["posted_at"] == "2026-10-17"  # "Just posted"
    assert {j["posted_at_source"] for j in jobs.values()} == {"alert_badge"}


@pytest.mark.parametrize("name,parse", [("linkedin_jobs_1", email_alerts.parse_linkedin),
                                        ("linkedin_jobalerts_2", email_alerts.parse_linkedin),
                                        ("indeed_match_1", email_alerts.parse_indeed_match)])
def test_alerts_without_badges_use_the_email_date(name, parse):
    m = message(name)
    jobs = [email_alerts.to_job(r, "x", m) for r in parse(m)]
    assert jobs and {(j["posted_at"], j["posted_at_source"]) for j in jobs} == {(m["date"][:10], "email_date")}


# --- Migration and date preference ---------------------------------------------------


def test_migration_backfills_email_jobs_as_email_date(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    email_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": "1", "company": "A", "title": "E",
                                 "posted_at": "2026-10-10"}, db_path=path)
    feed_id, _ = db.upsert_job({"source": "greenhouse", "external_id": "gh:a:1", "company": "A", "title": "E",
                                "posted_at": "2026-09-01"}, db_path=path)
    undated, _ = db.upsert_job({"source": "email:indeed", "external_id": "2", "company": "A", "title": "F"}, db_path=path)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN posted_at_source")
        conn.execute("PRAGMA user_version = 17")
    conn.close()
    db.init_db(path)
    assert [job(path, i)["posted_at_source"] for i in (email_id, feed_id, undated)] == ["email_date", "ats", None]


def test_better_dates_replace_worse_ones_never_the_reverse(db_path):
    job_id = add(db_path, "1", days=2)  # email_date
    assert db.offer_posted_at(job_id, ago(5), "email_date", db_path=db_path) is False  # alert dates never move
    assert db.offer_posted_at(job_id, ago(6), "alert_badge", db_path=db_path) is True
    assert db.offer_posted_at(job_id, ago(1), "email_date", db_path=db_path) is False
    assert db.offer_posted_at(job_id, ago(3), "alert_badge", db_path=db_path) is False
    assert db.offer_posted_at(job_id, ago(20), "posting_page", db_path=db_path) is True
    assert db.offer_posted_at(job_id, ago(4), "alert_badge", db_path=db_path) is False
    assert db.offer_posted_at(job_id, ago(21), "ats", db_path=db_path) is True  # a posting's date replaces another's
    assert (job(db_path, job_id)["posted_at"], job(db_path, job_id)["posted_at_source"]) == (ago(21), "ats")
    with pytest.raises(ValueError):
        db.offer_posted_at(job_id, ago(1), "guess", db_path=db_path)


def test_a_later_alert_with_a_badge_improves_an_email_date(db_path):
    job_id = add(db_path, "1", days=1)
    db.touch_job("email:indeed", "1", posted_at=ago(4), posted_at_source="alert_badge", db_path=db_path)
    assert (job(db_path, job_id)["posted_at"], job(db_path, job_id)["posted_at_source"]) == (ago(4), "alert_badge")
    db.upsert_job({"source": "email:indeed", "external_id": "1", "company": "Co 1", "title": "Engineer",
                   "posted_at": ago(0), "posted_at_source": "email_date"}, db_path=db_path)
    assert job(db_path, job_id)["posted_at"] == ago(4)


def test_store_details_keeps_a_better_date(db_path):
    job_id = add(db_path, "1", days=12, source="ats")
    db.store_details(job_id, description="x", apply_url=None, source_url=None, years_required=None,
                     source_trust="official", posted_at=ago(2), posted_at_source="alert_badge", db_path=db_path)
    assert job(db_path, job_id)["posted_at"] == ago(12)
    db.store_details(job_id, description="x", apply_url=None, source_url=None, years_required=None,
                     source_trust="official", posted_at=ago(3), posted_at_source="posting_page", db_path=db_path)
    assert (job(db_path, job_id)["posted_at"], job(db_path, job_id)["posted_at_source"]) == (ago(3), "posting_page")


# --- Limits and labels ----------------------------------------------------------------


def test_limits_come_from_the_profile_with_defaults():
    assert age.limits(PROFILE) == (10, 30, 45)
    assert age.limits({}) == (age.DEFAULT_MAX_AGE_DAYS, age.DEFAULT_OFFICIAL_OPEN_MAX_AGE_DAYS,
                              age.DEFAULT_PREFERRED_OPEN_MAX_AGE_DAYS) == (10, 30, 45)
    assert age.limits({"hard_filters": {"max_posting_age_days": 7, "max_official_open_age_days": 20,
                                        "max_official_open_age_days_preferred": 60}}) == (7, 20, 60)


@pytest.mark.parametrize("trust,detail,preferred,limit", [
    ("official", {"pass": 2}, None, 30),
    ("official", {"pass": 2}, "Peljunuri", 45),
    ("official", {"pass": 2, "possibly_closed": True}, "Peljunuri", 10),
    ("official", {"pass": 2, "red_flags": ["posting appears expired"]}, None, 10),
    ("aggregator", {"pass": 2}, "Peljunuri", 10),
    ("not_found", {"pass": 1}, None, 10),
])
def test_confirmed_open_official_postings_get_the_longer_limit(trust, detail, preferred, limit):
    j = {"source": "email:indeed", "source_trust": trust, "score_detail": json.dumps(detail),
         "preferred_match": preferred}
    assert age.limit_for(j, PROFILE) == limit


def test_labels():
    assert age.label({"posted_at": ago(12), "posted_at_source": "posting_page"}) == "Posted 12 days ago (from posting)"
    assert age.label({"posted_at": ago(1), "posted_at_source": "ats"}) == "Posted 1 day ago (from posting)"
    assert age.label({"posted_at": ago(3), "posted_at_source": "alert_badge"}) == "Posted ~3 days ago (alert)"
    assert age.label({"posted_at": ago(0), "posted_at_source": "alert_badge"}) == "Posted today (alert)"
    assert age.label({"posted_at": ago(2), "posted_at_source": "email_date"}) == \
        "Posted at least 2 days ago (email date, date unverified)"
    assert age.label({"posted_at": None}) == "Date unknown"
    assert age.unverified({"posted_at": ago(2), "posted_at_source": "email_date"})
    assert not age.unverified({"posted_at": ago(2), "posted_at_source": "alert_badge"})


def test_fetch_filter_uses_the_same_limits():
    profile = PROFILE
    old_alert = {"source": "email:indeed", "title": "Engineer", "posted_at": ago(30), "posted_at_source": "alert_badge"}
    assert apply_hard_filters(old_alert, profile, today=TODAY) == (False, "posting too old: posted 30 days ago (max 10)")
    feed = {**old_alert, "source": "greenhouse", "posted_at_source": "ats", "posted_at": ago(25)}  # open
    assert apply_hard_filters(feed, profile, today=TODAY)[0]
    assert not apply_hard_filters({**feed, "posted_at": ago(31)}, profile, today=TODAY)[0]
    assert apply_hard_filters({**feed, "posted_at": ago(40), "preferred_match": "Acme"}, profile, today=TODAY)[0]
    assert not apply_hard_filters({**feed, "posted_at": ago(46), "preferred_match": "Acme"}, profile, today=TODAY)[0]


# --- Closing --------------------------------------------------------------------------


def test_closes_jobs_over_their_limit(db_path):
    old_new = add(db_path, "1", days=11, status="new")
    old_scored = add(db_path, "2", days=14, source="alert_badge", trust="aggregator")
    old_shortlisted = add(db_path, "3", days=12, status="shortlisted", trust="not_found")
    old_researched = add(db_path, "4", days=50, source="ats", status="researched", trust="official")
    fresh = add(db_path, "5", days=10)
    official_open = add(db_path, "6", days=30, source="posting_page", trust="official")
    official_doubtful = add(db_path, "7", days=30, source="posting_page", trust="official", detail={"pass": 2, "possibly_closed": True})
    undated = add(db_path, "8")
    official_old = add(db_path, "9", days=31, source="ats", trust="official")
    preferred_open = add(db_path, "10", days=45, source="ats", trust="official", preferred="Co 10")
    preferred_old = add(db_path, "11", days=46, source="ats", trust="official", preferred="Co 11")
    closed = age.close_too_old(PROFILE, db_path=db_path)
    assert [j["id"] for j in closed] == [old_new, old_scored, old_shortlisted, old_researched, official_doubtful,
                                         official_old, preferred_old]
    for jid, days in [(old_new, 11), (old_scored, 14), (old_researched, 50), (official_doubtful, 30),
                      (official_old, 31), (preferred_old, 46)]:
        assert job(db_path, jid)["status"] == "closed"
        event = db.get_job_events(jid, db_path=db_path)[-1]
        assert (event["actor"], event["detail"]) == ("scout", f"posting too old: {days} days")
    assert [job(db_path, j)["status"] for j in (fresh, official_open, undated, preferred_open)] == ["scored"] * 4


def test_never_touches_applied_interviewing_or_held_jobs(db_path):
    applied = add(db_path, "1", days=60, status="applied")
    interviewing = add(db_path, "2", days=60, status="interviewing")
    held = add(db_path, "3", days=60)
    db.set_hold(held, "waiting on a referral", None, db_path=db_path)
    assert age.close_too_old(PROFILE, db_path=db_path) == []
    assert [job(db_path, j)["status"] for j in (applied, interviewing, held)] == ["applied", "interviewing", "scored"]
    assert [j["id"] for j in age.held_too_old(PROFILE, db_path=db_path)] == [held]
    assert age.note(job(db_path, held), PROFILE).endswith("— older posting, check it's still open")
    assert "older posting" not in age.note(job(db_path, applied), PROFILE)


def test_dry_run_closes_nothing(db_path, capsys, tmp_path):
    job_id = add(db_path, "1", days=20)
    assert [j["id"] for j in age.close_too_old(PROFILE, db_path=db_path, dry_run=True)] == [job_id]
    assert job(db_path, job_id)["status"] == "scored"
    profile = tmp_path / "profile.yaml"
    profile.write_text("hard_filters:\n  max_posting_age_days: 10\n", encoding="utf-8")
    assert age.main(["--db", str(db_path), "--profile", str(profile), "--dry-run"]) == 0
    assert "Would close 1 job" in capsys.readouterr().out and job(db_path, job_id)["status"] == "scored"


def test_a_reopened_job_is_left_alone(db_path):
    job_id = add(db_path, "1", days=20)
    age.close_too_old(PROFILE, db_path=db_path)
    assert feedback.main(["--db", str(db_path), "reopen", str(job_id), "still open, I checked"]) == 0
    assert age.close_too_old(PROFILE, db_path=db_path) == []
    assert job(db_path, job_id)["status"] == "scored"


def test_daily_cleanup_closes_old_postings_and_the_digest_lists_them(db_path, tmp_path):
    job_id = add(db_path, "1", days=15, source="alert_badge")
    profile = tmp_path / "profile.yaml"
    profile.write_text("shortlist_min_score: 60\nhard_filters:\n  max_posting_age_days: 10\n", encoding="utf-8")
    steps = {s: (lambda: None) for s in ("fetch", "track", "score", "details", "pass2", "shortlist", "research")}
    _, stats, _ = daily.run_daily(db_path=db_path, profile_path=profile, steps=steps, review_out=tmp_path / "r.html",
                                  out_dir=tmp_path / "digest", log_dir=tmp_path / "logs", claude_check=lambda: None)
    assert job(db_path, job_id)["status"] == "closed" and stats.cleaned_up == 1
    section = digest.build(db_path=db_path, profile=PROFILE).split("## Cleaned up today")[1].split("##")[0]
    assert f"#{job_id} **Co 1**: Engineer (70.0), posting too old: 15 days" in section


# --- Shortlist ------------------------------------------------------------------------


def test_shortlist_skips_old_postings_and_allows_unverified_dates(db_path):
    old = add(db_path, "1", days=50, source="ats", score=90, trust="official")
    unverified = add(db_path, "2", days=3, score=80, trust="official")
    picked = [j["id"] for j in shortlist.shortlist(PROFILE, db_path=db_path).picked]
    assert picked == [unverified]
    top = digest.build(db_path=db_path, profile=PROFILE).split("## Today's top 3")[1].split("## Worth checking")[0]
    assert "**Posted:** Posted at least 3 days ago (email date, date unverified)" in top
    assert old not in [j["id"] for j in shortlist.would_have_made_it([], 60, db_path=db_path, profile=PROFILE)]


def test_shortlist_now_refuses_an_old_posting(db_path):
    job_id = add(db_path, "1", days=50, source="ats", trust="official")
    with pytest.raises(shortlist.Refused, match="the posting is 50 days old \\(the limit is 30 days\\)"):
        shortlist.shortlist_now(job_id, PROFILE, db_path=db_path)
    ok = add(db_path, "2", days=2, trust="official")  # only the email date: allowed
    assert shortlist.shortlist_now(ok, PROFILE, db_path=db_path) == "scored"


# --- Better dates arriving -------------------------------------------------------------


def test_details_reads_the_posted_date_and_rechecks_age(db_path):
    job_id = add(db_path, "1", days=1, trust=None)
    found = {"found": True, "source_url": "https://boards.greenhouse.io/co1/jobs/1", "apply_url": None,
             "description": "Build things. " * 100, "posted_date": ago(60), "still_open": True}
    runner = lambda f, u: {"result": json.dumps(found), "is_error": False, "modelUsage": {}}  # noqa: E731
    session = type("S", (), {"get": lambda self, *a, **k: (_ for _ in ()).throw(details.requests.ConnectionError())})()
    result = details.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None, session=session,
                                   profile=PROFILE)
    j = job(db_path, job_id)
    assert (j["posted_at"], j["posted_at_source"]) == (ago(60), "posting_page")
    assert result.too_old == 1 and j["status"] == "closed"


def test_details_ignores_dates_from_aggregator_pages_and_future_dates(db_path):
    assert details.parse_details(json.dumps({"found": True, "source_url": "https://x.example/1", "description": "d",
                                             "posted_date": "2999-01-01"}))["posted_at"] is None
    job_id = add(db_path, "1", days=1, trust=None)
    found = {"found": True, "source_url": "https://www.builtin.com/job/1", "description": "Build things. " * 100,
             "posted_date": ago(60), "still_open": True}
    runner = lambda f, u: {"result": json.dumps(found), "is_error": False, "modelUsage": {}}  # noqa: E731
    session = type("S", (), {"get": lambda self, *a, **k: (_ for _ in ()).throw(details.requests.ConnectionError())})()
    details.fetch_details(db_path=db_path, runner=runner, sleep=lambda s: None, session=session, profile=PROFILE)
    j = job(db_path, job_id)
    assert (j["posted_at"], j["posted_at_source"], j["status"]) == (ago(1), "email_date", "scored")


def test_set_posting_closes_a_posting_too_old(db_path, monkeypatch):
    job_id = add(db_path, "1", days=1, trust="aggregator")
    page = {"title": "Engineer", "description": "Build things. " * 100, "location": None, "salary": None,
            "posted_at": ago(50), "posted_at_source": "posting_page", "office_days": None}
    monkeypatch.setattr(posting, "fetch_posting", lambda url, session=None: page)
    from agents.scout import pass2
    monkeypatch.setattr(pass2, "rescore", lambda *a, **k: pass2.Result(dry_run=False))
    lines = posting.set_posting(job_id, "https://boards.greenhouse.io/co1/jobs/1", profile=PROFILE, preferred=[],
                                db_path=db_path)
    assert "  date: Posted 50 days ago (from posting)" in lines
    assert any(line.startswith("  closed: posting too old: 50 days (limit 30 days)") for line in lines)
    assert job(db_path, job_id)["status"] == "closed"


def test_review_app_shows_dates_and_flags_old_held_jobs(db_path):
    fresh = add(db_path, "1", days=3, source="alert_badge")
    held = add(db_path, "2", days=30)
    db.set_hold(held, "later", None, db_path=db_path)
    html = review_app.card(job(db_path, fresh), "near", profile=PROFILE)
    assert "<dt>Posted</dt><dd>Posted ~3 days ago (alert)</dd>" in html
    html = review_app.card(job(db_path, held), "hold", profile=PROFILE)
    assert "date unverified" in html and "older posting, check it&#x27;s still open" in html
    panel, _ = review_app.read_panel({**job(db_path, fresh), "description": "Text."})
    assert "<dt>Posted</dt><dd>Posted ~3 days ago (alert)</dd>" in panel


def test_review_app_find_posting_closes_an_old_find(db_path, tmp_path, monkeypatch):
    job_id = add(db_path, "1", days=1, trust=None)
    profile = tmp_path / "profile.yaml"
    profile.write_text("shortlist_min_score: 60\nhard_filters:\n  max_posting_age_days: 10\n", encoding="utf-8")

    def fake_fetch(*, db_path, job_ids, **kw):
        db.store_details(job_ids[0], description="d", apply_url=None, source_url="https://boards.greenhouse.io/c/1",
                         years_required=None, source_trust="official", posted_at=ago(60),
                         posted_at_source="posting_page", db_path=db_path)
        return details.Result(dry_run=False, found=1, official=1)

    from agents.scout import pass2
    monkeypatch.setattr(details, "fetch_details", fake_fetch)
    monkeypatch.setattr(pass2, "rescore", lambda *a, **k: pass2.Result(dry_run=False))
    monkeypatch.setattr(review_app, "load_preferred", lambda path: [])
    app = review_app.App(db_path, profile, root=tmp_path, log_dir=tmp_path / "logs")
    good, text = app._find_posting(job_id, db_path)
    assert not good and text.startswith("Posting found, but it's too old, so the job was closed (posting too old: 60")
    assert job(db_path, job_id)["status"] == "closed"
