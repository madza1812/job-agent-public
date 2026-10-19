import sqlite3
import threading
from datetime import datetime

import pytest

from agents import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def make_job(**overrides):
    job = {
        "source": "greenhouse",
        "external_id": "12345",
        "company": "Acme",
        "title": "Data Analyst",
        "location": "Remote",
        "url": "https://boards.greenhouse.io/acme/jobs/12345",
    }
    job.update(overrides)
    return job


def advance(job_id, db_path, *statuses, actor="scout"):
    for status in statuses:
        db.set_status(job_id, status, actor, db_path=db_path)


def events_for(job_id, db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    finally:
        conn.close()


def test_init_db_creates_file_and_is_idempotent(tmp_path):
    path = tmp_path / "nested" / "jobs.db"
    db.init_db(path)
    db.init_db(path)
    assert path.exists()
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(jobs)")}
    finally:
        conn.close()
    assert {"idx_jobs_status", "idx_jobs_score"} <= indexes


def test_migration_from_v2_keeps_data_and_allows_new_sources(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    for target, sql in enumerate(db.MIGRATIONS[:2], start=1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;")
    # Seed with the v2 schema as it was, not today's upsert_job.
    t = "2026-10-01T00:00:00+00:00"
    with conn:
        old_id = conn.execute(
            "INSERT INTO jobs (source, external_id, company, title, status, first_seen_at,"
            " last_seen_at, updated_at, remote, salary_min, dedupe_key)"
            " VALUES ('greenhouse', 'greenhouse:acme:1', 'Acme', 'Engineer', 'scored', ?, ?, ?, 1, 90000,"
            " 'acme|engineer|')",
            (t, t, t),
        ).lastrowid
        for event, to_status in [("inserted", "new"), ("status_change", "scored")]:
            conn.execute(
                "INSERT INTO events (job_id, at, actor, event, to_status) VALUES (?, ?, 'scout', ?, ?)",
                (old_id, t, event, to_status),
            )
    conn.close()

    db.init_db(path)

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(jobs)")}
        events = conn.execute("SELECT COUNT(*) FROM events WHERE job_id = ?", (old_id,)).fetchone()[0]
    finally:
        conn.close()
    assert {"idx_jobs_status", "idx_jobs_score", "idx_jobs_dedupe_key", "idx_jobs_preferred_tier"} <= indexes
    assert events == 2  # inserted + status change survived the table rebuild
    [old] = db.get_jobs(db_path=path)
    assert (old["id"], old["status"], old["remote"], old["salary_min"]) == (old_id, "scored", 1, 90000)
    assert (old["salary_is_predicted"], old["preferred_match"], old["preferred_tier"]) == (0, None, None)

    new_id, inserted = db.upsert_job(
        {"source": "email:linkedin", "external_id": "email:linkedin:9", "company": "Kaiser Foundation Hospitals",
         "title": "Analyst", "needs_details": 1, "preferred_match": "Kaiser Permanente",
         "preferred_tier": "dream"},
        db_path=path,
    )
    assert inserted
    new = [j for j in db.get_jobs(db_path=path) if j["id"] == new_id][0]
    assert (new["source"], new["needs_details"], new["preferred_tier"]) == ("email:linkedin", 1, "dream")
    assert old["needs_details"] == 0


def test_upsert_rejects_unknown_tier(db_path):
    with pytest.raises(ValueError, match="preferred_tier"):
        db.upsert_job(make_job(preferred_tier="favourite"), db_path=db_path)


@pytest.mark.parametrize("source", ["monster", "email:", "linkedin", ""])
def test_upsert_rejects_unknown_source(db_path, source):
    with pytest.raises(ValueError):
        db.upsert_job(make_job(source=source), db_path=db_path)


def test_processed_emails_are_remembered(db_path):
    assert not db.is_email_processed("msg-1", db_path=db_path)
    db.record_processed_email("msg-1", "email:linkedin", 7, db_path=db_path)
    assert db.is_email_processed("msg-1", db_path=db_path)
    assert not db.is_email_processed("msg-2", db_path=db_path)
    with pytest.raises(sqlite3.IntegrityError):  # a message is recorded once
        db.record_processed_email("msg-1", "email:linkedin", 7, db_path=db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT * FROM processed_emails").fetchone()
    finally:
        conn.close()
    assert (row["message_id"], row["source"], row["jobs_found"]) == ("msg-1", "email:linkedin", 7)


def test_insert_new_job(db_path):
    job_id, inserted = db.upsert_job(make_job(), db_path=db_path)

    assert inserted is True
    [job] = db.get_jobs(db_path=db_path)
    assert job["id"] == job_id
    assert job["status"] == "new"
    assert job["score"] is None
    assert job["company"] == "Acme"
    [event] = events_for(job_id, db_path)
    assert event["event"] == "inserted"


def test_duplicate_upsert_keeps_status_and_score(db_path, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: "2026-10-01T00:00:00+00:00")
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "scored", "scout", db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET score = ? WHERE id = ?", (87.5, job_id))
    conn.close()

    monkeypatch.setattr(db, "_now", lambda: "2026-10-14T00:00:00+00:00")
    again_id, inserted = db.upsert_job(make_job(title="Renamed"), db_path=db_path)

    assert again_id == job_id
    assert inserted is False
    [job] = db.get_jobs(db_path=db_path)
    assert job["status"] == "scored"
    assert job["score"] == 87.5
    assert job["title"] == "Data Analyst"
    assert job["first_seen_at"] == "2026-10-01T00:00:00+00:00"
    assert job["last_seen_at"] == "2026-10-14T00:00:00+00:00"


def test_invalid_transition_is_rejected(db_path):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)

    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)

    [job] = db.get_jobs(db_path=db_path)
    assert job["status"] == "new"
    assert [e["event"] for e in events_for(job_id, db_path)] == ["inserted"]


_PATH_TO = {"new": (), "scored": ("scored",), "shortlisted": ("scored", "shortlisted"),
            "researched": ("scored", "shortlisted", "researched")}


@pytest.mark.parametrize("status", db.USER_APPLY_FROM)
@pytest.mark.parametrize("actor", ["scout", "research", "tracker"])
def test_non_user_cannot_set_applied(db_path, actor, status):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    advance(job_id, db_path, *_PATH_TO[status])

    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job_id, "applied", actor, db_path=db_path)

    [job] = db.get_jobs(db_path=db_path)
    assert job["status"] == status


@pytest.mark.parametrize("status", db.USER_APPLY_FROM)
def test_user_marks_applied_from_any_open_status(db_path, status):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    advance(job_id, db_path, *_PATH_TO[status])

    db.mark_applied(job_id, "referral", "met at a meetup", db_path=db_path)

    assert db.get_job(job_id, db_path=db_path)["status"] == "applied"
    last = events_for(job_id, db_path)[-1]
    assert (last["actor"], last["from_status"], last["to_status"]) == ("user", status, "applied")


def test_marking_applied_ends_a_hold(db_path):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_hold(job_id, "later", "2099-01-01", db_path=db_path)
    db.mark_applied(job_id, "ats", db_path=db_path)
    job = db.get_job(job_id, db_path=db_path)
    assert job["status"] == "applied" and job["hold_reason"] is None and job["held_until"] is None


def test_user_marks_applied_and_change_is_logged(db_path):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    advance(job_id, db_path, "scored", "shortlisted", "researched")

    db.mark_applied(job_id, "ats", db_path=db_path)

    [job] = db.get_jobs("applied", db_path=db_path)
    assert job["id"] == job_id
    last = events_for(job_id, db_path)[-1]
    assert (last["actor"], last["from_status"], last["to_status"]) == ("user", "researched", "applied")
    conn = db.connect(db_path)
    try:
        [app] = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchall()
    finally:
        conn.close()
    assert app["method"] == "ats"


@pytest.mark.parametrize("status", ["applied", "interviewing", "closed"])
def test_mark_applied_from_wrong_status_records_nothing(db_path, status):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "applied", "user", db_path=db_path)
    if status != "applied":
        db.set_status(job_id, status, "user", db_path=db_path)

    with pytest.raises(db.TransitionError):  # closed: only after a "Did you apply?" item
        db.mark_applied(job_id, "ats", db_path=db_path)

    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    finally:
        conn.close()


# Concurrency: the daily run and the review app at the same time ----------------------

def test_connections_use_wal_and_a_busy_timeout(db_path):
    conn = db.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_S * 1000
    finally:
        conn.close()


def test_a_write_waits_for_another_writer_and_readers_never_wait(db_path):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")  # the daily run, mid-write
    holder.execute("UPDATE jobs SET location = 'Elsewhere' WHERE id = ?", (job_id,))

    # A reader isn't blocked, and sees the last committed data.
    assert db.get_job(job_id, db_path=db_path)["location"] == "Remote"

    done, errors = threading.Event(), []

    def app_write():  # the review app: a rating while the daily run writes
        try:
            db.set_user_rating(job_id, 1, db_path=db_path)
        except Exception as exc:
            errors.append(exc)
        done.set()

    thread = threading.Thread(target=app_write)
    thread.start()
    assert not done.wait(0.5)  # waiting for the lock, not failing with "database is locked"
    holder.commit()
    holder.close()
    thread.join(10)
    assert errors == [] and done.is_set()
    job = db.get_job(job_id, db_path=db_path)
    assert job["user_rating"] == 1 and job["location"] == "Elsewhere"


# Backups ---------------------------------------------------------------------------

def test_pending_migrations(tmp_path):
    path = tmp_path / "jobs.db"
    assert db.pending_migrations(path) == 0  # no file yet: nothing to back up
    sqlite3.connect(path).close()
    assert db.pending_migrations(path) == len(db.MIGRATIONS)
    db.init_db(path)
    assert db.pending_migrations(path) == 0


def test_backup_names_by_minute_and_keeps_the_last_10(db_path):
    db.upsert_job(make_job(), db_path=db_path)
    made = [db.backup(db_path, now=datetime(2026, 10, 18, 9, m)) for m in range(12)]
    backups = db_path.parent / "backups"
    names = sorted(p.name for p in backups.iterdir())
    assert len(names) == 10 and names[0] == "jobs-20261018-0902.db" and names[-1] == "jobs-20261018-0911.db"
    again = db.backup(db_path, now=datetime(2026, 10, 18, 9, 11))
    assert again.name == "jobs-20261018-0911-1.db"
    conn = sqlite3.connect(made[-1])
    try:
        assert conn.execute("SELECT company FROM jobs").fetchone()[0] == "Acme"
    finally:
        conn.close()


def test_any_status_can_close_and_only_the_user_reopens(db_path):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "closed", "user", detail="posting removed", db_path=db_path)

    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "closed", "user", db_path=db_path)
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job_id, "scored", "scout", db_path=db_path)
    for status in ("shortlisted", "researched", "interviewing"):
        with pytest.raises(db.TransitionError):
            db.set_status(job_id, status, "user", db_path=db_path)
    db.set_status(job_id, "scored", "user", detail="reopened: changed my mind", db_path=db_path)


def test_sponsored_flag_is_stored(db_path):
    job_id, _ = db.upsert_job(make_job(sponsored=1, needs_details=1), db_path=db_path)
    other_id, _ = db.upsert_job(make_job(external_id="2"), db_path=db_path)
    conn = db.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT id, sponsored FROM jobs").fetchall())
    finally:
        conn.close()
    assert rows == {job_id: 1, other_id: 0}


def test_scout_reopens_a_job_closed_by_the_filters(db_path):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "closed", "scout", detail="filtered: title contains excluded word: 'senior'", db_path=db_path)

    assert [j["id"] for j in db.get_filtered_closed_jobs(db_path=db_path)] == [job_id]
    assert db.set_status(job_id, "new", "scout", detail="refilter: passes", db_path=db_path) == "closed"
    last = events_for(job_id, db_path)[-1]
    assert (last["actor"], last["from_status"], last["to_status"]) == ("scout", "closed", "new")
    assert db.get_filtered_closed_jobs(db_path=db_path) == []


@pytest.mark.parametrize("actor", ["user", "research", "tracker"])
def test_only_scout_may_reopen(db_path, actor):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "closed", "scout", detail="filtered: location outside targets", db_path=db_path)
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job_id, "new", actor, db_path=db_path)


@pytest.mark.parametrize("detail", ["posting removed", "rejection", None, "refiltered: x"])
def test_jobs_closed_for_other_reasons_cannot_reopen(db_path, detail):
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "closed", "user", detail=detail, db_path=db_path)
    assert db.get_filtered_closed_jobs(db_path=db_path) == []
    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "new", "scout", db_path=db_path)


def test_reopen_checks_the_latest_close_only(db_path):
    """Filtered, reopened, then closed for another reason: no longer reopenable."""
    job_id, _ = db.upsert_job(make_job(), db_path=db_path)
    db.set_status(job_id, "closed", "scout", detail="filtered: x", db_path=db_path)
    db.set_status(job_id, "new", "scout", db_path=db_path)
    db.set_status(job_id, "closed", "scout", detail="posting removed", db_path=db_path)
    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "new", "scout", db_path=db_path)


def test_notices_raise_clear_and_reraise(db_path, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: "2026-10-14T00:00:00+00:00")
    db.raise_notice("gmail_auth_expired", "first", db_path=db_path)
    monkeypatch.setattr(db, "_now", lambda: "2026-10-15T00:00:00+00:00")
    db.raise_notice("gmail_auth_expired", "second", db_path=db_path)
    [n] = db.get_active_notices(db_path=db_path)
    assert (n["message"], n["raised_at"]) == ("second", "2026-10-14T00:00:00+00:00")

    db.clear_notice("gmail_auth_expired", db_path=db_path)
    assert db.get_active_notices(db_path=db_path) == []

    monkeypatch.setattr(db, "_now", lambda: "2026-10-20T00:00:00+00:00")
    db.raise_notice("gmail_auth_expired", "third", db_path=db_path)
    [n] = db.get_active_notices(db_path=db_path)
    assert n["raised_at"] == "2026-10-20T00:00:00+00:00"  # a new episode


def _shortlisted_job(db_path):
    job_id, _ = db.upsert_job({"source": "greenhouse", "external_id": "pc-1", "company": "Acme", "title": "Dev"},
                              db_path=db_path)
    for s in ("scored", "shortlisted"):
        db.set_status(job_id, s, "scout", db_path=db_path)
    return job_id


def test_back_to_scored_only_for_scout_with_possibly_closed(tmp_path):
    import pytest
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    job_id = _shortlisted_job(db_path)
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job_id, "scored", "user", "possibly closed", db_path=db_path)
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job_id, "scored", "research", "possibly closed", db_path=db_path)
    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "scored", "scout", "rescore", db_path=db_path)
    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "scored", "scout", db_path=db_path)
    db.set_status(job_id, "researched", "research", db_path=db_path)
    assert db.set_status(job_id, "scored", "scout", "possibly closed", db_path=db_path) == "researched"
    assert db.get_job(job_id, db_path=db_path)["status"] == "scored"


def test_applied_jobs_never_go_back_to_scored(tmp_path):
    import pytest
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    job_id = _shortlisted_job(db_path)
    db.set_status(job_id, "researched", "research", db_path=db_path)
    db.set_status(job_id, "applied", "user", db_path=db_path)
    with pytest.raises(db.TransitionError):
        db.set_status(job_id, "scored", "scout", "possibly closed", db_path=db_path)


def test_back_to_scored_also_allowed_as_aggregator_only(tmp_path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    job_id = _shortlisted_job(db_path)
    db.set_status(job_id, "scored", "scout", "aggregator only", db_path=db_path)
    assert list(db.moved_back_at(db_path=db_path)) == [job_id]
