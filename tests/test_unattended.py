import os
import time
from datetime import date, timedelta

import pytest

from agents import daily, db, digest, review
from agents.common import claude, lastrun, runlock, runlog, toast

ORDER = ["fetch", "track", "score", "details", "pass2", "cleanup", "shortlist", "research"]
TODAY = date.today()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def run(tmp_path, db_path, steps=None, **kw):
    steps = steps or {name: (lambda: None) for name in ORDER}
    return daily.run_daily(db_path=db_path, log_dir=tmp_path / "logs", out_dir=tmp_path / "digest",
                           review_out=tmp_path / "review.html", steps=steps,
                           claude_check=kw.pop("claude_check", lambda: "claude.exe"), **kw)


# Lock --------------------------------------------------------------------------------

def test_lock_blocks_a_second_run_and_is_released(tmp_path):
    path = tmp_path / "daily.lock"
    with runlock.hold(path):
        assert path.exists()
        with pytest.raises(runlock.LockBusy, match=f"pid {os.getpid()}"):
            with runlock.hold(path):
                pass
    assert not path.exists()
    with runlock.hold(path):  # free again
        pass


def test_stale_lock_is_taken_over(tmp_path):
    path = tmp_path / "daily.lock"
    path.write_text('{"pid": 1, "started_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8")
    old = time.time() - runlock.STALE_AFTER_S - 60
    os.utime(path, (old, old))
    with runlock.hold(path) as lock:
        assert str(os.getpid()) in path.read_text(encoding="utf-8")
        lock.touch()
        assert time.time() - path.stat().st_mtime < 60
    assert not path.exists()


def test_a_lock_under_two_hours_old_is_not_stale(tmp_path):
    path = tmp_path / "daily.lock"
    path.write_text("not json", encoding="utf-8")
    recent = time.time() - runlock.STALE_AFTER_S + 300
    os.utime(path, (recent, recent))
    with pytest.raises(runlock.LockBusy, match="owner unknown"):
        with runlock.hold(path):
            pass
    assert path.read_text(encoding="utf-8") == "not json"  # left alone


def test_main_exits_1_without_running_when_locked(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(daily, "run_daily", lambda **kw: ran.append(kw))
    lock = tmp_path / "daily.lock"
    with runlock.hold(lock):
        code = daily.main(["--db", str(tmp_path / "jobs.db"), "--log-dir", str(tmp_path / "logs"),
                           "--lock", str(lock), "--no-toast"])
    assert code == 1 and ran == []


# Exit code and toast -------------------------------------------------------------------

def _main(tmp_path, monkeypatch, steps):
    shown = []
    monkeypatch.setattr(daily.toast, "notify", lambda title, message: shown.append((title, message)))
    real = daily.run_daily
    monkeypatch.setattr(daily, "run_daily", lambda **kw: real(**kw, steps=steps, claude_check=lambda: None,
                                                              review_out=tmp_path / "review.html"))
    code = daily.main(["--db", str(tmp_path / "jobs.db"), "--log-dir", str(tmp_path / "logs"),
                       "--out-dir", str(tmp_path / "digest"), "--lock", str(tmp_path / "daily.lock")])
    return code, shown


def test_main_exits_0_and_toasts_new_picks_when_every_step_passes(tmp_path, monkeypatch):
    code, shown = _main(tmp_path, monkeypatch, {name: (lambda: None) for name in ORDER})
    assert code == 0
    assert shown == [("Job agent", "Job digest ready: 0 new picks")]
    assert not (tmp_path / "daily.lock").exists()


def test_main_exits_1_and_toasts_problems_when_a_step_fails(tmp_path, monkeypatch):
    steps = {name: (lambda: None) for name in ORDER}
    steps["pass2"] = lambda: daily._raise_if(["batch 1: boom"], "errors")
    code, shown = _main(tmp_path, monkeypatch, steps)
    assert code == 1
    assert shown == [("Job agent", "Daily run had problems — see digest")]


def test_toast_message_counts_this_runs_picks():
    stats = digest.RunStats(steps=[("fetch", "ok")], shortlisted=1)
    assert daily.toast_message(stats) == "Job digest ready: 1 new pick"
    stats.shortlisted = 3
    assert daily.toast_message(stats) == "Job digest ready: 3 new picks"


def test_toast_script_escapes_text_and_never_raises(monkeypatch):
    text = toast.script("Job agent", "A <b> & 'quoted' — done")
    assert "&lt;b&gt; &amp; ''quoted'' — done" in text
    assert "ToastNotificationManager" in text
    monkeypatch.setattr(toast.sys, "platform", "win32")

    def broken(*a, **k):
        raise OSError("no powershell")
    assert toast.notify("t", "m", runner=broken) is False
    calls = []

    class Done:
        returncode = 0
    assert toast.notify("t", "m", runner=lambda cmd, **k: calls.append(cmd) or Done()) is True
    assert calls[0][0].lower().endswith("powershell.exe") and "-EncodedCommand" in calls[0]
    monkeypatch.setattr(toast.sys, "platform", "linux")
    assert toast.notify("t", "m", runner=broken) is False


# Claude path ----------------------------------------------------------------------------

def test_missing_claude_fails_the_claude_steps_once_and_runs_the_rest(tmp_path, db_path):
    ran = []
    steps = {name: (lambda n=name: ran.append(n)) for name in ORDER}

    def no_claude():
        raise claude.ClaudeNotConfigured("local.yaml is missing, so claude.exe can't be found; run setup")
    _, stats, warnings = run(tmp_path, db_path, steps, claude_check=no_claude)
    assert ran == ["fetch", "cleanup", "shortlist"]
    assert dict(stats.steps) == {"fetch": "ok", "track": "failed", "score": "failed", "details": "failed", "pass2": "failed",
                                 "cleanup": "ok", "shortlist": "ok", "research": "failed", "digest": "ok", "review": "ok"}
    assert warnings == ["track, score, details, pass2, research not run: local.yaml is missing, "
                        "so claude.exe can't be found; run setup"]


# Last run line -------------------------------------------------------------------------

def test_no_run_today_yet(tmp_path, db_path):
    assert lastrun.summary(db_path) == ("No run today yet", True)
    run_id = db.start_daily_run(db_path=db_path)
    db.finish_daily_run(run_id, [("fetch", "ok")], db_path=db_path)
    assert lastrun.summary(db_path, TODAY + timedelta(days=1))[0] == "No run today yet"
    text = digest.build(db_path=db_path, today=TODAY + timedelta(days=1))
    assert text.splitlines()[2] == "**Last run:** No run today yet"


def test_last_run_line_at_the_top_of_digest_and_review(tmp_path, db_path):
    steps = {name: (lambda: None) for name in ORDER}
    steps["details"] = lambda: daily._raise_if(["job 4: boom"], "errors")
    path, _, _ = run(tmp_path, db_path, steps)
    run_row = db.last_daily_run(db_path=db_path)
    assert run_row["finished_at"] and run_row["steps"][-2:] == [("digest", "ok"), ("review", "ok")]
    line = path.read_text(encoding="utf-8").splitlines()[2]
    assert line.startswith(f"**Last run:** {TODAY.isoformat()} ") and " – " in line
    assert line.endswith("fetch ok, track ok, score ok, details with problems, pass2 ok, cleanup ok, shortlist ok, research ok")
    page = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert 'class="sub warn" id="last-run">Last run: ' in page and "details with problems" in page
    assert page.index('id="last-run"') < page.index("jobs in play")


def test_unfinished_run_says_so(db_path):
    run_id = db.start_daily_run(db_path=db_path)
    db.finish_daily_run(run_id, [("fetch", "ok")], finished=False, db_path=db_path)
    text, ok = lastrun.summary(db_path)
    assert text.endswith("– not finished · fetch ok") and ok is False
    assert 'id="last-run">Last run: ' in review.build(db_path=db_path)


# Logs ------------------------------------------------------------------------------------

def test_logs_older_than_30_days_are_deleted(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    today = date(2026, 10, 17)
    keep = ["daily-2026-10-17.log", "score-2026-09-17.log", "notes.txt"]
    drop = ["daily-2026-09-16.log", "research-2026-01-02.log"]
    for name in keep + drop + ["undated.log"]:
        (logs / name).write_text("x", encoding="utf-8")
    old = time.mktime((today - timedelta(days=40)).timetuple())
    os.utime(logs / "undated.log", (old, old))
    deleted = runlog.prune_logs(logs, today=today)
    assert sorted(p.name for p in deleted) == sorted(drop + ["undated.log"])
    assert sorted(p.name for p in logs.iterdir()) == sorted(keep)
