import http.client
import inspect
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from agents import db, digest, feedback, review_app
from agents.research import brief
from agents.scout import shortlist

PROFILE_TEXT = "shortlist_min_score: 65\nfeedback:\n  liked_examples: []\n  disliked_examples: []\n"
DETAIL = {"pass": 2, "why": "Fits the stack.", "gaps": ["Kubernetes"], "red_flags": ["contract role"],
          "components": {"skills_match": {"value": 80}, "location": {"value": 100}}}


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE_TEXT, encoding="utf-8")
    state = {"release": threading.Event(), "fail": None, "find": (True, "Posting found ✓ (official, score 60.0)"),
             "found": [], "posted": []}
    state["release"].set()

    def fake_research(job_id, path):
        state["release"].wait(5)
        result = brief.Result(dry_run=False, candidates=1)
        if state["fail"]:
            result.errors.append(state["fail"])
            return result
        out = tmp_path / f"brief-{job_id}.md"
        out.write_text("# T\n\n## Skills match\n<script>alert(1)</script> **strong**\n", encoding="utf-8")
        db.set_brief_path(job_id, str(out), db_path=path)
        if db.get_job(job_id, db_path=path)["status"] == "shortlisted":
            db.set_status(job_id, "researched", "research", "brief", db_path=path)
        return result

    def fake_find(job_id, path):
        state["release"].wait(5)
        state["found"].append(job_id)
        if state["find"][0]:
            db.store_details(job_id, description="Found it.", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                             source_url="https://boards.greenhouse.io/acme/jobs/1", years_required=None,
                             source_trust="official", db_path=path)
        return state["find"]

    def fake_posting(job_id, url, path):
        state["release"].wait(5)
        state["posted"].append((job_id, url))
        before = db.get_job(job_id, db_path=path)["score"]
        db.store_details(job_id, description="The official text.", apply_url=url, source_url=url,
                         years_required=None, source_trust="official", db_path=path)
        db.set_score(job_id, before + 2, DETAIL, db_path=path)
        return True, f"Posting stored ✓ — score {before:.1f} → {before + 2:.1f}"

    app = review_app.App(db_path, profile, root=tmp_path, research_fn=fake_research, find_fn=fake_find,
                         posting_fn=fake_posting, log_dir=tmp_path / "logs")
    server = review_app.make_server(app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield {"db": db_path, "profile": profile, "app": app, "server": server, "state": state, "tmp": tmp_path}
    server.shutdown()
    server.server_close()


def add(env, ext, score, status="scored", trust="official", company="Acme", title="Engineer", description=None):
    job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": ext, "company": company, "title": title,
                               "url": f"https://www.linkedin.com/jobs/view/{ext}/"}, db_path=env["db"])
    db.set_status(job_id, "scored", "scout", db_path=env["db"])
    db.set_score(job_id, score, DETAIL, db_path=env["db"])
    conn = db.connect(env["db"])
    with conn:
        conn.execute("UPDATE jobs SET source_trust = ?, apply_url = ?, description = ? WHERE id = ?",
                     (trust, f"https://boards.greenhouse.io/acme/jobs/{ext}", description, job_id))
    conn.close()
    path = {"scored": [], "shortlisted": ["shortlisted"], "researched": ["shortlisted", "researched"],
            "applied": ["shortlisted", "researched", "applied"]}[status]
    for s in path:
        if s == "applied":
            db.mark_applied(job_id, "ats", db_path=env["db"])
        else:
            db.set_status(job_id, s, "user" if s == "applied" else "scout", db_path=env["db"])
    return job_id


def backdate_shortlist(env, job_id, days):
    at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = db.connect(env["db"])
    with conn:
        conn.execute("UPDATE events SET at = ? WHERE job_id = ? AND to_status = 'shortlisted'", (at, job_id))
    conn.close()


def request(env, method, path, body=None, token=True, host=None, content_type="application/json"):
    port = env["server"].server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Review-Token"] = env["app"].token if token is True else token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw


def post(env, job_id, action, body=None):
    return request(env, "POST", f"/api/jobs/{job_id}/{action}", body if body is not None else {})


# --- binding and request checks -------------------------------------------------------

def test_binds_to_loopback_only(env):
    assert env["server"].server_address[0] == "127.0.0.1"
    assert env["server"].url.startswith("http://127.0.0.1:")
    assert "host" not in inspect.signature(review_app.make_server).parameters
    other = review_app.make_server(env["app"])  # port 0: a free port, also loopback
    try:
        assert other.server_address[0] == "127.0.0.1" and other.server_address[1] not in (0, env["server"].server_address[1])
    finally:
        other.server_close()


def test_page_is_served_with_tabs_and_counts(env):
    today_id = add(env, "1", 80, "shortlisted")
    waiting_id = add(env, "2", 75, "researched", company="Older")
    backdate_shortlist(env, waiting_id, 3)
    near_id = add(env, "3", 60)
    agg_id = add(env, "4", 60, trust="aggregator", company="AggregatorCo")
    add(env, "5", 50, company="TooLow")
    nf_id = add(env, "6", 56, trust="not_found", company="NotFoundCo")
    applied_id = add(env, "7", 70, "applied", company="AppliedCo")
    status, html = request(env, "GET", "/", token=False)
    assert status == 200 and html.startswith("<!doctype html>")
    assert f'data-token="{env["app"].token}"' in html
    for tab, n in (("today", 1), ("near", 3), ("hold", 0), ("waiting", 1), ("applied", 1)):
        assert f'id="count-{tab}">{n}<' in html
    section = lambda t: html.split(f'id="tab-{t}"')[1].split("</section>")[0]
    assert f'id="job-{today_id}"' in section("today") and f'id="job-{waiting_id}"' in section("waiting")
    assert f'id="job-{near_id}"' in section("near") and f'id="job-{nf_id}"' in section("near")
    assert f'id="job-{agg_id}"' in section("near") and "TooLow" not in html
    assert f'id="job-{applied_id}"' in section("applied") and 'class="timeline"' in section("applied")
    assert 'id="rated-filter"' in html and 'data-rated="0"' in html
    card = section("today")
    for text in ("80.0", "Acme: Engineer", "Fits the stack.", "Kubernetes", "contract role", "trust-official",
                 "https://boards.greenhouse.io/acme/jobs/1", 'style="width:80%"', "not stated"):
        assert text in card


def test_wrong_host_is_refused(env):
    status, body = request(env, "GET", "/", token=False, host="evil.example:80")
    assert status == 403 and body["ok"] is False
    status, _ = request(env, "GET", "/", token=False, host=f"localhost:{env['server'].server_address[1]}")
    assert status == 200


def test_api_needs_the_token_and_json(env):
    job_id = add(env, "1", 80, "shortlisted")
    assert request(env, "POST", f"/api/jobs/{job_id}/rate", {"direction": "up"}, token=False)[0] == 403
    assert request(env, "POST", f"/api/jobs/{job_id}/rate", {"direction": "up"}, token="wrong")[0] == 403
    assert request(env, "POST", f"/api/jobs/{job_id}/rate", {"direction": "up"}, content_type="text/plain")[0] == 415
    assert request(env, "GET", f"/api/jobs/{job_id}/card", token=False)[0] == 403
    assert db.get_job(job_id, db_path=env["db"])["user_rating"] is None


def test_unknown_paths_jobs_and_methods(env):
    job_id = add(env, "1", 80, "shortlisted")
    assert request(env, "GET", "/nope")[0] == 404
    assert request(env, "GET", "/api/jobs/999/card")[0] == 404
    assert post(env, 999, "rate", {"direction": "up"})[0] == 404
    assert request(env, "GET", f"/api/jobs/{job_id}/rate")[0] == 405
    assert post(env, job_id, "card")[0] == 405


# --- actions ---------------------------------------------------------------------------

def test_card_endpoint(env):
    job_id = add(env, "1", 80, "shortlisted")
    status, data = request(env, "GET", f"/api/jobs/{job_id}/card")
    assert status == 200 and data["tab"] == "today" and f'id="job-{job_id}"' in data["card"]
    assert data["counts"]["today"] == 1


def test_rate_up_and_down_with_note(env):
    job_id = add(env, "1", 80, "shortlisted")
    status, data = post(env, job_id, "rate", {"direction": "up", "note": "great stack"})
    assert status == 200 and data["message"] == "Rated 👍" and 'data-rated="1"' in data["card"]
    assert db.get_job(job_id, db_path=env["db"])["user_rating"] == 1
    assert "great stack" in env["profile"].read_text(encoding="utf-8")
    status, data = post(env, job_id, "rate", {"direction": "down"})
    assert status == 200 and 'data-rated="-1"' in data["card"]
    assert post(env, job_id, "rate", {"direction": "sideways"})[0] == 400


def test_hold_moves_to_on_hold_and_unhold_brings_it_back(env):
    job_id = add(env, "1", 80, "shortlisted")
    until = (date.today() + timedelta(days=7)).isoformat()
    status, data = post(env, job_id, "hold", {"until": until, "reason": "after the holidays"})
    assert status == 200 and data["tab"] == "hold" and data["counts"]["today"] == 0 and data["counts"]["hold"] == 1
    assert "Unhold" in data["card"] and "after the holidays" in data["card"]
    job = db.get_job(job_id, db_path=env["db"])
    assert job["held_until"] == until and job["hold_reason"] == "after the holidays" and job["status"] == "shortlisted"
    assert [e["event"] for e in db.get_job_events(job_id, db_path=env["db"])][-1] == "held"
    status, data = post(env, job_id, "unhold")
    assert status == 200 and data["tab"] == "today" and data["counts"]["hold"] == 0
    assert db.get_job(job_id, db_path=env["db"])["hold_reason"] is None


def test_hold_without_a_date_and_bad_input(env):
    job_id = add(env, "1", 60)
    assert post(env, job_id, "hold", {"reason": " "})[0] == 400
    assert post(env, job_id, "hold", {"reason": "x", "until": date.today().isoformat()})[0] == 400
    assert post(env, job_id, "hold", {"reason": "x", "until": "next week"})[0] == 400
    status, data = post(env, job_id, "hold", {"reason": "maybe later"})
    assert status == 200 and data["tab"] == "hold" and "you unhold it" in data["card"]


def test_held_job_returns_when_held_until_passes(env):
    job_id = add(env, "1", 80, "shortlisted")
    backdate_shortlist(env, job_id, 3)
    until = date.today() + timedelta(days=2)
    db.set_hold(job_id, "busy", until.isoformat(), db_path=env["db"])
    assert request(env, "GET", f"/api/jobs/{job_id}/card")[1]["tab"] == "hold"
    env["app"].today = lambda: until  # the day it comes back
    assert request(env, "GET", f"/api/jobs/{job_id}/card")[1]["tab"] == "waiting"
    assert db.is_held(db.get_job(job_id, db_path=env["db"]), until - timedelta(days=1))
    assert not db.is_held(db.get_job(job_id, db_path=env["db"]), until)


def test_held_jobs_are_left_out_of_shortlist_waiting_and_research(env):
    held = add(env, "1", 90)
    other = add(env, "2", 80, company="Other")
    db.set_hold(held, "later", db_path=env["db"])
    result = shortlist.shortlist({"shortlist_min_score": 65}, db_path=env["db"])
    assert [j["id"] for j in result.picked] == [other]
    waiting = add(env, "3", 85, "shortlisted", company="Waiter")
    backdate_shortlist(env, waiting, 3)
    assert "Waiter" in digest.build(db_path=env["db"]).split("## Still waiting")[1].split("##")[0]
    db.set_hold(waiting, "later", db_path=env["db"])
    assert "Waiter" not in digest.build(db_path=env["db"]).split("## Still waiting")[1].split("##")[0]
    assert waiting not in [j["id"] for j in brief.candidates(db_path=env["db"])]


def test_hold_refuses_applied_jobs(env):
    job_id = add(env, "1", 80, "applied")
    status, data = post(env, job_id, "hold", {"reason": "x"})
    assert status == 400 and "can be held" in data["error"]


def test_skip_closes_with_a_reason(env):
    job_id = add(env, "1", 80, "shortlisted")
    assert post(env, job_id, "skip", {"reason": ""})[0] == 400
    status, data = post(env, job_id, "skip", {"reason": "relocation"})
    assert status == 200 and data["tab"] is None and data["card"] is None and data["counts"]["today"] == 0
    event = db.get_job_events(job_id, db_path=env["db"])[-1]
    assert (event["actor"], event["to_status"], event["detail"]) == ("user", "closed", "skipped: relocation")


def test_i_applied_marks_applied_as_the_user(env):
    job_id = add(env, "1", 80, "researched")
    assert post(env, job_id, "applied", {"method": "carrier pigeon"})[0] == 400
    status, data = post(env, job_id, "applied", {"method": "referral", "notes": "via a friend"})
    assert status == 200 and data["tab"] == "applied" and data["message"] == "Marked applied (referral)"
    job = db.get_job(job_id, db_path=env["db"])
    assert job["status"] == "applied"
    event = db.get_job_events(job_id, db_path=env["db"])[-1]
    assert (event["actor"], event["to_status"]) == ("user", "applied")
    app_row = db.get_applications(db_path=env["db"])[0]
    assert app_row["id"] == job_id
    conn = db.connect(env["db"])
    row = conn.execute("SELECT method, notes FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    assert tuple(row) == ("referral", "via a friend")


@pytest.mark.parametrize("status", ["scored", "shortlisted", "researched"])
def test_i_applied_from_any_open_status(env, status):
    job_id = add(env, "1", 60 if status == "scored" else 80, status)
    assert 'data-form="applied">✅ I applied' in request(env, "GET", f"/api/jobs/{job_id}/card")[1]["card"]
    status_code, data = post(env, job_id, "applied", {"method": "email"})
    assert status_code == 200 and data["tab"] == "applied"
    event = db.get_job_events(job_id, db_path=env["db"])[-1]
    assert (event["actor"], event["from_status"], event["to_status"]) == ("user", status, "applied")


def test_i_applied_on_a_held_job_ends_the_hold(env):
    job_id = add(env, "1", 60)
    db.set_hold(job_id, "later", db_path=env["db"])
    assert 'data-form="applied">✅ I applied' in request(env, "GET", f"/api/jobs/{job_id}/card")[1]["card"]
    status, data = post(env, job_id, "applied", {"method": "ats"})
    assert status == 200 and data["tab"] == "applied" and data["counts"]["hold"] == 0


def test_i_applied_still_follows_the_transition_rules(env):
    job_id = add(env, "1", 80, "applied")
    assert 'data-form="applied">✅ I applied' not in request(env, "GET", f"/api/jobs/{job_id}/card")[1]["card"]
    status, data = post(env, job_id, "applied", {"method": "ats"})  # applied twice
    assert status == 400 and "not allowed" in data["error"]


def test_app_backs_up_the_database_before_migrating(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("PRAGMA user_version = 17")  # one migration behind
        conn.execute("ALTER TABLE jobs DROP COLUMN posted_at_source")
    conn.close()
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE_TEXT, encoding="utf-8")
    app = review_app.App(path, profile, root=tmp_path)
    assert app.backup is not None and app.backup.parent == tmp_path / "backups"
    assert app.backup.name.startswith("jobs-") and db.pending_migrations(path) == 0
    conn = sqlite3.connect(app.backup)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 17  # the copy is from before
    conn.close()
    assert review_app.App(path, profile, root=tmp_path).backup is None  # up to date: no backup
    assert len(list((tmp_path / "backups").iterdir())) == 1


def _wait_task(env, job_id):
    for _ in range(100):
        status, data = request(env, "GET", f"/api/jobs/{job_id}/task")
        if data["state"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("task never finished")


_wait_research = _wait_task


def read(env, job_id):
    status, data = request(env, "GET", f"/api/jobs/{job_id}/read")
    assert status == 200
    return data


def test_research_runs_in_the_background_then_shows_the_brief(env):
    job_id = add(env, "1", 80, "shortlisted", description="Build things.")
    env["state"]["release"].clear()
    status, data = post(env, job_id, "research")
    assert status == 200 and data["message"] == review_app.WRITING and 'data-task="research"' in data["card"]
    assert 'class="spinner"' in data["card"] and review_app.WRITING in data["card"]
    panel = read(env, job_id)
    assert review_app.WRITING in panel["html"] and "Write brief" not in panel["html"]  # no second start
    assert request(env, "GET", f"/api/jobs/{job_id}/task")[1]["state"] == "running"
    status, again = post(env, job_id, "research")  # a second click while it runs starts nothing
    assert status == 200 and again["message"] == "Already writing this brief"
    env["state"]["release"].set()
    data = _wait_task(env, job_id)
    assert data["state"] == "done" and data["kind"] == "research" and data["message"] == "Brief updated ✓"
    assert db.get_job(job_id, db_path=env["db"])["status"] == "researched"
    assert "Brief updated ✓" in data["card"] and "spinner" not in data["card"]
    panel = read(env, job_id)
    assert panel["tab"] == "brief" and 'data-ptab="brief"' in panel["html"] and 'data-ptab="posting"' in panel["html"]
    assert 'data-confirm="Write a new brief?' in panel["html"]  # now a rewrite, in the Brief tab
    assert len(env["app"].threads) == 1


def test_research_failure_is_reported(env):
    job_id = add(env, "1", 80, "shortlisted", description="Build things.")
    env["state"]["fail"] = "job 1: brief failed validation"
    post(env, job_id, "research")
    data = _wait_task(env, job_id)
    assert data["state"] == "failed" and "brief failed validation" in data["message"]
    assert db.get_job(job_id, db_path=env["db"])["status"] == "shortlisted"
    assert '<p class="error" role="alert">Brief failed: job 1: brief failed validation</p>' in data["card"]
    assert "Write brief" in read(env, job_id)["html"]  # can try again
    assert "Brief failed: job 1" in request(env, "GET", "/", token=False)[1]  # also after a reload


def test_every_card_has_one_read_button(env):
    ids = [add(env, "1", 80, "shortlisted"), add(env, "2", 60, company="Near"), add(env, "3", 70, "applied", company="Applied")]
    html = request(env, "GET", "/", token=False)[1]
    for jid in ids:
        card = html.split(f'id="job-{jid}"')[1].split("</article>")[0]
        assert card.count('data-act="read"') == 1
        assert 'data-act="research"' not in card and 'data-act="brief"' not in card and "Read brief" not in card
    assert '<dialog id="confirm">' in html and "window.confirm(" not in html and "alert(" not in html


def test_read_with_a_brief_has_brief_and_posting_tabs(env):
    job_id = add(env, "1", 80, "researched", description="Build <b>APIs</b>.\n\nApply at https://acme.example/jobs/1.")
    out = env["tmp"] / "b.md"
    out.write_text("# T\n\n## Skills match\n<script>alert(1)</script> **strong**\n", encoding="utf-8")
    db.set_brief_path(job_id, str(out), db_path=env["db"])
    data = read(env, job_id)
    assert data["tab"] == "brief" and data["title"] == "Acme: Engineer"
    body = data["html"]
    assert body.index('data-pane="brief"') < body.index('data-pane="posting"')
    assert "<script>" not in body and "&lt;script&gt;" in body and "<strong>strong</strong>" in body
    assert ('<button data-act="research" data-confirm="Write a new brief? Takes about a minute and uses some '
            'Claude allowance.">Rewrite brief</button>') in body
    posting_pane = body.split('data-pane="posting"')[1]
    assert "Build &lt;b&gt;APIs&lt;/b&gt;." in posting_pane and "<b>" not in posting_pane
    assert '<a href="https://acme.example/jobs/1" target="_blank"' in posting_pane  # the final "." isn't in the link
    assert "https://boards.greenhouse.io/acme/jobs/1" in posting_pane and "trust-official" in posting_pane
    assert "<dt>Salary</dt><dd>not stated</dd>" in posting_pane
    assert "Write brief" not in body  # it has one


def test_read_with_a_posting_but_no_brief_opens_on_posting_with_write_brief(env):
    job_id = add(env, "1", 60, description="Build things.")
    data = read(env, job_id)
    assert data["tab"] == "posting" and 'data-pane="brief"' not in data["html"] and "ptabs" not in data["html"]
    assert data["html"].index('data-act="research" class="primary">Write brief') < data["html"].index("Build things.")
    assert "data-confirm" not in data["html"]  # a first brief needs no confirmation
    status, started = post(env, job_id, "research")  # a scored job may get a brief too
    assert status == 200 and started["message"] == review_app.WRITING
    done = _wait_task(env, job_id)
    assert done["state"] == "done" and db.get_job(job_id, db_path=env["db"])["status"] == "scored"
    assert read(env, job_id)["tab"] == "brief"


def test_posting_text_is_escaped_and_links_only_http():
    out = review_app.posting_html('<img src=x onerror=alert(1)> see javascript:alert(1) and http://a.example/x?q=1&b=2')
    assert "<img" not in out and "&lt;img" in out
    assert "javascript:alert(1)" in out and 'href="javascript' not in out
    assert '<a href="http://a.example/x?q=1&amp;b=2"' in out


def test_read_with_neither_offers_find_posting(env):
    job_id = add(env, "1", 60, trust="not_found")
    data = read(env, job_id)
    assert data["tab"] == "posting" and "No posting found yet." in data["html"]
    assert "https://www.linkedin.com/jobs/view/1/" in data["html"]  # the alert link
    assert '<button data-act="find" class="primary">Find posting</button>' in data["html"]


def test_find_posting_runs_in_the_background(env):
    job_id = add(env, "1", 60, trust="not_found")
    env["state"]["release"].clear()
    status, data = post(env, job_id, "find")
    assert status == 200 and data["message"] == review_app.TASKS["find"][0] and 'data-task="find"' in data["card"]
    assert "Find posting" not in read(env, job_id)["html"]
    env["state"]["release"].set()
    done = _wait_task(env, job_id)
    assert done["state"] == "done" and done["kind"] == "find" and done["message"].startswith("Posting found ✓")
    assert env["state"]["found"] == [job_id]
    data = read(env, job_id)
    assert "Found it." in data["html"] and "No posting found yet" not in data["html"]


def test_find_posting_not_found_and_refusals(env):
    job_id = add(env, "1", 60, trust="not_found")
    env["state"]["find"] = (False, "No posting found")
    post(env, job_id, "find")
    done = _wait_task(env, job_id)
    assert done["state"] == "done" and done["message"] == "No posting found" and "state ok" not in done["card"]
    applied = add(env, "2", 70, "applied", company="Applied")
    status, data = post(env, applied, "find")
    assert status == 400 and "Find posting is for" in data["error"]


def test_find_posting_uses_the_details_lookup_for_that_job_then_pass_2(env, monkeypatch):
    from agents.scout import details, pass2
    job_id = add(env, "1", 60, trust="not_found")
    calls = {}

    def fake_details(**kwargs):
        calls["details"] = kwargs
        result = details.Result(dry_run=False, candidates=1, found=1, official=1)
        db.store_details(job_id, description="Real posting.", apply_url="https://jobs.acme.example/1",
                         source_url="https://jobs.acme.example/1", years_required=None, source_trust="official",
                         db_path=kwargs["db_path"])
        return result

    monkeypatch.setattr(details, "fetch_details", fake_details)
    monkeypatch.setattr(pass2, "rescore", lambda *a, **k: calls.setdefault("pass2", k) and pass2.Result(dry_run=False))
    monkeypatch.setattr(review_app, "load_preferred", lambda path: [])
    good, message = review_app.App._find_posting(env["app"], job_id, env["db"])
    assert good and message.startswith("Posting found ✓ (official")
    assert calls["details"]["job_ids"] == [job_id] and calls["pass2"]["job_ids"] == [job_id]


def test_details_lookup_for_given_jobs_ignores_the_pool_and_attempts(tmp_path):
    from agents.scout import details
    path = tmp_path / "jobs.db"
    db.init_db(path)
    job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": "1", "company": "Acme", "title": "Engineer",
                               "url": "https://www.linkedin.com/jobs/view/1/"}, db_path=path)
    for _ in range(details.MAX_ATTEMPTS):  # out of retries for the daily run
        db.set_details_not_found(job_id, db_path=path)
    assert details.candidates(path) == []
    seen = []

    def runner(prompt_file, user_prompt):
        seen.append(user_prompt)
        return {"result": json.dumps({"found": False, "why": "nothing"}), "usage": {}, "modelUsage": {}}

    result = details.fetch_details(db_path=path, job_ids=[job_id], runner=runner, sleep=lambda s: None,
                                   session=_NoBoards())
    assert result.candidates == 1 and len(seen) == 1 and result.not_found == 1


class _NoBoards:
    """A requests-like session where every board answers 404."""
    def get(self, url, *args, **kwargs):
        class R:
            status_code = 404
            text = ""
            def json(self):
                return {}
        return R()


def test_paste_url_runs_set_posting_in_the_background(env):
    job_id = add(env, "1", 70, trust="aggregator", description="Aggregator copy.")
    assert 'Paste official posting URL' in read(env, job_id)["html"]
    assert post(env, job_id, "posting", {"url": "ftp://x"})[0] == 400
    status, data = post(env, job_id, "posting", {"url": "https://www.linkedin.com/jobs/view/1/"})
    assert status == 400  # a blocked domain, refused before anything runs
    env["state"]["release"].clear()
    status, data = post(env, job_id, "posting", {"url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert status == 200 and data["message"] == review_app.TASKS["posting"][0] and 'data-task="posting"' in data["card"]
    env["state"]["release"].set()
    done = _wait_task(env, job_id)
    assert done["state"] == "done" and done["message"] == "Posting stored ✓ — score 70.0 → 72.0"
    assert env["state"]["posted"] == [(job_id, "https://boards.greenhouse.io/acme/jobs/1")]
    assert done["tab"] == "near" and "Shortlist now" in done["card"] and "Needs official posting" not in done["card"]


def test_paste_url_is_offered_only_before_applying(env):
    for n, status in enumerate(("scored", "shortlisted", "researched")):
        assert "Paste official posting URL" in read(env, add(env, str(n), 70, status, company=f"C{n}"))["html"]
    applied = add(env, "9", 70, "applied", company="Applied")
    assert "Paste official posting URL" not in read(env, applied)["html"]
    status, data = post(env, applied, "posting", {"url": "https://boards.greenhouse.io/acme/jobs/9"})
    assert status == 400 and "a posting URL is for" in data["error"]


def test_set_posting_wrapper_reports_the_rescore(env, monkeypatch):
    from agents.scout import posting
    job_id = add(env, "1", 60, trust="aggregator")

    def fake_set_posting(jid, url, **kwargs):
        db.set_score(jid, 68.5, DETAIL, db_path=kwargs["db_path"])
        return ["Fetched", "  pass 2: 60.0 -> 68.5"]

    monkeypatch.setattr(posting, "set_posting", fake_set_posting)
    monkeypatch.setattr(review_app, "load_preferred", lambda path: [])
    assert review_app.App._set_posting(env["app"], job_id, "https://x.example/1", env["db"]) == \
        (True, "Posting stored ✓ — score 60.0 → 68.5; Date unknown")
    monkeypatch.setattr(posting, "set_posting", lambda *a, **k: (_ for _ in ()).throw(posting.PostingError("HTTP 404")))
    with pytest.raises(review_app.TaskFailed, match="HTTP 404"):
        review_app.App._set_posting(env["app"], job_id, "https://x.example/1", env["db"])


def test_shortlist_now_shortlists_and_writes_the_brief(env):
    job_id = add(env, "1", 70, description="Build things.")
    assert "Shortlist now" in request(env, "GET", f"/api/jobs/{job_id}/card")[1]["card"]
    status, data = post(env, job_id, "shortlist")
    assert status == 200 and data["tab"] == "today" and data["message"] == "Shortlisted ✓ Writing its brief…"
    event = [e for e in db.get_job_events(job_id, db_path=env["db"]) if e["to_status"] == "shortlisted"][-1]
    assert event["actor"] == "user"
    assert _wait_task(env, job_id)["state"] == "done"
    assert db.get_job(job_id, db_path=env["db"])["status"] == "researched"


def test_shortlist_now_with_a_brief_goes_straight_to_researched(env):
    job_id = add(env, "1", 70)
    out = env["tmp"] / "kept.md"
    out.write_text("# Kept\n", encoding="utf-8")
    db.set_brief_path(job_id, str(out), db_path=env["db"])
    status, data = post(env, job_id, "shortlist")
    assert status == 200 and "already written" in data["message"]
    assert db.get_job(job_id, db_path=env["db"])["status"] == "researched" and env["app"].threads == []


@pytest.mark.parametrize("setup, why", [
    (lambda env, j: db.set_hold(j, "later", db_path=env["db"]), "on hold"),
    (lambda env, j: _trust(env, j, "aggregator"), "no official posting"),
    (lambda env, j: db.set_score(j, 60, DETAIL, db_path=env["db"]), "below shortlist_min_score 65"),
    (lambda env, j: db.set_score(j, 70, {**DETAIL, "pass": 1}, db_path=env["db"]), "pass 2"),
    (lambda env, j: db.set_score(j, 70, {**DETAIL, "possibly_closed": True}, db_path=env["db"]), "may be closed"),
])
def test_shortlist_now_says_why_it_refuses(env, setup, why):
    job_id = add(env, "1", 70)
    setup(env, job_id)
    status, data = post(env, job_id, "shortlist")
    assert status == 400 and data["error"].startswith("Not shortlisted: ") and why in data["error"]
    assert db.get_job(job_id, db_path=env["db"])["status"] == "scored"


def test_shortlist_now_keeps_to_3_a_day(env):
    for n in range(3):
        add(env, str(n), 80 - n, "shortlisted", company=f"Today{n}")
    job_id = add(env, "9", 70)
    status, data = post(env, job_id, "shortlist")
    assert status == 400 and "3 jobs were shortlisted today already" in data["error"]


def _trust(env, job_id, trust):
    conn = db.connect(env["db"])
    with conn:
        conn.execute("UPDATE jobs SET source_trust = ? WHERE id = ?", (trust, job_id))
    conn.close()


def test_near_misses_include_aggregator_jobs_at_any_score(env):
    low = add(env, "1", 54, trust="aggregator", company="TooLow")
    mid = add(env, "2", 60, trust="aggregator", company="Mid")
    high = add(env, "3", 90, trust="aggregator", company="High")
    official = add(env, "4", 58, company="Official")
    held = add(env, "5", 80, trust="aggregator", company="Held")
    db.set_hold(held, "later", db_path=env["db"])
    html = request(env, "GET", "/", token=False)[1]
    near = html.split('id="tab-near"')[1].split("</section>")[0]
    assert "TooLow" not in near and f'id="job-{held}"' not in near
    order = [near.index(f'id="job-{j}"') for j in (high, mid, official)]
    assert order == sorted(order)  # by score
    for j in (high, mid):
        assert "Needs official posting" in near.split(f'id="job-{j}"')[1].split("</article>")[0]
    assert "Needs official posting" not in near.split(f'id="job-{official}"')[1].split("</article>")[0]
    assert 'id="count-near">3<' in html


def test_cards_show_the_brief_date(env):
    job_id = add(env, "1", 80, "researched")
    out = env["tmp"] / "dated.md"
    out.write_text("# Brief\n", encoding="utf-8")
    written = datetime(2026, 10, 18, 14, 5).timestamp()
    os.utime(out, (written, written))
    db.set_brief_path(job_id, str(out), db_path=env["db"])
    data = request(env, "GET", f"/api/jobs/{job_id}/card")[1]
    assert 'title="written 2026-10-18 14:05">Brief: Oct 18</span>' in data["card"]
    out.unlink()
    assert "Brief: file missing" in request(env, "GET", f"/api/jobs/{job_id}/card")[1]["card"]
    assert "Brief:" not in request(env, "GET", f"/api/jobs/{add(env, '2', 70, 'shortlisted')}/card")[1]["card"]


def test_favicon_is_served(env):
    port = env["server"].server_address[1]
    for path in ("/favicon.ico", "/favicon.svg"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200 and resp.getheader("Content-Type") == "image/svg+xml" and body.startswith(b"<svg")
    assert request(env, "GET", "/favicon.ico", token=False, host="evil.example:80")[0] == 403
    assert '<link rel="icon" href="/favicon.svg"' in request(env, "GET", "/", token=False)[1]


def test_research_refuses_a_closed_job(env):
    job_id = add(env, "1", 60)
    db.set_status(job_id, "closed", "user", "skipped: no", db_path=env["db"])
    status, data = post(env, job_id, "research")
    assert status == 400 and "only scored, shortlisted" in data["error"]
    assert post(env, job_id, "find")[0] == 400 and post(env, job_id, "posting", {"url": "https://a.example/1"})[0] == 400
    assert env["app"].threads == []


def test_a_second_click_while_one_runs_gets_409(env):
    job_id = add(env, "1", 80, "shortlisted")
    lock = env["app"]._lock(job_id)
    lock.acquire()
    try:
        status, data = post(env, job_id, "rate", {"direction": "up"})
        assert status == 409 and data["ok"] is False
    finally:
        lock.release()
    assert db.get_job(job_id, db_path=env["db"])["user_rating"] is None
    assert post(env, job_id, "rate", {"direction": "up"})[0] == 200


# --- the rest -------------------------------------------------------------------------

def test_hold_and_unhold_from_the_cli(env, capsys):
    job_id = add(env, "1", 60)
    assert feedback.main(["--db", str(env["db"]), "hold", str(job_id), "later", "--until", "2099-01-01"]) == 0
    assert db.get_job(job_id, db_path=env["db"])["held_until"] == "2099-01-01"
    assert feedback.main(["--db", str(env["db"]), "unhold", str(job_id)]) == 0
    assert db.get_job(job_id, db_path=env["db"])["hold_reason"] is None


def test_static_export_still_writes_index_html(env):
    out = env["tmp"] / "review" / "index.html"
    assert review_app.main(["--static", "--db", str(env["db"]), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "<script" not in text
