import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents import db, digest, review
from agents.scout import source_trust
from agents.tracker import classify, match, report, track

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
ATS = source_trust.load_config()["ats_sender_domains"]


def fixture(name):
    return json.loads((FIXTURES / f"email_{name}.json").read_text(encoding="utf-8"))


def email(mid, sender, subject, text, date="2026-10-17T15:00:00+00:00", sender_name="", primary=False):
    return {"id": mid, "sender": sender, "sender_name": sender_name, "subject": subject, "date": date,
            "html": "", "text": text, "_primary": primary}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def add_job(db_path, ext, company, title, status, apply_url=None, applied_at="2026-10-17T14:30:00+00:00"):
    job_id, _ = db.upsert_job({"source": "greenhouse", "external_id": ext, "company": company, "title": title,
                               "location": "Remote"}, db_path=db_path)
    path = {"scored": ["scored"], "researched": ["scored", "shortlisted", "researched"],
            "applied": ["scored", "shortlisted", "researched"], "interviewing": ["scored", "shortlisted", "researched"],
            "closed": ["scored"]}[status]
    for s in path:
        db.set_status(job_id, s, "research" if s == "researched" else "scout", db_path=db_path)
    if status in ("applied", "interviewing"):
        db.mark_applied(job_id, "ats", db_path=db_path)
        conn = db.connect(db_path)
        with conn:
            conn.execute("UPDATE applications SET applied_at = ? WHERE job_id = ?", (applied_at, job_id))
            conn.execute("UPDATE events SET at = ? WHERE job_id = ? AND to_status = 'applied'", (applied_at, job_id))
        conn.close()
    if status == "interviewing":
        db.set_status(job_id, "interviewing", "user", db_path=db_path)
    if status == "closed":
        db.set_status(job_id, "closed", "user", "skipped: recruiter talent network, applied directly", db_path=db_path)
    if apply_url:
        conn = db.connect(db_path)
        with conn:
            conn.execute("UPDATE jobs SET apply_url = ?, source_url = ? WHERE id = ?", (apply_url, apply_url, job_id))
        conn.close()
    return job_id


class FakeMailbox:
    """Answers queries roughly like Gmail: a quoted company plus from:(...) for job
    queries, from:(ATS senders) alone, and category:primary for the recruiter query."""

    def __init__(self, messages):
        self.messages = {m["id"]: m for m in messages}
        self.queries, self.bodies, self.headers = [], [], []

    def list_messages(self, query):
        self.queries.append(query)
        assert "-label:JobAlerts" in query and "after:" in query
        out = []
        for mid, m in self.messages.items():
            domain = match.sender_domain(m["sender"])
            blob = f"{m['subject']} {m['text']} {m['html']}".lower()
            if "category:primary" in query:
                hit = m.get("_primary")
            elif '"' in query:
                company = query.split('"')[1].lower()
                own = re.match(r".*?\(from:\(([^)]*)\) OR \(from:", query)
                own_domains = own.group(1).split(" OR ") if own else []
                hit = (match.domain_matches(domain, ATS) and company in blob) or match.domain_matches(domain, own_domains)
            else:
                hit = match.domain_matches(domain, ATS)
            if hit:
                out.append(mid)
        return out

    def get_headers(self, mid):
        self.headers.append(mid)
        m = self.messages[mid]
        return {k: m[k] for k in ("id", "sender", "sender_name", "subject", "date")}

    def get_message(self, mid):
        self.bodies.append(mid)
        return dict(self.messages[mid])


class FakeClaude:
    """Replies with a classification picked by a phrase in the email's subject."""

    def __init__(self, by_subject):
        self.by_subject = by_subject
        self.prompts = []

    def __call__(self, prompt_file, user_prompt):
        self.prompts.append(user_prompt)
        items = [json.loads(line) for line in user_prompt.splitlines()[1:]]
        out = []
        for item in items:
            for phrase, c in self.by_subject.items():
                if phrase in item["subject"]:
                    out.append({"id": item["id"], "confidence": 92, "about_job": item["job"] is not None,
                                "company": None, "role": None, "deadline": None, "next_action": None, **c})
                    break
        return {"result": json.dumps(out), "is_error": False,
                "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 5, "outputTokens": 50, "costUSD": 0.01}}}


def run(db_path, mailbox, claude, **kw):
    return track.track(mailbox, db_path=db_path, runner=claude, sleep=lambda s: None, now=kw.pop("now", NOW), **kw)


CONFIRMATION = {"category": "confirmation", "summary": "Application received; they'll be in touch if it fits."}


# The state machine: the tracker can make exactly three moves ------------------------------

ALLOWED = [("applied", "interviewing", "interview_invite: phone screen"), ("applied", "closed", "rejected"),
           ("interviewing", "closed", "rejected")]


@pytest.mark.parametrize("old,new,detail", ALLOWED)
def test_tracker_may_make_its_three_moves(db_path, old, new, detail):
    job_id = add_job(db_path, "1", "Contoso", "Engineer", old)
    db.set_status(job_id, new, "tracker", detail, db_path=db_path)
    assert db.get_job(job_id, db_path=db_path)["status"] == new


@pytest.mark.parametrize("old,new,detail", [
    ("applied", "closed", "withdrawn"), ("applied", "closed", None), ("interviewing", "closed", "filled"),
    ("researched", "applied", None), ("scored", "shortlisted", None), ("scored", "closed", "rejected"),
    ("researched", "closed", "rejected"), ("interviewing", "applied", None), ("closed", "new", "filtered: x"),
])
def test_tracker_can_make_no_other_change(db_path, old, new, detail):
    job_id = add_job(db_path, "1", "Contoso", "Engineer", old)
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job_id, new, "tracker", detail, db_path=db_path)
    assert db.get_job(job_id, db_path=db_path)["status"] == old


# Real emails (sanitized fixtures) --------------------------------------------------------

def test_extend_confirmation_is_an_event_and_the_security_code_is_ignored(db_path):
    job = add_job(db_path, "1", "Contoso", "Software Engineer II - Billing Team", "applied",
                  apply_url="https://job-boards.greenhouse.io/contoso/jobs/1")
    box = FakeMailbox([fixture("tracker_contoso_1"), fixture("tracker_contoso_2")])
    claude = FakeClaude({"Thank you for applying": CONFIRMATION})
    result = run(db_path, box, claude)
    assert result.classified == 1 and result.ignored == 1 and result.status_changes == 0
    assert box.bodies == ["fixture-tracker_contoso_1"]  # the code email's body is never fetched
    assert len(claude.prompts) == 1 and "Security code" not in claude.prompts[0]
    [msg] = [m for m in db.get_tracker_messages(db_path=db_path) if m["outcome"] == "event"]
    assert (msg["job_id"], msg["category"], msg["sender_domain"]) == (job, "confirmation", "us.greenhouse-mail.io")
    assert msg["summary"] == CONFIRMATION["summary"] and msg["msg_date"] == "2026-10-17T14:42:10+00:00"
    assert db.get_job(job, db_path=db_path)["status"] == "applied"


def test_nothing_but_id_date_domain_and_summary_is_stored(db_path):
    add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    run(db_path, FakeMailbox([fixture("tracker_contoso_1")]), FakeClaude({"Thank you": CONFIRMATION}))
    conn = db.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tracker_messages)")]
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM tracker_messages")])
    conn.close()
    assert not {"subject", "body", "text", "html"} & set(cols)
    assert "Thank you for applying" not in dump and "unattended mailbox" not in dump


def test_tds_confirmation_for_a_job_not_marked_applied_asks_did_you_apply(db_path):
    fabrikam = add_job(db_path, "7", "Fabrikam Solutions", "Solutions Engineer", "closed")
    box = FakeMailbox([fixture("tracker_fabrikam_1")])
    claude = FakeClaude({"Thanks for applying": {**CONFIRMATION, "company": "Fabrikam Solutions",
                                                 "role": "Solutions Engineering Professionals"}})
    run(db_path, box, claude)
    [msg] = db.get_tracker_messages(db_path=db_path)
    assert (msg["job_id"], msg["outcome"]) == (fabrikam, "did_you_apply")
    assert db.get_job(fabrikam, db_path=db_path)["status"] == "closed"
    lines = "\n".join(report.digest_lines(db_path=db_path, now=NOW, today=NOW.date()))
    assert "**Did you apply?**" in lines and f"#{fabrikam} **Fabrikam Solutions**" in lines
    assert f"mark-applied {fabrikam} --method ats" in lines
    payload = json.loads(claude.prompts[0].splitlines()[1])
    assert payload["job"]["status"] == "closed" and "application data removed" in payload["text"]


def test_processed_messages_are_never_classified_twice(db_path):
    add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    box = FakeMailbox([fixture("tracker_contoso_1"), fixture("tracker_contoso_2")])
    claude = FakeClaude({"Thank you": CONFIRMATION})
    run(db_path, box, claude)
    again = run(db_path, box, claude, since_days=30)
    assert again.already == 2 and again.classified == 0 and len(claude.prompts) == 1
    assert box.headers.count("fixture-tracker_contoso_1") == 1


# Outcomes ----------------------------------------------------------------------------------

def test_invite_moves_to_interviewing_and_rejection_closes(db_path):
    a = add_job(db_path, "1", "Acme", "Backend Engineer", "applied")
    b = add_job(db_path, "2", "Globex", "Data Engineer", "interviewing")
    box = FakeMailbox([
        email("m1", "no-reply@us.greenhouse-mail.io", "Interview with Acme", "Acme would like to schedule a call."),
        email("m2", "no-reply@hire.lever.co", "Your application to Globex", "Globex has decided not to move forward."),
    ])
    claude = FakeClaude({"Interview with Acme": {"category": "interview_invite", "summary": "Acme invites you to a call.",
                                                 "next_action": "Pick a time", "deadline": "2026-10-21"},
                         "Globex": {"category": "rejection", "summary": "Globex is not moving forward."}})
    result = run(db_path, box, claude)
    assert result.status_changes == 2
    assert db.get_job(a, db_path=db_path)["status"] == "interviewing"
    assert db.get_job(b, db_path=db_path)["status"] == "closed"
    last = db.get_job_events(b, db_path=db_path)[-1]
    assert (last["actor"], last["detail"]) == ("tracker", "rejected")
    first = db.get_job_events(a, db_path=db_path)[-1]
    assert first["actor"] == "tracker" and first["detail"].startswith("interview_invite: ")


def test_offer_changes_nothing_and_is_flagged_first(db_path):
    a = add_job(db_path, "1", "Acme", "Backend Engineer", "interviewing")
    box = FakeMailbox([email("m1", "no-reply@us.greenhouse-mail.io", "Offer from Acme", "We're pleased to offer...")])
    run(db_path, box, FakeClaude({"Offer": {"category": "offer", "summary": "Written offer, $150k.",
                                            "deadline": "2026-10-25"}}))
    assert db.get_job(a, db_path=db_path)["status"] == "interviewing"
    lines = report.digest_lines(db_path=db_path, now=NOW, today=NOW.date())
    assert lines[2].startswith("- 🎉 **OFFER**") and "deadline **2026-10-25**" in lines[2]


def test_low_confidence_needs_review_with_no_status_change(db_path):
    a = add_job(db_path, "1", "Acme", "Backend Engineer", "applied")
    box = FakeMailbox([email("m1", "no-reply@us.greenhouse-mail.io", "Update from Acme", "Next steps...")])
    run(db_path, box, FakeClaude({"Update": {"category": "interview_invite", "confidence": 55,
                                             "summary": "Unclear next steps."}}))
    assert db.get_job(a, db_path=db_path)["status"] == "applied"
    [msg] = db.get_tracker_messages(db_path=db_path)
    assert msg["outcome"] == "needs_review"
    lines = "\n".join(report.digest_lines(db_path=db_path, now=NOW, today=NOW.date()))
    assert "**Needs review**" in lines and "https://mail.google.com/mail/u/0/#all/m1" in lines


def test_not_about_the_matched_job_needs_review(db_path):
    add_job(db_path, "1", "Acme", "Backend Engineer", "applied")
    box = FakeMailbox([email("m1", "no-reply@us.greenhouse-mail.io", "Acme: your other application", "...")])
    run(db_path, box, FakeClaude({"Acme": {"category": "rejection", "about_job": False, "summary": "Other role."}}))
    assert db.get_tracker_messages(db_path=db_path)[0]["outcome"] == "needs_review"


def test_recruiter_outreach_for_roles_not_applied_to(db_path):
    box = FakeMailbox([
        email("m1", "jane@talentfirm.com", "Opportunity: Staff AI Engineer at Initech", "Your background fits...",
              primary=True),
        email("m2", "news@talentfirm.com", "Opportunity newsletter", "Ten roles this week", primary=True),
    ])
    claude = FakeClaude({"Staff AI Engineer": {"category": "recruiter_outreach", "company": "Initech",
                                               "role": "Staff AI Engineer", "summary": "Recruiter pitching a role."}})
    run(db_path, box, claude)
    assert box.bodies == ["m1"]  # the newsletter's sender is automated: headers only
    lines = "\n".join(report.digest_lines(db_path=db_path, now=NOW, today=NOW.date()))
    assert "**Recruiter outreach**" in lines and "**Initech**: Staff AI Engineer from talentfirm.com" in lines


def test_unlikely_headers_are_recorded_without_fetching_the_body(db_path):
    add_job(db_path, "1", "Acme", "Backend Engineer", "applied", apply_url="https://careers.acme.com/jobs/1")
    box = FakeMailbox([email("m1", "events@acme.com", "Acme newsletter", "Our conference...")])
    claude = FakeClaude({"Acme": CONFIRMATION})
    run(db_path, box, claude)  # company domain + company in subject: a likely match
    assert box.bodies == ["m1"]
    box2 = FakeMailbox([email("m2", "noreply@shop.example", "Weekly deals", "Acme sale")])
    box2.list_messages = lambda q: ["m2"]
    result = run(db_path, box2, claude)
    assert result.unmatched == 1 and box2.bodies == [] and len(claude.prompts) == 1


# Queries and windows ---------------------------------------------------------------------

def test_job_query_uses_company_domains_and_ats_senders():
    job = {"id": 1, "company": "Acme", "title": "Engineer", "status": "applied",
           "apply_url": "https://careers.acme.com/jobs/1", "source_url": "https://job-boards.greenhouse.io/acme/jobs/1"}
    assert match.company_domains(job, "https://boards.greenhouse.io/acme") == ["acme.com"]
    q = match.job_query(match.Candidate(job, ["acme.com"]), ATS, NOW)
    assert q.startswith(f"-label:JobAlerts -from:me after:{int(NOW.timestamp())} (from:(acme.com) OR (from:(")
    assert q.endswith(') "Acme"))') and "greenhouse-mail.io" in q
    no_domain = match.job_query(match.Candidate(job), ATS, NOW)
    assert "acme.com" not in no_domain and no_domain.endswith(') "Acme"))')


def test_oracle_recruiting_cloud_is_not_a_company_domain():
    job = {"id": 1, "company": "Initech", "title": "Engineer", "status": "applied",
           "apply_url": "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"}
    assert match.company_domains(job, None) == []


def test_ats_sender_domains_include_the_senders_seen_in_the_inbox():
    for real in ("no-reply@us.greenhouse-mail.io", "noreply@candidates.workablemail.com"):
        assert match.domain_matches(match.sender_domain(real), ATS)
    assert {"lever.co", "ashbyhq.com", "myworkday.com", "icims.com", "smartrecruiters.com"} <= set(ATS)


def test_window_first_run_30_days_then_since_last_run_unless_the_jobs_changed(db_path):
    assert track.window_start(NOW, None, db_path, "36") == NOW - timedelta(days=30)
    db.record_tracker_run("2026-10-15T07:00:00+00:00", "x", 0, "36", db_path=db_path)
    assert track.window_start(NOW, None, db_path, "36") == datetime(2026, 10, 14, 7, tzinfo=timezone.utc)
    assert track.window_start(NOW, None, db_path, "14,36") == NOW - timedelta(days=30)  # a job was marked applied
    assert track.window_start(NOW, 7, db_path, "14,36") == NOW - timedelta(days=7)


def test_dry_run_reads_headers_only_and_stores_nothing(db_path):
    add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    box = FakeMailbox([fixture("tracker_contoso_1")])
    claude = FakeClaude({})
    result = run(db_path, box, claude, dry_run=True)
    assert box.bodies == [] and claude.prompts == [] and db.get_tracker_messages(db_path=db_path) == []
    assert result.likely and "Contoso" in result.likely[0]
    assert db.last_tracker_run(db_path=db_path) is None


# Validation ------------------------------------------------------------------------------

def test_classification_is_validated():
    ok = {"category": "assessment", "confidence": 80, "summary": "Take-home test", "deadline": "2026-10-20"}
    assert classify.valid(ok)["deadline"] == "2026-10-20"
    assert classify.valid({**ok, "deadline": "next Friday"})["deadline"] is None
    for bad in ({**ok, "category": "ghosted"}, {**ok, "confidence": 101}, {**ok, "confidence": "80"},
                {**ok, "confidence": True}, {**ok, "summary": " "}):
        assert classify.valid(bad) is None
    assert len(classify.valid({**ok, "summary": "x" * 500})["summary"]) == classify.SUMMARY_CHARS
    assert set(classify.CATEGORIES) == {"confirmation", "assessment", "interview_invite", "scheduling", "rejection",
                                        "offer", "recruiter_outreach", "other"}


def test_message_text_falls_back_to_html_without_styles():
    text = classify.message_text(fixture("tracker_contoso_2"))
    assert "security code field" in text and "font-family" not in text


# Staleness and output --------------------------------------------------------------------

def test_follow_up_after_14_quiet_days_and_check_in_after_7(db_path):
    a = add_job(db_path, "1", "Acme", "Backend Engineer", "applied", applied_at="2026-10-01T12:00:00+00:00")
    add_job(db_path, "2", "Globex", "Data Engineer", "applied", applied_at="2026-10-10T12:00:00+00:00")
    c = add_job(db_path, "3", "Initech", "AI Engineer", "interviewing", applied_at="2026-10-05T12:00:00+00:00")
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE events SET at = '2026-10-09T12:00:00+00:00' WHERE job_id = ? AND to_status = 'interviewing'",
                     (c,))
    conn.close()
    db.record_tracker_message({"message_id": "c1", "job_id": a, "msg_date": "2026-10-01T13:00:00+00:00",
                               "sender_domain": "greenhouse-mail.io", "match_score": 60, "category": "confirmation",
                               "confidence": 95, "summary": "Received.", "outcome": "event"}, db_path=db_path)
    found = {(kind, job["id"]): days for kind, job, days in report.stale(
        db.get_applications(db_path=db_path), db.get_tracker_messages(db_path=db_path), NOW, db_path)}
    assert found == {("follow_up", a): 17, ("check_in", c): 9}  # a confirmation isn't a reply
    lines = "\n".join(report.digest_lines(db_path=db_path, now=NOW, today=NOW.date()))
    assert "**Consider a follow-up**" in lines and "no reply for 17 days" in lines
    assert "**Check in**" in lines and "no update for 9 days" in lines


def test_application_updates_sit_right_after_warnings(db_path):
    text = digest.build(NOW.date(), db_path=db_path)
    heads = [line for line in text.splitlines() if line.startswith("## ")]
    assert heads[:3] == ["## Warnings", "## Application updates", "## Today's top 3"]
    assert "Nothing to track yet: no job is marked applied." in text


def test_review_page_has_an_applications_timeline(db_path):
    a = add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    run(db_path, FakeMailbox([fixture("tracker_contoso_1")]), FakeClaude({"Thank you": CONFIRMATION}))
    page = review.build(db_path=db_path)
    section = page[page.index('<section id="applications">'):page.index("</section>")]
    assert f'id="app-{a}"' in section and "researched → applied (user, method=ats)" in section
    assert "confirmation from us.greenhouse-mail.io: Application received" in section
    assert section.index("researched → applied") < section.index("confirmation from")


def test_an_unlabeled_job_alert_naming_the_company_and_title_is_not_a_match(db_path):
    add_job(db_path, "1", "AceStack", "Full-Stack Lead", "applied")
    box = FakeMailbox([email("m1", "jobalerts-noreply@linkedin.com", "Full-Stack Lead @ Menlo Park at AceStack",
                             "New jobs for you", primary=True)])
    box.list_messages = lambda q: ["m1"]
    result = run(db_path, box, FakeClaude({}))
    assert result.unmatched == 1 and box.bodies == []


# Unmatched mail is only a header check -----------------------------------------------------

def test_a_confirmation_before_marking_applied_is_matched_on_the_next_run(db_path):
    job = add_job(db_path, "1", "Acme", "Backend Engineer", "researched", apply_url="https://careers.acme.com/jobs/1")
    box = FakeMailbox([email("m1", "careers@acme.com", "Thank you for applying to Acme",
                             "We received your application for Backend Engineer.", primary=True)])
    claude = FakeClaude({"Thank you for applying": CONFIRMATION})
    first = run(db_path, box, claude)
    assert first.unmatched == 1 and box.bodies == [] and claude.prompts == []  # headers only
    assert db.get_tracker_messages(db_path=db_path) == []  # not processed: only a header check

    db.mark_applied(job, "ats", db_path=db_path)
    second = run(db_path, box, claude, now=NOW + timedelta(hours=1))
    assert second.classified == 1 and box.bodies == ["m1"] and len(claude.prompts) == 1
    [msg] = db.get_tracker_messages(db_path=db_path)
    assert (msg["job_id"], msg["category"], msg["outcome"]) == (job, "confirmation", "event")

    third = run(db_path, box, claude, now=NOW + timedelta(hours=2))  # same jobs: nothing is fetched again
    assert third.already == 1 and box.headers == ["m1", "m1"] and len(claude.prompts) == 1


def test_a_message_claude_did_not_answer_is_retried(db_path):
    add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    box = FakeMailbox([fixture("tracker_contoso_1")])
    run(db_path, box, FakeClaude({}))  # the reply leaves it out
    assert db.get_tracker_messages(db_path=db_path) == []
    again = run(db_path, box, FakeClaude({"Thank you": CONFIRMATION}), now=NOW + timedelta(hours=1))
    assert again.classified == 1


def test_processing_is_per_message_and_job(db_path):
    a = add_job(db_path, "1", "Acme", "Backend Engineer", "applied")
    db.record_tracker_message({"message_id": "m1", "job_id": a, "sender_domain": "x.io", "match_score": 60,
                               "outcome": "event", "category": "other", "confidence": 90, "summary": "s"},
                              db_path=db_path)
    db.record_tracker_message({"message_id": "m1", "job_id": None, "sender_domain": "x.io", "match_score": 0,
                               "outcome": "event", "category": "other", "confidence": 90, "summary": "s"},
                              db_path=db_path)
    assert db.tracker_pairs(["m1"], db_path=db_path) == {("m1", a), ("m1", None)}
    with pytest.raises(Exception):  # the same pair twice
        db.record_tracker_message({"message_id": "m1", "job_id": None, "sender_domain": "x.io", "match_score": 0,
                                   "outcome": "event"}, db_path=db_path)


def test_migration_15_turns_unmatched_rows_into_header_checks(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for target, sql in enumerate(db.MIGRATIONS[:14], 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;")
    conn.executescript(
        "INSERT INTO tracker_messages (message_id, sender_domain, match_score, outcome, processed_at)"
        " VALUES ('u1', 'shop.example', 0, 'unmatched', '2026-10-17T00:00:00+00:00');"
        "INSERT INTO tracker_messages (message_id, sender_domain, match_score, category, confidence, summary,"
        " outcome, processed_at) VALUES ('c1', 'greenhouse-mail.io', 60, 'confirmation', 97, 'Received.', 'event',"
        " '2026-10-17T00:00:00+00:00');")
    conn.close()
    db.init_db(path)
    assert [m["message_id"] for m in db.get_tracker_messages(db_path=path)] == ["c1"]
    assert db.tracker_header_checks(["u1", "c1"], db_path=path) == {"u1": "", "c1": ""}  # checked again next run


# Reopen and closed -> applied -------------------------------------------------------------

def test_user_reopens_a_closed_job_to_scored(db_path):
    from agents import feedback
    job = add_job(db_path, "7", "Fabrikam Solutions", "Solutions Engineer", "closed")
    assert feedback.reopen(job, "applied after all", db_path=db_path) == "closed"
    assert db.get_job(job, db_path=db_path)["status"] == "scored"
    last = db.get_job_events(job, db_path=db_path)[-1]
    assert (last["actor"], last["from_status"], last["to_status"], last["detail"]) == \
        ("user", "closed", "scored", "reopened: applied after all")
    with pytest.raises(ValueError):
        feedback.reopen(job, " ", db_path=db_path)
    with pytest.raises(db.TransitionError):  # only from closed
        feedback.reopen(job, "again", db_path=db_path)


@pytest.mark.parametrize("actor", ["scout", "research", "tracker"])
def test_only_the_user_may_reopen(db_path, actor):
    job = add_job(db_path, "7", "Fabrikam", "Engineer", "closed")
    with pytest.raises(db.ActorNotAllowed):
        db.set_status(job, "scored", actor, "reopened: x", db_path=db_path)


def test_closed_to_applied_only_with_a_did_you_apply_item(db_path):
    job = add_job(db_path, "7", "Fabrikam Solutions", "Solutions Engineer", "closed")
    with pytest.raises(db.TransitionError, match="Did you apply"):
        db.mark_applied(job, "ats", db_path=db_path)
    run(db_path, FakeMailbox([fixture("tracker_fabrikam_1")]), FakeClaude({"Thanks for applying": CONFIRMATION}))
    for actor in ("scout", "tracker"):
        with pytest.raises(db.ActorNotAllowed):
            db.set_status(job, "applied", actor, db_path=db_path)
    db.mark_applied(job, "ats", db_path=db_path)
    assert db.get_job(job, db_path=db_path)["status"] == "applied"
    last = db.get_job_events(job, db_path=db_path)[-1]
    assert (last["actor"], last["from_status"], last["to_status"], last["detail"]) == \
        ("user", "closed", "applied", "method=ats")
    lines = "\n".join(report.digest_lines(db_path=db_path, now=NOW, today=NOW.date()))
    assert "Did you apply?" not in lines  # resolved


def test_did_you_apply_hint_gives_the_command_for_a_closed_job(db_path):
    job = add_job(db_path, "7", "Fabrikam Solutions", "Solutions Engineer", "closed")
    run(db_path, FakeMailbox([fixture("tracker_fabrikam_1")]), FakeClaude({"Thanks for applying": CONFIRMATION}))
    lines = "\n".join(report.digest_lines(db_path=db_path, now=NOW, today=NOW.date()))
    assert f"mark-applied {job} --method ats" in lines


# Reply toast -----------------------------------------------------------------------------

def test_reply_toast_for_an_interview_invite(db_path):
    add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    box = FakeMailbox([email("m1", "no-reply@us.greenhouse-mail.io", "Interview with Contoso", "Pick a time.")])
    shown = []
    run(db_path, box, FakeClaude({"Interview": {"category": "interview_invite", "summary": "Phone screen."}}),
        notify=lambda title, message: shown.append((title, message)))
    assert shown == [("Job agent", "Contoso: interview invite — check digest")]


def test_no_toast_when_nothing_new_or_only_a_confirmation(db_path):
    add_job(db_path, "1", "Contoso", "Software Engineer II", "applied")
    box = FakeMailbox([fixture("tracker_contoso_1")])
    shown = []
    run(db_path, box, FakeClaude({"Thank you": CONFIRMATION}), notify=lambda t, m: shown.append(m))
    run(db_path, box, FakeClaude({}), notify=lambda t, m: shown.append(m), now=NOW + timedelta(hours=1))
    assert shown == []


def test_reply_toast_lists_two_and_counts_the_rest():
    assert track.reply_toast([]) is None
    assert track.reply_toast(["Acme: rejection", "Globex: offer", "Initech: email to review"]) == \
        "Acme: rejection; Globex: offer and 1 more — check digest"


def test_needs_review_gets_a_toast(db_path):
    add_job(db_path, "1", "Acme", "Backend Engineer", "applied")
    box = FakeMailbox([email("m1", "no-reply@us.greenhouse-mail.io", "Update from Acme", "Next steps...")])
    shown = []
    run(db_path, box, FakeClaude({"Update": {"category": "confirmation", "confidence": 40, "summary": "Unclear."}}),
        notify=lambda t, m: shown.append(m))
    assert shown == ["Acme: email to review — check digest"]
