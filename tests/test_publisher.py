import ast
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agents.publisher import draft, guard as guard_mod, ideas, sources, typefully

SOURCES = sources.Sources(
    resume="### Summary\nSoftware engineer with 10+ years. Cut per-call overhead from about 30,000 to about 600 tokens.",
    readme="# job-agent\nIt shortlists the best 3 jobs a day. See https://github.com/example/job-agent",
    git_log="2026-10-01 Initial public release\n",
    notes="",
)

POST = (
    "I cut the overhead of every headless Claude call from about 30,000 tokens to about 600.\n\n"
    + "The trick was boring: strip the tools, the system prompt and the settings, and send only my prompt and the data. "
    * 6
    + "\n\nThe lesson for me is simple. Measure the cost of one call before you build a system that makes hundreds."
    "\n\n#AI #Python"
)


def good_guard(**kw):
    return guard_mod.Guard(
        hype_words=["game-changer", "revolutionary"],
        security_terms=["firewall", "Acme Tunnel"],
        application_patterns=[r"\bI (?:just )?applied (?:to|at|for)\b"],
        companies=["Hooli", "Globex Global Tech", "Globex"],
        **kw,
    )


# --- fake Typefully -----------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeTypefully:
    """Answers the endpoints with the shapes the live API returned on 2026-10-18."""

    def __init__(self, remaining=10, draft_status="draft"):
        self.headers = {}
        self.calls = []
        self.remaining = remaining
        self.draft_status = draft_status
        self.next_id = 100

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        path = url.removeprefix(typefully.API_BASE)
        if method == "GET" and path == "/v2/social-sets":
            return FakeResponse(200, {"results": [{"id": 42, "username": "u", "name": "An", "profile_image_url": "",
                                                    "team": None}],
                                      "count": 1, "limit": 50, "offset": 0, "next": None, "previous": None})
        if method == "GET" and path == "/v2/social-sets/42/":
            return FakeResponse(200, {"id": 42, "platforms": {"x": None, "linkedin": {"platform": "linkedin"}},
                                      "publishing_quota": {"used": 10 - self.remaining, "remaining": self.remaining,
                                                           "resets_at": "2026-11-01T00:00:00-07:00"}})
        if method == "POST" and path == "/v2/social-sets/42/drafts":
            self.next_id += 1
            return FakeResponse(201, {"id": self.next_id, "draft_id": self.next_id, "status": self.draft_status,
                                      "private_url": f"https://typefully.com/?d={self.next_id}",
                                      "created_at": "2026-10-18T17:13:15.819Z", "platforms": json["platforms"]})
        return FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": path}})


def client_for(fake):
    return typefully.Typefully("test-key", session=fake, sleep=lambda s: None)


def runner_returning(*texts):
    replies = iter(texts)
    calls = []

    def runner(prompt_file, user_prompt):
        calls.append(user_prompt)
        return {"result": json.dumps({"text": next(replies)}), "modelUsage": {}}
    runner.calls = calls
    return runner


@pytest.fixture
def ideas_file(tmp_path):
    path = tmp_path / "post-ideas-2026-10-18.md"
    path.write_text(ideas.render([ideas.Idea(f"Idea {i}", f"line {i}") for i in range(1, 6)],
                                 __import__("datetime").date(2026, 10, 18)), encoding="utf-8")
    return path


@pytest.fixture
def local_config(tmp_path):
    path = tmp_path / "local.yaml"
    path.write_bytes(b"# settings\r\nclaude_path: 'C:\\x\\claude.exe'\r\n")
    return path


def run_draft(tmp_path, ideas_file, local_config, fake, runner, numbers=(1,)):
    return draft.run(list(numbers), ideas_file, drafts_dir=tmp_path / "drafts", srcs=SOURCES, guard=good_guard(),
                     client=client_for(fake), runner=runner, config_path=local_config)


# --- never publish ------------------------------------------------------------------

def test_no_request_ever_includes_publish_at(tmp_path, ideas_file, local_config):
    fake = FakeTypefully()
    result = run_draft(tmp_path, ideas_file, local_config, fake, runner_returning(POST, POST, POST), numbers=(1, 2, 3))
    assert len(result.saved) == 3
    assert fake.calls, "the run made no requests"
    for call in fake.calls:
        sent = json.dumps({"params": call["params"], "json": call["json"]})
        assert "publish_at" not in sent and "plan_at" not in sent, call
    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert len(posts) == 3
    assert posts[0]["json"] == {"platforms": {"linkedin": {"enabled": True, "posts": [{"text": POST}]}}}


@pytest.mark.parametrize("params,body", [
    (None, {"publish_at": "now"}),
    (None, {"platforms": {"linkedin": {"enabled": True, "posts": [{"text": "x", "plan_at": "next-free-slot"}]}}}),
    ({"publish_at": "2026-10-19T09:00:00Z"}, None),
])
def test_publish_at_and_plan_at_are_refused_before_sending(params, body):
    fake = FakeTypefully()
    with pytest.raises(typefully.ForbiddenRequest):
        client_for(fake)._request("POST", "/v2/social-sets/42/drafts", params=params, body=body)
    assert fake.calls == []


@pytest.mark.parametrize("method,path", [
    ("PATCH", "/v2/social-sets/42/drafts/7"),
    ("DELETE", "/v2/social-sets/42/drafts/7"),
    ("POST", "/v2/social-sets/42/drafts/7"),
])
def test_only_gets_and_draft_creation_are_allowed(method, path):
    fake = FakeTypefully()
    with pytest.raises(typefully.ForbiddenRequest):
        client_for(fake)._request(method, path, body={})
    assert fake.calls == []


def test_a_draft_that_comes_back_not_as_draft_raises():
    with pytest.raises(typefully.TypefullyError, match="not 'draft'"):
        client_for(FakeTypefully(draft_status="scheduled")).create_draft(42, POST)


def test_api_key_from_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nTYPEFULLY_API_KEY='abc123'\n", encoding="utf-8")
    assert typefully.api_key(env) == "abc123"
    env.write_text("OTHER=1\n", encoding="utf-8")
    with pytest.raises(typefully.TypefullyError):
        typefully.api_key(env)


# --- social set cache ---------------------------------------------------------------

def test_social_set_id_is_fetched_once_and_cached(local_config):
    fake = FakeTypefully()
    assert typefully.social_set_id(client_for(fake), local_config) == 42
    raw = local_config.read_bytes().decode("utf-8")
    assert raw == "# settings\r\nclaude_path: 'C:\\x\\claude.exe'\r\ntypefully_social_set_id: 42\r\n"
    fake.calls.clear()
    assert typefully.social_set_id(client_for(fake), local_config) == 42
    assert fake.calls == []


# --- limits -------------------------------------------------------------------------

def test_at_most_three_drafts_per_run(tmp_path, ideas_file, local_config):
    fake = FakeTypefully()
    with pytest.raises(ValueError, match="at most 3"):
        run_draft(tmp_path, ideas_file, local_config, fake, runner_returning(), numbers=(1, 2, 3, 4))
    assert fake.calls == []


def test_used_up_quota_stops_before_any_claude_call(tmp_path, ideas_file, local_config):
    runner = runner_returning(POST)
    with pytest.raises(draft.LimitReached, match="used up"):
        run_draft(tmp_path, ideas_file, local_config, FakeTypefully(remaining=0), runner)
    assert runner.calls == []


def test_monthly_draft_count_is_kept_and_capped(tmp_path, ideas_file, local_config, caplog):
    caplog.set_level("INFO")
    fake = FakeTypefully()
    run_draft(tmp_path, ideas_file, local_config, fake, runner_returning(POST))
    ledger = tmp_path / "drafts" / draft.LEDGER_NAME
    assert draft.month_count(ledger) == 1
    assert "Monthly count: 0 published, 10 left" in caplog.text
    entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert entries[0]["draft_id"] == 101 and entries[0]["idea"] == 1
    for _ in range(8):
        draft.record(ledger, 2, ideas.Idea("t", "l"), typefully.Draft(1, "draft", "", ""))
    with pytest.raises(draft.LimitReached, match="pass 10 drafts"):
        run_draft(tmp_path, ideas_file, local_config, fake, runner_returning(POST, POST), numbers=(2, 3))


def test_month_count_ignores_other_months(tmp_path):
    ledger = tmp_path / "l.jsonl"
    ledger.write_text('{"month": "2026-09"}\n{"month": "2026-10"}\nnot json\n', encoding="utf-8")
    assert draft.month_count(ledger, __import__("datetime").date(2026, 10, 18)) == 1


# --- drafting -----------------------------------------------------------------------

def test_a_post_failing_twice_is_kept_local_and_never_sent(tmp_path, ideas_file, local_config):
    fake = FakeTypefully()
    bad = POST.replace("The lesson", "This game-changer at Hooli taught me")
    runner = runner_returning(bad, bad)
    result = run_draft(tmp_path, ideas_file, local_config, fake, runner)
    assert result.saved == [] and result.rejected[0][0] == 1
    assert not [c for c in fake.calls if c["method"] == "POST"]
    assert "game-changer" in runner.calls[1] and "Hooli" in runner.calls[1]  # the retry lists the problems
    saved = next((tmp_path / "drafts").glob("post-*-1.md")).read_text(encoding="utf-8")
    assert "Not saved to Typefully" in saved


def test_a_post_fixed_on_retry_is_sent(tmp_path, ideas_file, local_config):
    fake = FakeTypefully()
    result = run_draft(tmp_path, ideas_file, local_config, fake, runner_returning("too short", POST))
    assert [n for n, _, _ in result.saved] == [1]
    assert len([c for c in fake.calls if c["method"] == "POST"]) == 1


def test_dry_run_never_calls_typefully(tmp_path, ideas_file, local_config):
    result = draft.run([1], ideas_file, dry_run=True, drafts_dir=tmp_path / "drafts", srcs=SOURCES,
                       guard=good_guard(), client=None, runner=runner_returning(POST), config_path=local_config)
    assert result.saved[0][1] is None


def test_unknown_idea_number(tmp_path, ideas_file, local_config):
    with pytest.raises(ValueError, match="no idea 9"):
        run_draft(tmp_path, ideas_file, local_config, FakeTypefully(), runner_returning(), numbers=(9,))


# --- guard --------------------------------------------------------------------------

def test_a_good_post_passes():
    assert good_guard().check(POST, SOURCES.all_text()) == []


@pytest.mark.parametrize("change,problem", [
    (lambda t: t.replace("The lesson", "This revolutionary idea"), "hype word"),
    (lambda t: t.replace("The lesson", "Hooli taught me"), "company"),
    (lambda t: t.replace("The lesson", "Globex taught me"), "company"),
    (lambda t: t.replace("600", "450"), "number '450'"),
    (lambda t: t.replace("The lesson", "Our 7 servers taught me"), "number '7'"),
    (lambda t: t + " #Claude #Agents", "hashtags"),
    (lambda t: t.replace("The lesson", "Behind the firewall, the lesson"), "security detail"),
    (lambda t: t.replace("The lesson", "Server 10.0.0.12 taught me"), "IP address"),
    (lambda t: t.replace("The lesson", "Mail me at a@b.com, the lesson"), "email"),
    (lambda t: t.replace("The lesson", "I applied to a role and the lesson"), "application"),
    (lambda t: t.replace("The lesson", "See https://example.com, the lesson"), "link not in"),
    (lambda t: t + " 🚀🚀🚀", "emoji"),
    (lambda t: "Too short. 600 tokens.", "words"),
])
def test_guard_refuses(change, problem):
    problems = good_guard().check(change(POST), SOURCES.all_text())
    assert any(problem in p for p in problems), problems


def test_links_and_numbers_from_the_sources_are_fine():
    text = POST.replace("The lesson", "Code: https://github.com/example/job-agent. The lesson")
    assert good_guard().check(text, SOURCES.all_text()) == []


def test_private_terms_from_the_profile(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("publisher:\n  blocked_terms: [Acme Orchards]\n", encoding="utf-8")
    g = guard_mod.load(profile_path=profile, preferred_path=tmp_path / "none.yaml", db_path=tmp_path / "none.db")
    assert g.hype_words and g.security_terms  # from config/publisher.yaml
    problems = g.check(POST.replace("The lesson", "Acme Orchards taught me"), SOURCES.all_text())
    assert any("private term" in p for p in problems)


def test_company_terms_keep_original_casing():
    assert guard_mod.company_terms("Globex Global Tech") == ["Globex Global Tech", "Globex"]
    assert guard_mod.company_terms("Stark Motors Inc.") == ["Stark Motors Inc.", "Stark Motors"]
    assert guard_mod.company_terms("Hooli") == ["Hooli"]


def test_companies_in_play_come_from_the_database_read_only(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, company TEXT, status TEXT, brief_path TEXT);"
        "CREATE TABLE applications (job_id INTEGER);"
        "INSERT INTO jobs VALUES (1, 'Applied Co', 'applied', NULL), (2, 'Scored Co', 'scored', NULL),"
        " (3, 'Briefed Co', 'closed', 'briefs/x.md'), (4, 'Recorded Co', 'closed', NULL),"
        " (5, 'Shortlisted Co', 'shortlisted', NULL);"
        "INSERT INTO applications VALUES (4);"
    )
    conn.commit()
    conn.close()
    assert sorted(guard_mod.db_companies(db_path)) == ["Applied Co", "Briefed Co", "Recorded Co", "Shortlisted Co"]
    assert guard_mod.db_companies(tmp_path / "missing.db") == []


def test_preferred_companies_and_aliases(tmp_path):
    path = tmp_path / "preferred.yaml"
    path.write_text("companies:\n  - name: Meta\n    aliases: [Facebook]\n    tier: dream\n", encoding="utf-8")
    assert guard_mod.preferred_companies(path) == ["Meta", "Facebook"]


# --- sources ------------------------------------------------------------------------

@pytest.mark.parametrize("folder", ["data", "briefs", "digest", "review", "archive", "secrets"])
def test_private_folders_are_never_read(tmp_path, folder):
    path = tmp_path / folder / "x.md"
    path.parent.mkdir()
    path.write_text("private", encoding="utf-8")
    with pytest.raises(sources.PrivatePath):
        sources.read_public(path, root=tmp_path)


def test_sources_module_never_touches_the_database():
    tree = ast.parse(Path(sources.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{a.name}" for a in node.names} | {node.module or ""}
    assert not {name for name in imported if "db" in name.split(".") or "sqlite" in name}, imported


def test_resume_drops_the_contact_header():
    raw = "# Sam\n\nSpringfield · (555) 555-0100 · someone@example.com\n\n### Summary\nEngineer. Mail me@x.com or 555-555-0101."
    text = sources.resume_text(raw)
    assert text.startswith("### Summary")
    assert "@" not in text and "555" not in text


def test_load_reads_resume_readme_git_log_and_notes(tmp_path):
    root, repo = tmp_path / "root", tmp_path / "public"
    (root / "profile").mkdir(parents=True)
    (root / "profile" / "resume.md").write_text("# Sam\n555-123-4567\n### Summary\nBuilt things.", encoding="utf-8")
    (root / "profile" / "post_notes.md").write_text("Write about tests.", encoding="utf-8")
    repo.mkdir()
    (repo / "README.md").write_text("# job-agent", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Initial public release"], check=True)
    srcs = sources.load(root=root, repo=repo)
    assert srcs.resume == "### Summary\nBuilt things."
    assert srcs.git_log.strip().endswith("Initial public release") and "t@example.com" not in srcs.git_log
    assert srcs.notes == "Write about tests."
    assert '<source name="Owner\'s post notes">' in srcs.as_prompt()


def test_public_repo_defaults_to_the_sibling_folder(tmp_path):
    root = tmp_path / "job-agent"
    assert sources.public_repo(tmp_path / "missing.yaml", root=root) == (tmp_path / "job-agent-public").resolve()


# --- ideas --------------------------------------------------------------------------

def test_ideas_round_trip_through_the_file(ideas_file):
    read = ideas.read_ideas(ideas_file)
    assert sorted(read) == [1, 2, 3, 4, 5] and read[3] == ideas.Idea("Idea 3", "line 3")


def test_ideas_reply_needs_exactly_five():
    with pytest.raises(ValueError, match="expected 5"):
        ideas.parse_reply(json.dumps({"ideas": [{"title": "a", "line": "b"}]}))


def test_an_idea_naming_a_company_is_retried():
    good = [{"title": f"Idea {c}", "line": f"About 600 tokens, take {c}"} for c in "ABCDE"]
    bad = [dict(good[0], line="What Hooli taught me"), *good[1:]]
    replies = iter([bad, good])
    prompts = []

    def runner(prompt_file, user_prompt):
        prompts.append(user_prompt)
        return {"result": json.dumps({"ideas": next(replies)}), "modelUsage": {}}

    result = ideas.propose(SOURCES, good_guard(), runner)
    assert [i.title for i in result] == [f"Idea {c}" for c in "ABCDE"]
    assert "Hooli" in prompts[1] and "fix every one" in prompts[1]


# --- common-word companies and the allowlist (config/publisher.yaml) -----------------

@pytest.fixture
def real_guard(tmp_path):
    preferred = tmp_path / "preferred.yaml"
    preferred.write_text(
        "companies:\n"
        "  - {name: Hooli, tier: dream}\n"
        "  - {name: Northwind, tier: interested}\n"
        "  - {name: Beacon, tier: strong}\n"
        "  - {name: Summit, tier: strong}\n"
        "  - {name: Harbor, tier: strong}\n"
        "  - {name: Anchor, tier: strong}\n",
        encoding="utf-8")
    return guard_mod.load(profile_path=tmp_path / "none.yaml", preferred_path=preferred, db_path=tmp_path / "none.db")


def lesson(text):
    return POST.replace("The lesson", text)


@pytest.mark.parametrize("phrase", [
    "The test suite became a beacon, and the lesson", "At the summit of the release, the lesson",
    "Safe harbor aside, the lesson", "An anchor test showed the lesson",
])
def test_common_word_companies_pass_in_lowercase(real_guard, phrase):
    assert real_guard.check_content(lesson(phrase), SOURCES.all_text()) == []


@pytest.mark.parametrize("name", ["Beacon", "Summit", "Harbor", "Anchor"])
def test_common_word_companies_are_caught_as_written(real_guard, name):
    problems = real_guard.check_content(lesson(f"Talking to {name}, the lesson"), SOURCES.all_text())
    assert any(f"company you may be applying to: {name!r}" in p for p in problems), problems


def test_other_companies_match_in_any_case(real_guard):
    problems = real_guard.check_content(lesson("Working with hooli taught me the lesson"), SOURCES.all_text())
    assert any("'Hooli'" in p for p in problems), problems


@pytest.mark.parametrize("phrase", ["At Northwind, the lesson", "Behind acme tunnel, the lesson"])
def test_allowlisted_phrases_pass(real_guard, phrase):
    assert real_guard.check_content(lesson(phrase), SOURCES.all_text()) == []


@pytest.mark.parametrize("phrase,problem", [
    ("Acme Tunnel to 10.0.0.12 taught me the lesson", "IP address"),
    ("Acme Tunnel on port 8443 taught me the lesson", "port"),
    ("Acme Tunnel to erp.example.local taught me the lesson", "internal hostname"),
    ("Acme Tunnel on app.example-ranch.com taught me the lesson", "link not in"),
    ("Tunnel 12345678-1234-4abc-8def-1234567890ab.cfargotunnel.com taught me the lesson", "tunnel hostname or ID"),
    ("Acme Tunnel 12345678-1234-4abc-8def-1234567890ab taught me the lesson", "tunnel hostname or ID"),
    ("Our Acme Tunnel ID taught me the lesson", "security detail: 'tunnel ID'"),
])
def test_allowlist_never_lets_security_details_through(real_guard, phrase, problem):
    problems = real_guard.check_content(lesson(phrase), SOURCES.all_text())
    assert any(problem in p for p in problems), problems


def test_allowlist_does_not_hide_a_company_next_to_it(real_guard):
    problems = real_guard.check_content(lesson("Northwind and Hooli taught me the lesson"), SOURCES.all_text())
    assert [p for p in problems if "company" in p] == ["names a company you may be applying to: 'Hooli'"]
