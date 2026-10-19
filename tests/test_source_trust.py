import pytest

from agents.scout import source_trust as st

CONFIG = {
    "ats_domains": ["greenhouse.io", "lever.co", "myworkdayjobs.com", "oraclecloud.com/hcmui/candidateexperience"],
    "public_sector_domains": ["calcareers.ca.gov", "edjoin.org"],
    "company_domains": {"sitewire by hexacorp": ["hexacorp.com"]},
    "blocked_fetch_domains": ["linkedin.com", "www.linkedin.com"],
}


@pytest.mark.parametrize("url,company,trust", [
    ("https://boards.greenhouse.io/acme/jobs/1", "Acme", "official"),
    ("https://job-boards.greenhouse.io/acme/jobs/1", "Other Co", "official"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", "Acme", "official"),
    ("https://calcareers.ca.gov/CalHrPublic/Jobs/JobPosting.aspx?JobControlId=1", "State of CA", "official"),
    ("https://www.edjoin.org/Home/JobPosting/1", "Palomar College", "official"),
    ("https://jobs.supermicro.com/job/San-Jose-Web-Developer/1/", "Supermicro", "official"),
    ("https://careers.propetroservices.com/job/1", "ProPetro Services, Inc", "official"),
    ("https://www.sitewire.com/careers/1", "Sitewire by Hexacorp", "official"),  # first word of the name
    ("https://careers.hexacorp.com/job/1", "Sitewire by Hexacorp", "official"),  # company_domains
    ("https://www.builtinboston.com/job/mid-level-backend-engineer/1", "Sitewire by Hexacorp", "aggregator"),
    ("https://jobright.ai/jobs/info/1", "Northgate Industrial Systems - NIS", "aggregator"),
    ("https://www.linkedin.com/jobs/view/1/", "LinkedIn", "aggregator"),  # blocked domains never count
    ("https://www.indeed.com/viewjob?jk=abc", "Acme", "aggregator"),
    ("https://notgreenhouse.io/acme", "Acme", "aggregator"),  # a suffix match needs a dot
    ("https://ark.example.com/job/1", "ARK", "aggregator"),  # too short to match on name
    ("", "Acme", "aggregator"),
    # Oracle Recruiting Cloud counts only under its candidate pages (e.g. Initech).
    ("https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1000", "Initech", "official"),
    ("https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperienceX/job/1", "Initech", "aggregator"),
    ("https://example.fa.us2.oraclecloud.com/fscmUI/faces/Home", "Initech", "aggregator"),
])
def test_classify(url, company, trust):
    assert st.classify(url, company, CONFIG) == trust


def test_real_config_loads_with_the_requested_lists():
    cfg = st.load_config()
    for d in ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "icims.com",
              "smartrecruiters.com", "jobvite.com", "taleo.net"):
        assert d in cfg["ats_domains"]
    assert {"governmentjobs.com", "calcareers.ca.gov", "edjoin.org"} <= set(cfg["public_sector_domains"])
    assert "linkedin.com" in cfg["blocked_fetch_domains"]


@pytest.mark.parametrize("source,apply,expected", [
    # Job 57: text from an aggregator, apply link on the company's own domain.
    ("https://jobs.anitab.org/companies/hooli/jobs/1", "https://www.hooli.com/about/careers/jobs/1",
     ("official", "https://www.hooli.com/about/careers/jobs/1")),
    ("https://boards.greenhouse.io/acme/jobs/1", "https://www.indeed.com/viewjob?jk=a",
     ("official", "https://boards.greenhouse.io/acme/jobs/1")),
    ("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/1#app",
     ("official", "https://boards.greenhouse.io/acme/jobs/1")),  # source_url first
    ("https://jobright.ai/jobs/info/1", None, ("aggregator", None)),
    ("https://jobright.ai/jobs/info/1", "https://www.linkedin.com/jobs/view/1/", ("aggregator", None)),
])
def test_classify_posting_checks_both_links(source, apply, expected):
    company = "Hooli" if "hooli" in (apply or "") else "Acme"
    assert st.classify_posting(source, apply, company, CONFIG) == expected


def test_real_config_has_oracle_recruiting_cloud():
    url = "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1000"
    assert st.classify(url, "Initech") == "official"
