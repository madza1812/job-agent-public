import json

import pytest

from agents import db
from agents.scout import shortlist as sl

PROFILE = {"shortlist_min_score": 60}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, score, detail_pass=2, trust="official"):
    job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": ext_id, "company": "Acme",
                               "title": "Engineer", "url": f"https://www.linkedin.com/jobs/view/{ext_id}/"},
                              db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_score(job_id, score, {"final": score, "pass": detail_pass}, db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = ? WHERE id = ?", (trust, job_id))
    conn.close()
    return job_id


def status(db_path, job_id):
    return db.get_job(job_id, db_path=db_path)["status"]


def test_top_3_above_minimum_are_shortlisted(db_path):
    ids = {s: add(db_path, str(s), s) for s in (90, 80, 75, 70, 50)}
    result = sl.shortlist(PROFILE, db_path=db_path)
    assert [j["id"] for j in result.picked] == [ids[90], ids[80], ids[75]]
    assert status(db_path, ids[70]) == "scored"


def test_fewer_than_3_on_weak_days(db_path):
    a = add(db_path, "1", 61)
    add(db_path, "2", 59.9)
    result = sl.shortlist(PROFILE, db_path=db_path)
    assert [j["id"] for j in result.picked] == [a]


def test_default_minimum_is_60(db_path):
    add(db_path, "1", 59)
    assert sl.shortlist({}, db_path=db_path).picked == []


def test_never_shortlists_a_job_twice(db_path):
    a = add(db_path, "1", 90)
    # Shortlisted once before, then (by hand, in the db) back to scored.
    db.set_status(a, "shortlisted", "scout", db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (a,))
    conn.close()
    b = add(db_path, "2", 70)
    assert [j["id"] for j in sl.shortlist(PROFILE, db_path=db_path).picked] == [b]


def test_dry_run_changes_nothing(db_path):
    a = add(db_path, "1", 90)
    assert len(sl.shortlist(PROFILE, db_path=db_path, dry_run=True).picked) == 1
    assert status(db_path, a) == "scored"


def test_only_verified_jobs_are_shortlisted(db_path):
    no_pass2 = add(db_path, "1", 95, detail_pass=1, trust="aggregator")
    not_found = add(db_path, "2", 94, detail_pass=1, trust="not_found")
    unchecked = add(db_path, "3", 93, detail_pass=2, trust=None)
    agg = add(db_path, "4", 80, trust="aggregator")
    off = add(db_path, "5", 70, trust="official")
    picked = [j["id"] for j in sl.shortlist(PROFILE, db_path=db_path).picked]
    assert picked == [off]  # official only
    assert {status(db_path, i) for i in (no_pass2, not_found, unchecked, agg)} == {"scored"}
    worth = sl.would_have_made_it([db.get_job(off, db_path=db_path)], 60, db_path=db_path, count=5)
    assert agg in [j["id"] for j in worth]


def test_shortlisted_aggregator_job_is_moved_back_and_replaced(db_path):
    agg = add(db_path, "1", 90)
    off = add(db_path, "2", 80)
    spare = add(db_path, "3", 70)
    assert [j["id"] for j in sl.shortlist(PROFILE, db_path=db_path).picked] == [agg, off, spare]
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'aggregator' WHERE id = ?", (agg,))
    conn.close()
    nxt = add(db_path, "4", 65)
    result = sl.shortlist(PROFILE, db_path=db_path)
    assert [j["id"] for j in result.demoted] == [agg] and [j["id"] for j in result.picked] == [nxt]
    event = db.connect(db_path).execute(
        "SELECT actor, detail FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (agg,)).fetchone()
    assert tuple(event) == ("scout", "aggregator only")
    # Confirmed official later (set-posting, the board check): eligible again.
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'official' WHERE id = ?", (agg,))
    conn.close()
    assert sl.shortlist(PROFILE, db_path=db_path).picked == []  # today's 3 are full
    assert agg in {j["id"] for j in sl.shortlist(PROFILE, db_path=db_path, count=4).picked}


def test_would_have_made_it_lists_unverified_jobs_above_the_cut(db_path):
    best_unverified = add(db_path, "1", 95, detail_pass=1, trust="not_found")
    add(db_path, "2", 50, detail_pass=1, trust="not_found")  # below the minimum
    v = [add(db_path, str(10 + i), 90 - i) for i in range(3)]
    picks = sl.shortlist(PROFILE, db_path=db_path).picked
    assert [j["id"] for j in picks] == v
    assert [j["id"] for j in sl.would_have_made_it(picks, 60, db_path=db_path)] == [best_unverified]


def test_second_run_same_day_only_tops_up_to_3(db_path):
    first = [add(db_path, str(i), 90 - i) for i in range(2)]
    assert [j["id"] for j in sl.shortlist(PROFILE, db_path=db_path).picked] == first
    later = [add(db_path, str(10 + i), 80 - i) for i in range(3)]
    result = sl.shortlist(PROFILE, db_path=db_path)
    assert [j["id"] for j in result.picked] == later[:1] and result.already_today == 2
    assert sl.shortlist(PROFILE, db_path=db_path).picked == []


def flag(db_path, job_id, red_flags=(), possibly=None):
    detail = {"final": db.get_job(job_id, db_path=db_path)["score"], "pass": 2, "red_flags": list(red_flags)}
    if possibly is not None:
        detail["possibly_closed"] = possibly
    db.set_score(job_id, detail["final"], detail, db_path=db_path)


@pytest.mark.parametrize("red_flags, possibly, expected", [
    ([], True, True),
    (["posting shown as closed on aggregators"], None, True),
    (["no longer accepting applications"], None, True),
    (["on-call rotation"], False, False),
    (["closed-source stack"], None, False),
])
def test_possibly_closed(db_path, red_flags, possibly, expected):
    job_id = add(db_path, "1", 80)
    flag(db_path, job_id, red_flags, possibly)
    assert sl.possibly_closed(db.get_job(job_id, db_path=db_path)) is expected


def test_possibly_closed_jobs_are_never_shortlisted(db_path):
    closed = add(db_path, "1", 95)
    flag(db_path, closed, possibly=True)
    ok = add(db_path, "2", 70)
    assert [j["id"] for j in sl.shortlist(PROFILE, db_path=db_path).picked] == [ok]


@pytest.mark.parametrize("status", ["shortlisted", "researched"])
def test_shortlisted_job_found_possibly_closed_is_replaced_the_same_day(db_path, status):
    ids = [add(db_path, str(i), 90 - i) for i in range(4)]
    assert [j["id"] for j in sl.shortlist(PROFILE, db_path=db_path).picked] == ids[:3]
    if status == "researched":
        db.set_status(ids[1], "researched", "research", db_path=db_path)
    flag(db_path, ids[1], ["posting shown as closed"])

    result = sl.shortlist(PROFILE, db_path=db_path)
    assert [j["id"] for j in result.demoted] == [ids[1]]
    assert [j["id"] for j in result.picked] == [ids[3]] and result.already_today == 2
    job = db.get_job(ids[1], db_path=db_path)
    assert job["status"] == "scored" and job["score"] == 89  # the score stays
    event = db.connect(db_path).execute(
        "SELECT actor, from_status, detail FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (ids[1],)).fetchone()
    assert tuple(event) == ("scout", status, "possibly closed")
    # It shows under "Worth checking yourself", not again in the shortlist.
    top = [db.get_job(i, db_path=db_path) for i in (ids[0], ids[2], ids[3])]
    assert [j["id"] for j in sl.would_have_made_it(top, 60, db_path=db_path)] == [ids[1]]
    assert sl.shortlist(PROFILE, db_path=db_path).picked == []


def test_dry_run_moves_nothing_back(db_path):
    a = add(db_path, "1", 90)
    sl.shortlist(PROFILE, db_path=db_path)
    flag(db_path, a, possibly=True)
    result = sl.shortlist(PROFILE, db_path=db_path, dry_run=True)
    assert len(result.demoted) == 1 and status(db_path, a) == "shortlisted"


def test_job_moved_back_today_is_listed_even_when_outranked(db_path):
    ids = [add(db_path, str(i), 80 - i) for i in range(4)]
    sl.shortlist(PROFILE, db_path=db_path)
    flag(db_path, ids[1], possibly=True)
    unverified = [add(db_path, str(10 + i), 95 - i, detail_pass=1, trust="not_found") for i in range(3)]
    picks = [db.get_job(i, db_path=db_path) for i in (ids[0], ids[2])] + sl.shortlist(PROFILE, db_path=db_path).picked
    listed = [j["id"] for j in sl.would_have_made_it(picks, 60, db_path=db_path)]
    assert listed == unverified + [ids[1]]


def test_aggregator_job_moved_back_today_is_listed_even_when_outranked(db_path):
    agg = add(db_path, "1", 80)
    sl.shortlist(PROFILE, db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'aggregator' WHERE id = ?", (agg,))
    conn.close()
    unverified = [add(db_path, str(10 + i), 95 - i, detail_pass=1, trust="not_found") for i in range(3)]
    picks = sl.shortlist(PROFILE, db_path=db_path).picked
    listed = [j["id"] for j in sl.would_have_made_it(picks, 60, db_path=db_path)]
    assert listed == unverified + [agg]


def test_recruiter_pools_are_never_shortlisted_nor_worth_checking(db_path):
    pool = add(db_path, "1", 95)
    titled = add(db_path, "2", 93)
    job = add(db_path, "3", 70)
    db.set_score(pool, 95, {"final": 95, "pass": 2, "posting_type": "talent_pool"}, db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET title = 'General Application - Engineering' WHERE id = ?", (titled,))
    conn.close()
    result = sl.shortlist(PROFILE, db_path=db_path)
    assert [j["id"] for j in result.picked] == [job]
    assert sl.would_have_made_it(result.picked, 60, db_path=db_path) == []


@pytest.mark.parametrize("title,posting_type,expected", [
    ("Engineer", "job", False),
    ("Engineer", None, False),               # pass 1 only
    ("Engineer", "general_application", True),
    ("Join our Talent Network", None, True),
    ("Future Opportunities - Software", None, True),
    ("Talent Pool: Backend", "job", True),   # the title alone settles it
])
def test_is_recruiter_pool(title, posting_type, expected):
    detail = {"pass": 2, **({"posting_type": posting_type} if posting_type else {})}
    assert sl.is_recruiter_pool({"title": title, "score_detail": json.dumps(detail)}) is expected
