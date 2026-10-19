import pytest

from agents import db
from agents.scout import refilter as rf
from agents.scout.filters import dedupe_key

PROFILE = {
    "hard_filters": {
        "exclude_title_words": ["Senior"],
        "locations": ["CA"],
        "remote_ok": True,
    }
}
LOOSER = {"hard_filters": {"locations": ["CA"], "remote_ok": True}}

PROTECTED = ("shortlisted", "researched", "applied", "interviewing")
PATH_TO = {
    "scored": ["scored"],
    "shortlisted": ["scored", "shortlisted"],
    "researched": ["scored", "shortlisted", "researched"],
    "applied": ["scored", "shortlisted", "researched", "applied"],
    "interviewing": ["scored", "shortlisted", "researched", "applied", "interviewing"],
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add(db_path, ext_id, title="Software Engineer", location="San Jose, CA", status="new", company="Acme"):
    job = {"source": "greenhouse", "external_id": ext_id, "company": company, "title": title, "location": location}
    job["dedupe_key"] = dedupe_key(job)
    job_id, _ = db.upsert_job(job, db_path=db_path)
    for step in PATH_TO.get(status, []):
        db.set_status(job_id, step, "user" if step == "applied" else "scout", db_path=db_path)
    return job_id


def status(db_path, job_id):
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    finally:
        conn.close()


def last_event(db_path, job_id):
    conn = db.connect(db_path)
    try:
        return dict(conn.execute("SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone())
    finally:
        conn.close()


@pytest.mark.parametrize("start", ["new", "scored"])
def test_failing_new_and_scored_jobs_are_closed(db_path, start):
    bad = add(db_path, "1", title="Senior Software Engineer", status=start)
    good = add(db_path, "2", status=start)

    result = rf.refilter(PROFILE, db_path=db_path)

    assert [c.job_id for c in result.closed] == [bad]
    assert (status(db_path, bad), status(db_path, good)) == ("closed", start)
    ev = last_event(db_path, bad)
    assert (ev["actor"], ev["from_status"], ev["to_status"]) == ("scout", start, "closed")
    assert ev["detail"] == "filtered: title contains excluded word: 'Senior'"


@pytest.mark.parametrize("protected", PROTECTED)
def test_protected_statuses_are_never_touched(db_path, protected):
    job_id = add(db_path, "1", title="Senior Software Engineer", status=protected)
    result = rf.refilter(PROFILE, db_path=db_path)
    assert result.closed == [] and result.checked == 0
    assert status(db_path, job_id) == protected


def test_closed_for_other_reasons_is_never_reopened(db_path):
    job_id = add(db_path, "1")
    db.set_status(job_id, "closed", "scout", detail="posting removed", db_path=db_path)
    result = rf.refilter(PROFILE, db_path=db_path)
    assert result.reopened == []
    assert status(db_path, job_id) == "closed"


def test_filtered_job_reopens_when_it_passes_again(db_path):
    job_id = add(db_path, "1", title="Senior Software Engineer", status="scored")
    rf.refilter(PROFILE, db_path=db_path)
    assert status(db_path, job_id) == "closed"

    # Still failing under the same profile: stays closed.
    assert rf.refilter(PROFILE, db_path=db_path).reopened == []

    result = rf.refilter(LOOSER, db_path=db_path)
    assert [c.job_id for c in result.reopened] == [job_id]
    assert status(db_path, job_id) == "new"
    ev = last_event(db_path, job_id)
    assert (ev["actor"], ev["from_status"], ev["to_status"]) == ("scout", "closed", "new")
    assert ev["detail"].startswith("refilter: passes")


def test_reopen_skipped_when_the_same_posting_is_open(db_path):
    job_id = add(db_path, "1", title="Senior Software Engineer")
    rf.refilter(PROFILE, db_path=db_path)
    twin = add(db_path, "2", title="Senior Software Engineer")  # same dedupe_key, reposted

    result = rf.refilter(LOOSER, db_path=db_path)
    assert result.reopened == []
    assert [c.job_id for c in result.kept_closed] == [job_id]
    assert f"job {twin} is the same posting" in result.kept_closed[0].reason
    assert status(db_path, job_id) == "closed"


def test_dry_run_changes_nothing(db_path):
    to_close = add(db_path, "1", title="Senior Software Engineer")
    to_reopen = add(db_path, "2", title="Staff Engineer")
    db.set_status(to_reopen, "closed", "scout", detail="filtered: old rule", db_path=db_path)
    before = (last_event(db_path, to_close), last_event(db_path, to_reopen))

    result = rf.refilter(PROFILE, db_path=db_path, dry_run=True)

    assert [c.job_id for c in result.closed] == [to_close]
    assert [c.job_id for c in result.reopened] == [to_reopen]
    assert (status(db_path, to_close), status(db_path, to_reopen)) == ("new", "closed")
    assert (last_event(db_path, to_close), last_event(db_path, to_reopen)) == before
    assert result.lines()[0] == "Refilter (dry run, nothing changed): 2 jobs checked, 1 would close, 1 would reopen"


def test_cli_dry_run(db_path, tmp_path, capsys):
    job_id = add(db_path, "1", title="Senior Software Engineer")
    profile = tmp_path / "profile.yaml"
    profile.write_text("hard_filters:\n  exclude_title_words: [Senior]\n", encoding="utf-8")

    code = rf.main(["--db", str(db_path), "--profile", str(profile), "--log-dir", str(tmp_path / "logs"), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "1 would close" in out and "Senior Software Engineer" in out
    assert status(db_path, job_id) == "new"


def test_stale_dedupe_keys_are_refreshed(db_path):
    job_id, _ = db.upsert_job({"source": "greenhouse", "external_id": "1", "company": "Globex Global Tech",
                               "title": "Software Engineer", "location": "Sunnyvale, CA",
                               "dedupe_key": "globex global tech|software engineer|sunnyvale ca"}, db_path=db_path)

    dry = rf.refilter(PROFILE, db_path=db_path, dry_run=True)
    assert dry.rekeyed == 1 and "  1 stored dedupe keys would be refreshed" in dry.lines()
    assert db.find_open_job_by_dedupe_key("globex|software engineer|sunnyvale ca", db_path=db_path) is None

    assert rf.refilter(PROFILE, db_path=db_path).rekeyed == 1
    assert db.find_open_job_by_dedupe_key("globex|software engineer|sunnyvale ca", db_path=db_path)["id"] == job_id
    assert rf.refilter(PROFILE, db_path=db_path).rekeyed == 0
