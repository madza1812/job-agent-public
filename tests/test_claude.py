import pytest

from agents.common import claude


def test_scoring_flags_have_no_tools_and_use_sonnet():
    flags = claude.flags()
    assert flags[flags.index("--model") + 1] == "sonnet"
    assert flags[flags.index("--tools") + 1] == ""
    assert "--allowedTools" not in flags and "--max-turns" not in flags


def test_tool_flags_limit_preapprove_and_cap_turns():
    flags = claude.flags(("WebSearch", "WebFetch"), max_turns=10)
    assert flags[flags.index("--tools") + 1] == "WebSearch,WebFetch"
    assert flags[flags.index("--allowedTools") + 1] == "WebSearch WebFetch"
    assert flags[flags.index("--max-turns") + 1] == "10"
    assert flags[flags.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in flags and "--disable-slash-commands" in flags


def test_usage_per_model_including_haiku_web_searches():
    reply = {"modelUsage": {
        "claude-sonnet-5-5": {"inputTokens": 8, "outputTokens": 554, "cacheReadInputTokens": 11316,
                              "cacheCreationInputTokens": 6937, "webSearchRequests": 0, "costUSD": 0.034},
        "claude-haiku-5-5": {"inputTokens": 27953, "outputTokens": 6360, "webSearchRequests": 2, "costUSD": 0.026},
    }}
    totals = claude.UsageTotals()
    totals.add(claude.parse_usage(reply))
    assert totals.total().web_searches == 2
    assert totals.by_model["claude-haiku-5-5"].input_tokens == 27953
    assert "web_searches=2" in totals.by_model["claude-haiku-5-5"].fields()


@pytest.mark.parametrize("text,kind,expected", [
    ('```json\n{"found": false}\n```\n\nI found no posting {really}.', dict, {"found": False}),
    ('Here: [{"id": 1}] and more [text]', list, [{"id": 1}]),
    ('noise { not json } then {"a": 1}', dict, {"a": 1}),
])
def test_extract_json_ignores_prose_and_fences(text, kind, expected):
    assert claude.extract_json(text, kind) == expected


def test_extract_json_raises_without_json():
    with pytest.raises(ValueError):
        claude.extract_json("nothing here", dict)


def test_max_turns_is_not_retried():
    calls = []

    def runner(prompt_file, user_prompt):
        calls.append(1)
        raise claude.MaxTurnsReached("out", {"modelUsage": {}})

    with pytest.raises(claude.MaxTurnsReached):
        claude.call_with_retry(runner, None, "x", "t", sleep=lambda s: None)
    assert len(calls) == 1


def test_deny_rules_become_disallowed_tools():
    flags = claude.flags(("WebSearch", "WebFetch"), 10, deny=("WebFetch(domain:linkedin.com)",))
    assert flags[flags.index("--disallowedTools") + 1] == "WebFetch(domain:linkedin.com)"


def test_claude_exe_comes_only_from_local_config(tmp_path, monkeypatch):
    from agents.common import claude
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")
    cfg = tmp_path / "local.yaml"
    cfg.write_text(f"claude_path: '{exe}'\n", encoding="utf-8")
    assert claude.claude_exe(cfg) == str(exe)
    for text, says in (("claude_path: 'C:/gone/claude.exe'\n", "doesn't exist"),
                       ("claude_path: claude.exe\n", "isn't an absolute path"),
                       ("other: 1\n", "has no claude_path"),
                       ("claude_path: [\n", "isn't valid YAML")):
        cfg.write_text(text, encoding="utf-8")
        with pytest.raises(claude.ClaudeNotConfigured, match=says) as err:
            claude.claude_exe(cfg)
        assert "setup.ps1" in str(err.value)
    with pytest.raises(claude.ClaudeNotConfigured, match="is missing"):
        claude.claude_exe(tmp_path / "missing.yaml")


def test_missing_claude_stops_without_retrying(tmp_path, monkeypatch):
    from agents.common import claude
    monkeypatch.setattr(claude, "LOCAL_CONFIG", tmp_path / "missing.yaml")
    monkeypatch.setattr(claude.claude_exe, "__defaults__", (tmp_path / "missing.yaml",))
    ran = []
    monkeypatch.setattr(claude.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(claude.ClaudeUnavailable):
        claude.call_with_retry(claude.run_claude, tmp_path / "p.txt", "x", "t", sleep=lambda s: ran.append(s))
    assert ran == []  # no subprocess, no backoff
