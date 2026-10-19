import pytest
import yaml

from agents import db, feedback
from agents.scout import score

PROFILE_TEXT = (
    "identity:\r\n"
    "  name: Test User   # keep this comment\r\n"
    "feedback:\r\n"
    "  liked_examples: []\r\n"
    "  disliked_examples: []\r\n"
    "\r\n"
    "# Used by tools/sanitize_email.py\r\n"
    "sanitize:\r\n"
    "  replace:\r\n"
    "    \"A\": \"B\"\r\n"
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


@pytest.fixture
def profile_path(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_bytes(PROFILE_TEXT.encode("utf-8"))
    return path


def add(db_path, ext_id="1", title="PHP Developer", company="Acme"):
    job_id, _ = db.upsert_job({"source": "greenhouse", "external_id": ext_id, "company": company,
                               "title": title}, db_path=db_path)
    return job_id


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_rate_up_sets_rating_and_appends_example(db_path, profile_path):
    a = add(db_path)
    feedback.rate(a, "up", "love the stack", db_path=db_path, profile_path=profile_path)
    assert db.get_job(a, db_path=db_path)["user_rating"] == 1
    assert load(profile_path)["feedback"]["liked_examples"] == [
        {"title": "PHP Developer", "company": "Acme", "note": "love the stack"}]


def test_rewrite_keeps_comments_other_sections_and_crlf(db_path, profile_path):
    feedback.rate(add(db_path), "down", db_path=db_path, profile_path=profile_path)
    raw = profile_path.read_bytes().decode("utf-8")
    assert "# keep this comment" in raw and "# Used by tools/sanitize_email.py" in raw
    assert "\n" not in raw.replace("\r\n", "")
    data = load(profile_path)
    assert data["sanitize"] == {"replace": {"A": "B"}}
    assert data["feedback"]["disliked_examples"] == [{"title": "PHP Developer", "company": "Acme"}]


def test_rating_again_moves_the_example(db_path, profile_path):
    a = add(db_path)
    feedback.rate(a, "up", db_path=db_path, profile_path=profile_path)
    feedback.rate(a, "down", "changed my mind", db_path=db_path, profile_path=profile_path)
    fb = load(profile_path)["feedback"]
    assert fb["liked_examples"] == [] and len(fb["disliked_examples"]) == 1
    assert db.get_job(a, db_path=db_path)["user_rating"] == -1


def test_keeps_last_20(db_path, profile_path):
    for i in range(25):
        feedback.rate(add(db_path, str(i), title=f"Job {i}"), "up", db_path=db_path, profile_path=profile_path)
    liked = load(profile_path)["feedback"]["liked_examples"]
    assert len(liked) == 20 and liked[0]["title"] == "Job 5" and liked[-1]["title"] == "Job 24"


def test_pass1_prompt_includes_feedback_examples(db_path, profile_path):
    feedback.rate(add(db_path), "up", "great domain", db_path=db_path, profile_path=profile_path)
    prompt = score.build_system_prompt(load(profile_path), [])
    assert "liked_examples" in prompt and "great domain" in prompt and "PHP Developer" in prompt


def test_unknown_job_changes_nothing(db_path, profile_path):
    with pytest.raises(LookupError):
        feedback.rate(999, "up", db_path=db_path, profile_path=profile_path)
    assert profile_path.read_bytes().decode("utf-8") == PROFILE_TEXT


def test_skip_closes_as_user_with_reason(db_path):
    a = add(db_path)
    feedback.skip(a, "commute too long", db_path=db_path)
    assert db.get_job(a, db_path=db_path)["status"] == "closed"
    conn = db.connect(db_path)
    row = conn.execute("SELECT actor, detail FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()
    conn.close()
    assert tuple(row) == ("user", "skipped: commute too long")


def test_skip_needs_a_reason(db_path):
    with pytest.raises(ValueError):
        feedback.skip(add(db_path), "  ", db_path=db_path)
