import pytest

from agents.scout.companies import (
    PreferredCompaniesError,
    feed_companies,
    load_preferred,
    match_preferred,
    normalize_company,
)

PREFERRED = [
    {
        "name": "Kaiser Permanente",
        "aliases": ["Kaiser Foundation Hospitals", "The Permanente Medical Group"],
        "tier": "dream",
        "why": "mission",
    },
    {"name": "Abbott", "aliases": ["Abbott Laboratories"], "tier": "strong", "why": ""},
    {"name": "Ramp", "aliases": [], "tier": "interested", "poll_feed": True, "ats": "ashby", "slug": "ramp"},
]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Intuit Inc.", "intuit"),
        ("ACME, LLC", "acme"),
        ("Acme L.L.C.", "acme"),
        ("Evil Corp, Inc.", "evil"),
        ("Johnson & Johnson", "johnson and johnson"),
        ("The Permanente Medical Group", "permanente medical group"),
        ("Nestlé USA, Inc.", "nestle usa"),
        ("Co-op Financial", "co op financial"),  # suffix words only stripped at the end
        ("Globex Global Tech", "globex"),
        ("Globex Global Tech, Inc.", "globex"),    # stacked suffixes
        ("Globant Technologies Ltd", "globant"),
        ("Global Tech", "global tech"),              # the first word always stays
        ("Technologies Inc", "technologies"),
        ("Tech Global", "tech global"),              # not the "global tech" suffix
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Kaiser Permanente", ("Kaiser Permanente", "dream")),
        ("KAISER PERMANENTE, INC.", ("Kaiser Permanente", "dream")),
        ("Kaiser Foundation Hospitals", ("Kaiser Permanente", "dream")),
        ("Permanente Medical Group", ("Kaiser Permanente", "dream")),
        ("Abbott Laboratories Inc", ("Abbott", "strong")),
        ("Ramp", ("Ramp", "interested")),
        ("Abbott Nutrition", None),  # no partial matches
        ("Rampart Security", None),
        ("", None),
        (None, None),
    ],
)
def test_match_preferred(name, expected):
    assert match_preferred(name, PREFERRED) == expected


def test_match_preferred_ignores_global_tech_suffix():
    preferred = [{"name": "Globex", "tier": "strong"}]
    assert match_preferred("Globex Global Tech", preferred) == ("Globex", "strong")


def test_empty_preferred_list_matches_nothing():
    assert match_preferred("Kaiser Permanente", []) is None


def test_feed_companies():
    assert [c["name"] for c in feed_companies(PREFERRED)] == ["Ramp"]


def test_load_preferred_missing_or_empty_file_is_fine(tmp_path):
    assert load_preferred(tmp_path / "nope.yaml") == []
    empty = tmp_path / "empty.yaml"
    empty.write_text("companies: []\n", encoding="utf-8")
    assert load_preferred(empty) == []
    blank = tmp_path / "blank.yaml"
    blank.write_text("", encoding="utf-8")
    assert load_preferred(blank) == []


def test_load_preferred_reads_valid_file(tmp_path):
    path = tmp_path / "preferred.yaml"
    path.write_text(
        "companies:\n"
        "  - {name: Intuit, aliases: [Intuit Inc], tier: strong, why: fintech}\n"
        "  - {name: Ramp, tier: dream, poll_feed: true, ats: ashby, slug: ramp}\n",
        encoding="utf-8",
    )
    companies = load_preferred(path)
    assert [c["name"] for c in companies] == ["Intuit", "Ramp"]
    assert match_preferred("Intuit Inc.", companies) == ("Intuit", "strong")


@pytest.mark.parametrize(
    "content, message",
    [
        ("- just a list\n", "expected a mapping"),
        ("companies: {name: Ramp}\n", "must be a list"),
        ("companies:\n  - {tier: dream}\n", "needs a name"),
        ("companies:\n  - {name: Ramp, tier: favourite}\n", "tier must be one of"),
        ("companies:\n  - {name: Ramp, tier: dream, poll_feed: true}\n", "poll_feed needs ats"),
        ("companies:\n  - {name: Ramp, tier: dream, poll_feed: true, ats: workday, slug: r}\n", "poll_feed needs ats"),
        ("companies:\n  - {name: Ramp, tier: dream, poll_feed: true, ats: ashby}\n", "poll_feed needs a slug"),
        ("companies:\n  - {name: Ramp, tier: dream, aliases: Ramp Inc}\n", "aliases must be a list"),
        (
            "companies:\n  - {name: Ramp, tier: dream}\n  - {name: Other, tier: strong, aliases: [Ramp Inc.]}\n",
            "also matches",
        ),
    ],
)
def test_load_preferred_rejects_bad_entries(tmp_path, content, message):
    path = tmp_path / "preferred.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PreferredCompaniesError, match=message):
        load_preferred(path)
