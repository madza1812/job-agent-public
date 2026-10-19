from agents import db, review
from agents.common import mdhtml


def test_markdown_basics():
    out = mdhtml.to_html("## Title\n\nSome **bold** and *it* text.\n\n- one\n- two [x](https://a.com/?q=1&b=2)\n\n"
                         "1. first\n2. second https://b.com/page.\n\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<h2>Title</h2>" in out
    assert "<strong>bold</strong>" in out and "<em>it</em>" in out
    assert '<ul><li>one</li><li>two <a href="https://a.com/?q=1&amp;b=2">x</a></li></ul>' in out
    assert '<ol><li>first</li><li>second <a href="https://b.com/page">https://b.com/page</a>.</li></ol>' in out
    assert "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>" in out


def test_markdown_escapes_html_and_drops_unsafe_links():
    out = mdhtml.to_html('<script>alert(1)</script> [click](javascript:alert(1)) "q"')
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "javascript:" not in out.split("click")[0] and "<a" not in out


def test_heading_offset():
    assert mdhtml.to_html("# T", heading_offset=1) == "<h2>T</h2>"


def test_review_page_renders_jobs_in_play_with_briefs(tmp_path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    ids = []
    for ext, company in (("1", "Acme"), ("2", "Other"), ("3", "Scored Only")):
        job_id, _ = db.upsert_job({"source": "email:linkedin", "external_id": ext, "company": company,
                                   "title": "Engineer <II>", "url": f"https://www.linkedin.com/jobs/view/{ext}/"},
                                  db_path=db_path)
        db.set_status(job_id, "scored", "scout", db_path=db_path)
        db.set_score(job_id, 70 - int(ext), {"pass": 2, "why": "Fits.", "gaps": ["AWS"]}, db_path=db_path)
        ids.append(job_id)
    for job_id in ids[:2]:
        db.set_status(job_id, "shortlisted", "scout", db_path=db_path)
    brief = tmp_path / "briefs" / "b.md"
    brief.parent.mkdir()
    brief.write_text("# Acme: Engineer\n\n## Skills match\n| A | B |\n|---|---|\n| PHP | strong |\n", encoding="utf-8")
    db.set_brief_path(ids[0], str(brief), db_path=db_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE jobs SET source_trust = 'aggregator' WHERE id = ?", (ids[1],))
    conn.close()

    html = review.build(db_path=db_path, root=tmp_path)
    assert html.startswith("<!doctype html>")
    assert "Acme" in html and "Other" in html and "Scored Only" not in html
    assert "Engineer &lt;II&gt;" in html
    assert "<h3>Skills match</h3>" in html and "<td>PHP</td>" in html
    assert "<h2>Acme: Engineer</h2>" not in html  # the brief's title line is dropped
    assert "No brief yet." in html and "aggregator, verify before applying" in html
    assert html.index('id="job-%d"' % ids[0]) < html.index('id="job-%d"' % ids[1])
