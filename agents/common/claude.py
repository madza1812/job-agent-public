"""`claude -p` calls shared by the scoring passes and the details fetch.

Every call carries only our prompt plus data: no Claude Code system prompt, no MCP
servers, no settings or hooks, no skills, no saved session (BASE_FLAGS). That takes the
per-call overhead from ~30k input tokens to ~600. Tools are off unless a caller names
them; named tools are pre-approved, since nobody can answer a permission prompt.

--json-schema is deliberately not used: it adds an output tool and a second turn. Replies
are plain JSON, pulled out with extract_json() and validated by the caller.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

log = logging.getLogger(__name__)

# Machine-specific settings written by scripts/setup.ps1 (gitignored), e.g. claude_path.
LOCAL_CONFIG = Path(__file__).resolve().parents[2] / "config" / "local.yaml"

MODEL = "sonnet"
CALL_TIMEOUT_S = 300
RETRY_DELAYS_S = (10, 30)  # backoff between attempts after a failed call

BASE_FLAGS = (
    "-p",
    "--model", MODEL,
    "--output-format", "json",
    "--no-session-persistence",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--setting-sources", "",
    "--disable-slash-commands",
)
# The scoring passes: no tools at all.
CLAUDE_FLAGS = (*BASE_FLAGS, "--tools", "")

# A reply Claude produced; the runner's return value.
Reply = dict[str, Any]
Runner = Callable[[Path, str], Reply]


class ClaudeUnavailable(RuntimeError):
    """Further calls this run would fail too: callers stop instead of retrying."""


class UsageLimitReached(ClaudeUnavailable):
    """The Claude plan's usage limit is hit."""


class ClaudeNotConfigured(ClaudeUnavailable):
    """config/local.yaml has no usable absolute claude_path."""


class ClaudeCallFailed(RuntimeError):
    pass


class MaxTurnsReached(ClaudeCallFailed):
    """The call used up --max-turns before answering. Retrying wouldn't help."""

    def __init__(self, message: str, reply: Reply) -> None:
        super().__init__(message)
        self.reply = reply


def flags(tools: Sequence[str] = (), max_turns: int | None = None,
          deny: Sequence[str] = ()) -> tuple[str, ...]:
    """deny: permission rules that are refused, e.g. "WebFetch(domain:linkedin.com)"."""
    if not tools:
        return CLAUDE_FLAGS
    names = ",".join(tools)
    out = (*BASE_FLAGS, "--tools", names, "--allowedTools", " ".join(tools))
    if deny:
        out += ("--disallowedTools", *deny)
    if max_turns is not None:
        out += ("--max-turns", str(int(max_turns)))
    return out


def claude_exe(config_path: Path = LOCAL_CONFIG) -> str:
    """The absolute claude.exe path from config/local.yaml. Never PATH: Task Scheduler runs
    with a different PATH, so a missing or wrong setting fails here, clearly."""
    fix = "run: powershell -ExecutionPolicy Bypass -File scripts\\setup.ps1"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError:
        raise ClaudeNotConfigured(f"{config_path.name} is missing, so claude.exe can't be found; {fix}") from None
    except yaml.YAMLError as exc:
        raise ClaudeNotConfigured(f"{config_path.name} isn't valid YAML ({exc}); {fix}") from None
    path = data.get("claude_path") if isinstance(data, dict) else None
    if not path:
        raise ClaudeNotConfigured(f"{config_path.name} has no claude_path; {fix}")
    if not Path(path).is_absolute():
        raise ClaudeNotConfigured(f"claude_path {path!r} in {config_path.name} isn't an absolute path; {fix}")
    if not Path(path).is_file():
        raise ClaudeNotConfigured(f"claude_path {path} in {config_path.name} doesn't exist; {fix}")
    return str(path)


def run_claude(
    system_prompt_file: Path,
    user_prompt: str,
    *,
    tools: Sequence[str] = (),
    max_turns: int | None = None,
    deny: Sequence[str] = (),
    timeout: float = CALL_TIMEOUT_S,
) -> Reply:
    """One stripped-down `claude -p` call. Returns the parsed --output-format json reply."""
    cmd = [claude_exe(), *flags(tools, max_turns, deny), "--system-prompt-file", str(system_prompt_file)]
    # Run outside the project so nothing in it (CLAUDE.md, .claude/) can be picked up.
    proc = subprocess.run(
        cmd,
        input=user_prompt,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=system_prompt_file.parent,
    )
    try:
        reply = json.loads(proc.stdout)
    except json.JSONDecodeError:
        text = (proc.stdout or proc.stderr).strip()[:300]
        if _is_usage_limit(text):
            raise UsageLimitReached(text)
        raise ClaudeCallFailed(f"exit {proc.returncode}, not JSON: {text!r}")
    if reply.get("subtype") == "error_max_turns":
        raise MaxTurnsReached(f"used all {max_turns} turns without answering", reply)
    if reply.get("is_error") or proc.returncode != 0:
        text = str(reply.get("result") or reply.get("subtype") or "")[:300]
        if _is_usage_limit(text):
            raise UsageLimitReached(text)
        raise ClaudeCallFailed(f"exit {proc.returncode}: {text!r}")
    return reply


def call_with_retry(runner: Runner, prompt_file: Path, user_prompt: str, label: str,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[Reply, float]:
    """(reply, seconds). Retries failures with backoff; never a usage limit or max turns."""
    for attempt, delay in enumerate((*RETRY_DELAYS_S, None), 1):
        started = time.monotonic()
        try:
            return runner(prompt_file, user_prompt), time.monotonic() - started
        except MaxTurnsReached:
            raise
        except (ClaudeCallFailed, subprocess.TimeoutExpired, OSError) as exc:
            if delay is None:
                log.error("%s failed after %d attempts: %s", label, attempt, exc)
                raise ClaudeCallFailed(str(exc)) from exc
            log.warning("%s attempt %d failed (%s); retrying in %ds", label, attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")


@dataclass
class Usage:
    model: str = ""
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.output_tokens += other.output_tokens
        self.web_searches += other.web_searches
        self.cost_usd += other.cost_usd

    def fields(self) -> str:
        out = (
            f"input={self.input_tokens} cache_creation={self.cache_creation_tokens}"
            f" cache_read={self.cache_read_tokens} output={self.output_tokens}"
        )
        if self.web_searches:
            out += f" web_searches={self.web_searches}"
        return out + f" cost_usd={self.cost_usd:.4f}"


@dataclass
class UsageTotals:
    """Usage summed per model, e.g. over one step or a whole daily run."""

    by_model: dict[str, Usage] = field(default_factory=dict)
    calls: int = 0

    def add(self, usages: list[Usage]) -> None:
        self.calls += 1
        for u in usages:
            self.by_model.setdefault(u.model, Usage(model=u.model)).add(u)

    def merge(self, other: UsageTotals) -> None:
        self.calls += other.calls
        for u in other.by_model.values():
            self.by_model.setdefault(u.model, Usage(model=u.model)).add(u)

    def total(self) -> Usage:
        t = Usage(model="all")
        for u in self.by_model.values():
            t.add(u)
        return t


def parse_usage(reply: Reply) -> list[Usage]:
    """One Usage per model in the reply. A call with web tools also bills a small model
    (Haiku) that runs the searches, so there can be several."""
    out = []
    for model, u in (reply.get("modelUsage") or {}).items():
        out.append(Usage(
            model=model,
            input_tokens=int(u.get("inputTokens") or 0),
            cache_creation_tokens=int(u.get("cacheCreationInputTokens") or 0),
            cache_read_tokens=int(u.get("cacheReadInputTokens") or 0),
            output_tokens=int(u.get("outputTokens") or 0),
            web_searches=int(u.get("webSearchRequests") or 0),
            cost_usd=float(u.get("costUSD") or 0.0),
        ))
    if not out:  # older CLI versions only have the flat usage block
        u = reply.get("usage") or {}
        out.append(Usage(
            model="unknown",
            input_tokens=int(u.get("input_tokens") or 0),
            cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            web_searches=int((u.get("server_tool_use") or {}).get("web_search_requests") or 0),
            cost_usd=float(reply.get("total_cost_usd") or 0.0),
        ))
    return out


def log_usage(label: str, reply: Reply, seconds: float, totals: UsageTotals, extra: str = "") -> list[Usage]:
    """Log one `usage ...` line per model and add the reply's usage to totals."""
    usages = parse_usage(reply)
    totals.add(usages)
    turns = reply.get("num_turns")
    for u in usages:
        log.info("usage %s%s model=%s %s turns=%s duration_s=%.1f",
                 label, f" {extra}" if extra else "", u.model, u.fields(), turns, seconds)
    return usages


def extract_json(text: str, kind: type = list) -> Any:
    """The first JSON array (kind=list) or object (kind=dict) in text, ignoring prose
    and code fences around it."""
    opener = "[" if kind is list else "{"
    decoder = json.JSONDecoder()
    start = text.find(opener)
    while start >= 0:
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find(opener, start + 1)
            continue
        if isinstance(value, kind):
            return value
        start = text.find(opener, start + 1)
    raise ValueError(f"no JSON {kind.__name__} in reply: {text[:200]!r}")


def _is_usage_limit(text: str) -> bool:
    t = text.lower()
    return "usage limit" in t or "limit reached" in t or "rate limit" in t
