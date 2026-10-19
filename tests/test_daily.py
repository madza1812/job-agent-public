from agents import daily, db

ORDER = ["fetch", "track", "score", "details", "pass2", "cleanup", "shortlist", "research"]


def test_steps_run_in_order_and_a_failure_does_not_stop_the_rest(tmp_path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    ran = []

    def step(name, fail=False):
        def fn():
            ran.append(name)
            if fail:
                raise RuntimeError(f"{name} broke")
        return fn

    steps = {name: step(name, fail=(name == "details")) for name in ORDER}
    path, stats, warnings = daily.run_daily(db_path=db_path, log_dir=tmp_path / "logs",
                                            out_dir=tmp_path / "digest", review_out=tmp_path / "review.html", steps=steps,
                                            claude_check=lambda: None)
    assert ran == ORDER
    assert stats.steps == [("fetch", "ok"), ("track", "ok"), ("score", "ok"), ("details", "failed"), ("pass2", "ok"),
                           ("cleanup", "ok"), ("shortlist", "ok"), ("research", "ok"), ("digest", "ok"), ("review", "ok")]
    assert warnings == ["details failed: details broke"]
    text = path.read_text(encoding="utf-8")
    assert "details failed: details broke" in text.split("## Today's")[0]
    assert (tmp_path / "review.html").read_text(encoding="utf-8").startswith("<!doctype html>")


def test_step_problems_become_warnings(tmp_path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)

    def fetch():
        daily._raise_if(["Acme (greenhouse/acme): 404"], "source errors")

    steps = {name: (lambda: None) for name in ORDER}
    steps["fetch"] = fetch
    _, stats, warnings = daily.run_daily(db_path=db_path, log_dir=tmp_path / "logs",
                                         out_dir=tmp_path / "digest", review_out=tmp_path / "review.html", steps=steps,
                                            claude_check=lambda: None)
    assert stats.steps[0] == ("fetch", "with problems")
    assert warnings == ["fetch: 1 source errors: Acme (greenhouse/acme): 404"]


def test_track_runs_after_fetch_and_warns_without_a_gmail_token(tmp_path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    steps = {name: (lambda: None) for name in ORDER if name != "track"}
    _, stats, warnings = daily.run_daily(db_path=db_path, log_dir=tmp_path / "logs", out_dir=tmp_path / "digest",
                                         review_out=tmp_path / "review.html", steps=steps, claude_check=lambda: None,
                                         gmail_token=tmp_path / "no-token.json")
    assert [n for n, _ in stats.steps][:2] == ["fetch", "track"]
    assert stats.steps[1] == ("track", "with problems")
    assert warnings == ["track: no Gmail token, so application emails were not read"]
