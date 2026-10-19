import base64
import gzip
import json

import pytest

from tools import sanitize_email as se


def token(payload):
    return base64.urlsafe_b64encode(gzip.compress(json.dumps(payload).encode())).decode().rstrip("=")


def decode(tok):
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))))


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        "identity:\n  name: Jane Doe\n"
        "sanitize:\n  replace:\n    'Springfield, CA 00001': 'Somewhere, CA 00000'\n    'jane-doe-123': 'test-user'\n",
        encoding="utf-8",
    )
    return path


def test_replacements_come_from_profile_and_account(profile):
    rep = se.load_replacements(profile, "jane@example.org")
    assert rep["Jane Doe"] == "Test User"
    assert rep["Hi Jane"] == "Hi Test"
    assert rep["jane@example.org"] == "user@example.com"
    assert rep["Springfield, CA 00001"] == "Somewhere, CA 00000"
    assert list(rep) == sorted(rep, key=len, reverse=True)  # longest first


def test_missing_profile_still_sanitizes_urls(tmp_path):
    assert se.load_replacements(tmp_path / "none.yaml") == {}


def test_urls_keep_path_and_job_key_only():
    url = "https://www.linkedin.com/comm/jobs/view/123/?trackingId=abc&otpToken=SECRET&jk=0123456789abcdef"
    assert se.clean_url(url) == "https://www.linkedin.com/comm/jobs/view/123/?trackingId=x&otpToken=x&jk=0123456789abcdef"
    assert se.clean_url("https://t.example/open/" + "A" * 40 + "/p.gif") == "https://t.example/open/REDACTED/p.gif"


def test_indeed_token_is_rebuilt_without_account_data():
    inner = {"u": "https://www.indeed.com/rc/clk?jk=0123456789abcdef&tk=T", "m": {"accountId": "999"}}
    cleaned = se.clean_url(f"https://cts.indeed.com/v3/{token(inner)}/SIGNATURE")
    tok = cleaned.split("/")[4]
    assert decode(tok) == {"u": "https://www.indeed.com/rc/clk?jk=0123456789abcdef&tk=x"}
    assert cleaned.endswith("/sig")


def test_sanitize_message_replaces_personal_terms(profile):
    rep = se.load_replacements(profile, "jane@example.org")
    message = {
        "id": "18abc", "sender": "alerts@example.com", "sender_name": "Alerts",
        "subject": "Jobs for Jane Doe", "date": "2026-10-14T00:00:00+00:00",
        "html": '<p>Hi Jane,</p><a href="https://www.linkedin.com/in/jane-doe-123?x=1&amp;y=2">Jane Doe</a>'
                "<p>Location: Springfield, CA 00001</p>",
        "text": "Hi Jane,\nSent to jane@example.org",
    }
    out = se.sanitize_message(message, "sample_1", rep)
    assert out["id"] == "fixture-sample_1"
    assert out["subject"] == "Jobs for Test User"
    assert "Hi Test," in out["html"] and "Somewhere, CA 00000" in out["html"]
    assert "/in/test-user?x=x&amp;y=x" in out["html"]
    assert out["text"] == "Hi Test,\nSent to user@example.com"
    blob = json.dumps(out)
    assert "Jane" not in blob and "jane" not in blob


def test_leftover_term_aborts():
    with pytest.raises(se.LeakError):
        se.check_no_leaks({"id": "f", "html": "Jane Doe", "text": ""}, {"Jane Doe": "Test User"})
    leaky = f"https://cts.indeed.com/v3/{token({'u': 'https://www.indeed.com/', 'm': {'accountId': '1'}})}/s"
    with pytest.raises(se.LeakError, match="redirect token"):
        se.check_no_leaks({"id": "f", "html": leaky, "text": ""}, {})


def test_href_with_raw_space_is_cleaned_whole():
    # Glassdoor leaves "loc=California, US" unencoded; the tokens after the space must not survive.
    html_in = ('<a href="https://www.glassdoor.com/job-listing/api/rjtRedirect?loc=California, US'
               '&amp;userValidationKey=SECRET1&amp;JAK=SECRET2">Create</a>')
    out = se.sanitize_text(html_in, {}, is_html=True)
    assert "SECRET" not in out
    assert "rjtRedirect?loc=x&amp;userValidationKey=x&amp;JAK=x" in out


def test_glassdoor_job_listing_id_is_kept():
    url = "https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=1009876543210&jrtk=T"
    assert se.clean_url(url) == "https://www.glassdoor.com/partner/jobListing.htm?pos=x&jobListingId=1009876543210&jrtk=x"


def test_unredacted_url_parameter_aborts():
    with pytest.raises(se.LeakError, match=r"unredacted URL parameters \(key, loc\)"):
        se.check_no_leaks({"id": "f", "html": "<a href='https://example.com/a?jk=0123456789abcdef&loc=A B&key=SECRET'>", "text": ""}, {})
