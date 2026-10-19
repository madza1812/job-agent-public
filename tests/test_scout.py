import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents import db
from agents.common import gmail
from agents.scout import email_alerts, fetch, fetchers

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

PROFILE = {
    "targets": {"roles": ["Backend Engineer", "Data Analyst"]},
    "hard_filters": {
        "locations": ["California", "Texas", "Washington", "Remote-US"],
        "location_aliases": {
            "California": ["CA", "San Francisco", "Oakland"],
            "Texas": ["TX", "Austin"],
            "Washington": ["WA", "Seattle"],
        },
        "remote_ok": True,
        "min_salary": 70000,
        "exclude_title_words": ["director"],
        "exclude_companies": [],
        "max_posting_age_days": None,
    },
}

PREFERRED = [
    {"name": "Kaiser Permanente", "aliases": ["Kaiser Foundation Hospitals"], "tier": "dream", "why": ""},
    {"name": "Abbott", "aliases": ["Abbott Laboratories"], "tier": "strong", "why": ""},
    {"name": "Ramp", "aliases": [], "tier": "interested", "poll_feed": True, "ats": "ashby", "slug": "ramp"},
]
NO_FEEDS = [{k: v for k, v in c.items() if k not in ("poll_feed", "ats", "slug")} for c in PREFERRED]

# Ramp fixture: 3 pass (the London and Toronto postings fail).
RAMP_FETCHED, RAMP_PASSED = 5, 3

# The runner is tested with a stand-in "testalert" sender whose email body is the
# parser output as JSON. The real sender parsers are tested in test_email_alerts.py.
ALERT_SENDER = "alerts@testalert.example"
KAISER = {"title": "Backend Engineer", "company": "Kaiser Foundation Hospitals",
          "location": "Oakland, CA", "url": "https://jobs.testalert.example/view/101?utm_source=alert", "native_id": "101"}
ABBOTT = {"title": "Data Analyst", "company": "Abbott Laboratories",
          "location": "Remote", "url": "https://jobs.testalert.example/view/102", "native_id": "102"}
NYC = {"title": "Data Analyst", "company": "Big Bank",
       "location": "New York, NY", "url": "https://jobs.testalert.example/view/103", "native_id": "103"}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def alert(message_id, jobs, sender=ALERT_SENDER, date="2026-10-14T05:00:00-07:00"):
    return {"id": message_id, "sender": sender, "sender_name": "", "subject": f"alert {message_id}",
            "date": date, "html": json.dumps(jobs), "text": ""}


@pytest.fixture(autouse=True)
def test_parser(monkeypatch):
    def parse(message):
        if message["html"] == "BROKEN":
            raise ValueError("layout changed")
        return json.loads(message["html"])

    monkeypatch.setitem(email_alerts.SENDERS, ALERT_SENDER, ("testalert", parse))


class FakeMailbox:
    def __init__(self, messages=()):
        self.messages = {m["id"]: m for m in messages}
        self.queries = []
        self.fetched = []
        self.broken = False
        self.expired = False

    def list_messages(self, query):
        self.queries.append(query)
        if self.expired:
            raise gmail.GmailAuthExpired("Gmail login expired or was revoked (invalid_grant)")
        if self.broken:
            raise RuntimeError("Gmail unreachable")
        return list(self.messages)

    def get_message(self, message_id):
        self.fetched.append(message_id)
        return copy.deepcopy(self.messages[message_id])


class FakeFeeds:
    """Serves the saved Ashby fixture through the real parser."""

    def __init__(self):
        self.ramp = load("ashby_ramp.json")
        self.calls = []
        self.broken = False

    def remove_ramp(self, native_id):
        self.ramp["jobs"] = [p for p in self.ramp["jobs"] if p["id"] != native_id]

    def fetcher_map(self):
        def ashby(slug, company=None):
            self.calls.append(slug)
            if self.broken:
                raise fetchers.FetchError("GET ashby returned HTTP 503")
            return fetchers.parse_ashby(copy.deepcopy(self.ramp), slug, company)

        return {"ashby": ashby}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "jobs.db"


@pytest.fixture
def feeds():
    return FakeFeeds()


@pytest.fixture
def mailbox():
    return FakeMailbox([alert("m1", [KAISER, NYC]), alert("m2", [ABBOTT, KAISER])])


def run_at(when, db_path, monkeypatch, *, mailbox=None, feeds=None, preferred=PREFERRED, sleeps=None, since_days=3):
    monkeypatch.setattr(db, "_now", lambda: when.isoformat(timespec="seconds"))
    return fetch.run(
        PROFILE,
        preferred,
        mailbox=mailbox,
        since_days=since_days,
        db_path=db_path,
        fetcher_map=(feeds or FakeFeeds()).fetcher_map(),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        now=when,
    )


def job(db_path, external_id):
    [j] = [j for j in db.get_jobs(db_path=db_path) if j["external_id"] == external_id]
    return j


def processed(db_path):
    conn = db.connect(db_path)
    try:
        return {r["message_id"]: (r["source"], r["jobs_found"]) for r in conn.execute("SELECT * FROM processed_emails")}
    finally:
        conn.close()


# --- Email alerts ------------------------------------------------------------------


def test_email_only_run(mailbox, db_path, monkeypatch):
    s = run_at(T0, db_path, monkeypatch, mailbox=mailbox, preferred=NO_FEEDS)

    assert mailbox.queries == ["label:JobAlerts newer_than:3d"]
    # Kaiser is in both emails and counted once; New York fails the location filter.
    assert (s.fetched, s.passed, s.new, s.new_preferred, s.errors) == (3, 2, 2, 2, [])
    assert s.rejections == {"location outside target states": 1}

    kp = job(db_path, "email:testalert:101")
    assert (kp["source"], kp["needs_details"], kp["description"]) == ("email:testalert", 1, "")
    assert (kp["preferred_match"], kp["preferred_tier"]) == ("Kaiser Permanente", "dream")
    assert kp["url"] == "https://jobs.testalert.example/view/101"  # tracking parameter stripped
    assert kp["posted_at"] == "2026-10-14"  # from the email date, in UTC
    assert job(db_path, "email:testalert:102")["remote"] == 1

    assert processed(db_path) == {"m1": ("email:testalert", 2), "m2": ("email:testalert", 2)}
    st = s.senders["testalert"]
    assert (st.emails_read, st.jobs_found, st.unparsed) == (2, 4, 0)
    assert f"  email testalert ({ALERT_SENDER}): 2 read / 4 jobs found / 0 unparsed" in s.lines()


def test_emails_are_never_parsed_twice(mailbox, db_path, monkeypatch):
    run_at(T0, db_path, monkeypatch, mailbox=mailbox, preferred=NO_FEEDS)
    mailbox.messages["m3"] = alert("m3", [KAISER])
    mailbox.fetched.clear()

    s = run_at(T0 + timedelta(hours=6), db_path, monkeypatch, mailbox=mailbox, preferred=NO_FEEDS)

    assert mailbox.fetched == ["m3"]
    assert (s.emails_already_processed, s.fetched, s.new, s.updated) == (2, 1, 0, 1)
    assert job(db_path, "email:testalert:101")["last_seen_at"].startswith("2026-10-14T18")


def test_sender_without_parser_is_logged_not_failed(db_path, monkeypatch, caplog):
    box = FakeMailbox([alert("m1", [KAISER]), alert("x1", [], sender="news@unknownboard.example")])
    s = run_at(T0, db_path, monkeypatch, mailbox=box, preferred=NO_FEEDS)

    assert s.errors == []
    assert "No parser for job alert sender news@unknownboard.example" in caplog.text
    assert (s.senders["news@unknownboard.example"].emails_read, s.senders["news@unknownboard.example"].unparsed) == (1, 1)
    assert "  email news@unknownboard.example: 1 read / 0 jobs found / 1 unparsed" in s.lines()
    # Not recorded, so a parser added later still gets to read it.
    assert set(processed(db_path)) == {"m1"}


def test_parser_failure_and_empty_result_are_retried_next_run(db_path, monkeypatch):
    box = FakeMailbox([alert("bad", []), alert("empty", [])])
    box.messages["bad"]["html"] = "BROKEN"
    s = run_at(T0, db_path, monkeypatch, mailbox=box, preferred=NO_FEEDS)

    assert s.senders["testalert"].unparsed == 2
    assert len(s.errors) == 1 and "layout changed" in s.errors[0]
    assert processed(db_path) == {}

    box.messages["bad"]["html"] = json.dumps([KAISER])
    s = run_at(T0 + timedelta(hours=1), db_path, monkeypatch, mailbox=box, preferred=NO_FEEDS)
    assert s.new == 1 and set(processed(db_path)) == {"bad"}


def test_since_days_sets_the_search_window(mailbox, db_path, monkeypatch):
    run_at(T0, db_path, monkeypatch, mailbox=mailbox, since_days=7)
    assert mailbox.queries == ["label:JobAlerts newer_than:7d"]


def test_real_alert_emails_end_to_end(db_path, monkeypatch):
    """The saved real (sanitized) emails through the real parsers and the runner."""
    box = FakeMailbox([load(f"email_{n}.json") for n in (
        "linkedin_jobalerts_1", "linkedin_jobalerts_2", "linkedin_jobs_1", "linkedin_jobs_2",
        "indeed_match_1", "indeed_active_1", "indeed_jobalert_1", "indeed_jobalert_3",
        "glassdoor_1", "glassdoor_2",
    )] + [alert("feed1", [], sender="updates-noreply@linkedin.com"),
          alert("invite1", [], sender="invitations@linkedin.com"),
          alert("other1", [], sender="news@unknownboard.example")])

    s = run_at(T0, db_path, monkeypatch, mailbox=box, preferred=NO_FEEDS)

    li, indeed = s.senders["linkedin"], s.senders["indeed"]
    assert (li.emails_read, li.jobs_found, li.unparsed) == (4, 14, 0)
    assert li.addresses == {"jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"}
    # match (1) + confirmation (skipped) + two digests (15 + 30 cards, 21 of them sponsored)
    assert (indeed.emails_read, indeed.jobs_found, indeed.unparsed, indeed.skipped) == (4, 46, 0, 1)
    assert indeed.addresses == {"donotreply@match.indeed.com", "donotreply@jobalert.indeed.com"}
    gd = s.senders["glassdoor"]
    assert (gd.emails_read, gd.jobs_found, gd.unparsed) == (2, 2, 0)
    for ignored in ("updates-noreply@linkedin.com", "invitations@linkedin.com"):
        st = s.senders[ignored]
        assert (st.emails_read, st.unparsed, st.ignored) == (1, 0, 1)
    assert s.senders["news@unknownboard.example"].unparsed == 1
    assert any("1 skipped (not job lists)" in line for line in s.lines())
    assert "  email invitations@linkedin.com: 1 read / 0 jobs found / 0 unparsed / 1 ignored" in s.lines()
    assert s.errors == []

    done = processed(db_path)
    assert done["fixture-indeed_active_1"] == ("email:indeed", 0)  # skipped, never read again
    assert done["fixture-indeed_match_1"] == ("email:indeed", 1)
    assert "other1" not in done  # no parser: left for a later run
    assert done["invite1"] == done["feed1"] == (None, 0)  # ignored: marked processed, never read again
    assert done["fixture-indeed_jobalert_1"] == ("email:indeed", 15)
    assert done["fixture-indeed_jobalert_3"] == ("email:indeed", 30)
    assert done["fixture-glassdoor_2"] == ("email:glassdoor", 1)

    aiva = job(db_path, "email:indeed:eb5fd8db9e25d6e6")
    assert (aiva["company"], aiva["url"]) == ("BPEL KO", "https://www.indeed.com/viewjob?jk=eb5fd8db9e25d6e6")
    conn = db.connect(db_path)
    try:
        ads = conn.execute(
            "SELECT url, needs_details FROM jobs WHERE sponsored = 1 AND external_id LIKE 'email:indeed:sponsored-%'"
        ).fetchall()
    finally:
        conn.close()
    assert ads and all(a["url"].startswith("https://www.indeed.com/jobs?q=") and a["needs_details"] == 1 for a in ads)
    gd_job = job(db_path, "email:glassdoor:9287630000542")
    assert (gd_job["company"], gd_job["location"]) == ("Kagar24", "San Bruno, CA")

    cisco = job(db_path, "email:indeed:197d931256f2cf19")
    assert (cisco["company"], cisco["remote"], cisco["needs_details"]) == ("Zanwexlo Systems", 1, 1)
    assert cisco["url"] == "https://www.indeed.com/viewjob?jk=197d931256f2cf19"
    geico = job(db_path, "email:linkedin:9774902180")
    assert (geico["location"], geico["remote"]) == ("Palo Alto, CA", 0)


def test_expired_gmail_login_raises_a_notice_until_gmail_works(mailbox, feeds, db_path, monkeypatch):
    mailbox.expired = True
    s = run_at(T0, db_path, monkeypatch, mailbox=mailbox, feeds=feeds)

    assert s.gmail_auth_expired
    assert s.new == RAMP_PASSED  # feeds still ran
    assert s.lines()[1].startswith("  WARNING: Gmail login expired")
    [notice] = db.get_active_notices(db_path=db_path)
    assert notice["key"] == "gmail_auth_expired"
    assert "python -m agents.common.gmail auth" in notice["message"]

    # Still expired the next day: one notice, raised_at kept from the first time.
    run_at(T0 + timedelta(days=1), db_path, monkeypatch, mailbox=mailbox, feeds=feeds)
    [again] = db.get_active_notices(db_path=db_path)
    assert again["raised_at"] == notice["raised_at"]

    mailbox.expired = False  # the user ran `gmail auth`
    s = run_at(T0 + timedelta(days=2), db_path, monkeypatch, mailbox=mailbox, feeds=feeds)
    assert not s.gmail_auth_expired
    assert db.get_active_notices(db_path=db_path) == []


def test_other_gmail_errors_do_not_raise_or_clear_the_notice(mailbox, db_path, monkeypatch):
    mailbox.expired = True
    run_at(T0, db_path, monkeypatch, mailbox=mailbox)
    mailbox.expired, mailbox.broken = False, True
    run_at(T0 + timedelta(hours=1), db_path, monkeypatch, mailbox=mailbox)
    assert [n["key"] for n in db.get_active_notices(db_path=db_path)] == ["gmail_auth_expired"]


def test_gmail_failure_keeps_feed_results(mailbox, feeds, db_path, monkeypatch):
    mailbox.broken = True
    s = run_at(T0, db_path, monkeypatch, mailbox=mailbox, feeds=feeds)
    assert s.new == RAMP_PASSED
    assert len(s.errors) == 1 and "Gmail unreachable" in s.errors[0]


def test_email_jobs_are_never_closed_for_absence(mailbox, db_path, monkeypatch):
    run_at(T0, db_path, monkeypatch, mailbox=mailbox)
    s = run_at(T0 + timedelta(days=10), db_path, monkeypatch, mailbox=FakeMailbox())
    assert s.closed == 0
    assert job(db_path, "email:testalert:101")["status"] == "new"


# --- Feeds and both sources ------------------------------------------------------------


def test_feed_only_run(feeds, db_path, monkeypatch):
    s = run_at(T0, db_path, monkeypatch, feeds=feeds)
    assert feeds.calls == ["ramp"]
    assert (s.fetched, s.new) == (RAMP_FETCHED, RAMP_PASSED)
    assert {j["preferred_tier"] for j in db.get_jobs(db_path=db_path)} == {"interested"}
    assert {j["needs_details"] for j in db.get_jobs(db_path=db_path)} == {0}
    assert not any(line.strip().startswith("email") for line in s.lines())


def test_feeds_run_first_and_sleep_between_feed_requests(mailbox, feeds, db_path, monkeypatch):
    sleeps = []
    preferred = PREFERRED + [{"name": "Ramp Two", "aliases": [], "tier": "interested",
                              "poll_feed": True, "ats": "ashby", "slug": "ramp2"}]
    s = run_at(T0, db_path, monkeypatch, mailbox=mailbox, feeds=feeds, preferred=preferred, sleeps=sleeps)
    assert feeds.calls == ["ramp", "ramp2"]
    assert sleeps == [1.0]  # between the two feeds; Gmail calls aren't paced
    assert s.new == 2 * RAMP_PASSED + 2


def test_same_posting_from_feed_and_email_is_stored_once(feeds, db_path, monkeypatch):
    ramp_job = feeds.ramp["jobs"][1]  # Product Manager | Growth, New York + San Francisco
    box = FakeMailbox([alert("m1", [{
        "title": ramp_job["title"], "company": "Ramp", "location": "New York, NY (HQ); San Francisco, CA",
        "url": "https://jobs.testalert.example/view/999", "native_id": "999",
    }])])

    s = run_at(T0, db_path, monkeypatch, mailbox=box, feeds=feeds)

    assert s.duplicates == 1
    titles = [j for j in db.get_jobs(db_path=db_path) if j["title"] == ramp_job["title"]]
    assert [j["source"] for j in titles] == ["ashby"]  # the feed copy wins


def test_empty_preferred_list_is_fine(mailbox, db_path, monkeypatch):
    s = run_at(T0, db_path, monkeypatch, mailbox=mailbox, preferred=[])
    assert (s.new, s.new_preferred) == (2, 0)
    assert all(j["preferred_match"] is None for j in db.get_jobs(db_path=db_path))


def test_no_discovery_source_is_an_error(db_path, monkeypatch):
    with pytest.raises(fetch.NoDiscoverySource, match="gmail auth"):
        run_at(T0, db_path, monkeypatch, preferred=NO_FEEDS)
    assert not db_path.exists()


def test_second_run_refreshes_preferred_match(feeds, db_path, monkeypatch):
    run_at(T0, db_path, monkeypatch, feeds=feeds)
    am = job(db_path, "ashby:ramp:9ccbbed7-7c9c-5993-b357-ab109d1cea89")
    db.set_status(am["id"], "scored", "scout", db_path=db_path)

    retiered = copy.deepcopy(PREFERRED)
    retiered[2]["tier"] = "dream"
    s = run_at(T0 + timedelta(days=1), db_path, monkeypatch, feeds=feeds, preferred=retiered)

    assert (s.new, s.updated) == (0, RAMP_PASSED)
    am = job(db_path, am["external_id"])
    assert (am["status"], am["preferred_tier"]) == ("scored", "dream")


# --- Closing ------------------------------------------------------------------------


def test_removed_feed_posting_closed_after_three_days(feeds, db_path, monkeypatch):
    run_at(T0, db_path, monkeypatch, feeds=feeds)
    gone = "9ccbbed7-7c9c-5993-b357-ab109d1cea89"
    feeds.remove_ramp(gone)

    assert run_at(T0 + timedelta(days=2), db_path, monkeypatch, feeds=feeds).closed == 0
    assert run_at(T0 + timedelta(days=3, hours=1), db_path, monkeypatch, feeds=feeds).closed == 1
    assert job(db_path, f"ashby:ramp:{gone}")["status"] == "closed"


def test_failed_feed_does_not_close_its_jobs(feeds, db_path, monkeypatch):
    run_at(T0, db_path, monkeypatch, feeds=feeds)
    feeds.broken = True
    s = run_at(T0 + timedelta(days=5), db_path, monkeypatch, feeds=feeds)
    assert s.closed == 0
    assert all(j["status"] == "new" for j in db.get_jobs(db_path=db_path))


@pytest.mark.parametrize("final_status", ["applied", "interviewing"])
def test_applied_and_interviewing_are_never_closed(feeds, db_path, monkeypatch, final_status):
    run_at(T0, db_path, monkeypatch, feeds=feeds)
    native = "9ccbbed7-7c9c-5993-b357-ab109d1cea89"
    j = job(db_path, f"ashby:ramp:{native}")
    for status, actor in [("scored", "scout"), ("shortlisted", "scout"), ("researched", "research")]:
        db.set_status(j["id"], status, actor, db_path=db_path)
    db.mark_applied(j["id"], "ats", db_path=db_path)
    if final_status == "interviewing":
        db.set_status(j["id"], "interviewing", "tracker", db_path=db_path)
    feeds.remove_ramp(native)

    s = run_at(T0 + timedelta(days=10), db_path, monkeypatch, feeds=feeds)
    assert s.closed == 0
    assert job(db_path, f"ashby:ramp:{native}")["status"] == final_status


# --- CLI -----------------------------------------------------------------------------


@pytest.fixture
def cli_files(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(json.dumps(PROFILE), encoding="utf-8")  # JSON is valid YAML
    preferred = tmp_path / "preferred.yaml"
    preferred.write_text(json.dumps({"companies": NO_FEEDS}), encoding="utf-8")
    token = tmp_path / "token.json"  # never the real one
    log_dir = tmp_path / "logs"
    args = ["--db", str(tmp_path / "jobs.db"), "--profile", str(profile), "--preferred", str(preferred),
            "--gmail-token", str(token), "--log-dir", str(log_dir)]
    return args, token, log_dir


def test_cli_errors_when_no_discovery_source(cli_files, capsys):
    args, _, log_dir = cli_files
    assert fetch.main(args) == 2
    assert "No discovery source is configured" in capsys.readouterr().err
    [log_file] = log_dir.glob("scout-*.log")
    assert "Scout not run" in log_file.read_text(encoding="utf-8")


def test_cli_reads_alerts_and_writes_summary(cli_files, mailbox, monkeypatch, capsys):
    args, token, log_dir = cli_files
    token.write_text("{}", encoding="utf-8")
    seen_tokens = []

    def fake_mailbox(token_path):
        seen_tokens.append(token_path)
        return mailbox

    monkeypatch.setattr(fetch.gmail, "Mailbox", fake_mailbox)

    assert fetch.main(args) == 0

    assert seen_tokens == [token]
    out = capsys.readouterr().out
    assert "new:            2 (2 at preferred companies)" in out
    assert "2 read / 4 jobs found / 0 unparsed" in out
    text = next(log_dir.glob("scout-*.log")).read_text(encoding="utf-8")
    assert "location outside target states: 1" in text


def test_cli_since_days(cli_files, mailbox, monkeypatch):
    args, token, _ = cli_files
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fetch.gmail, "Mailbox", lambda token_path: mailbox)
    assert fetch.main(args + ["--since-days", "10"]) == 0
    assert mailbox.queries == ["label:JobAlerts newer_than:10d"]
    with pytest.raises(SystemExit):
        fetch.main(args + ["--since-days", "0"])


def test_cli_exit_1_when_the_only_source_fails(cli_files, mailbox, monkeypatch):
    args, token, _ = cli_files
    token.write_text("{}", encoding="utf-8")
    mailbox.broken = True
    monkeypatch.setattr(fetch.gmail, "Mailbox", lambda token_path: mailbox)
    assert fetch.main(args) == 1


def test_stats_inputs_are_recorded(db_path, monkeypatch):
    box = FakeMailbox([alert("m1", [KAISER, NYC]), alert("bad", []), alert("x1", [], sender="news@unknownboard.example")])
    box.messages["bad"]["html"] = "BROKEN"
    run_at(T0, db_path, monkeypatch, mailbox=box, preferred=NO_FEEDS)
    conn = db.connect(db_path)
    try:
        senders = {r["message_id"]: r["sender"] for r in conn.execute("SELECT * FROM processed_emails")}
        unparsed = {r["message_id"]: r["sender"] for r in conn.execute("SELECT * FROM unparsed_emails")}
        filtered = [dict(r) for r in conn.execute("SELECT * FROM filtered_postings")]
    finally:
        conn.close()
    assert senders == {"m1": ALERT_SENDER}
    assert unparsed == {"bad": ALERT_SENDER, "x1": "news@unknownboard.example"}
    assert [(f["company"], f["reason"].startswith("location")) for f in filtered] == [("Big Bank", True)]

    run_at(T0 + timedelta(hours=1), db_path, monkeypatch, mailbox=box, preferred=NO_FEEDS)  # seen again: once each
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM filtered_postings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM unparsed_emails").fetchone()[0] == 2
    finally:
        conn.close()
