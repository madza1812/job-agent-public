"""Propose 5 LinkedIn post ideas from the owner's public sources.

    python -m agents.publisher.ideas [--dry-run]

Reads only agents.publisher.sources (resume, job-agent-public's README and git log,
profile/post_notes.md), makes one stripped `claude -p` call (Sonnet, no tools, prompt
prompts/post_ideas.md), and writes drafts/post-ideas-YYYY-MM-DD.md: a numbered list, one
line per idea. An idea the guard's content checks refuse (a company, a security detail, a
number not in the sources, ...) is retried once with the problems listed.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agents.common import claude, runlog
from agents.publisher import guard as guard_mod
from agents.publisher import sources as sources_mod

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DRAFTS_DIR = ROOT / "drafts"
PROMPT = ROOT / "prompts" / "post_ideas.md"
IDEA_COUNT = 5
LOG_NAME = "publisher"
_IDEA_LINE = re.compile(r"^(\d+)\. \*\*(.+?)\*\* — (.+)$")


@dataclass
class Idea:
    title: str
    line: str


def ideas_path(day: date | None = None, drafts_dir: Path = DRAFTS_DIR) -> Path:
    return drafts_dir / f"post-ideas-{(day or date.today()).isoformat()}.md"


def latest_ideas_path(drafts_dir: Path = DRAFTS_DIR) -> Path | None:
    found = sorted(drafts_dir.glob("post-ideas-*.md"))
    return found[-1] if found else None


def parse_reply(text: str) -> list[Idea]:
    data = claude.extract_json(text, dict)
    items = data.get("ideas")
    if not isinstance(items, list) or len(items) != IDEA_COUNT:
        raise ValueError(f"expected {IDEA_COUNT} ideas, got {len(items) if isinstance(items, list) else items!r}")
    out = []
    for item in items:
        title = " ".join(str((item or {}).get("title") or "").split())
        line = " ".join(str((item or {}).get("line") or "").split())
        if not title or not line:
            raise ValueError(f"an idea without a title or line: {item!r}")
        out.append(Idea(title=title.strip("*"), line=line))
    return out


def render(ideas: list[Idea], day: date) -> str:
    lines = [f"# Post ideas — {day.isoformat()}", "",
             "From the resume, job-agent-public's README and git log, and profile/post_notes.md.", ""]
    lines += [f"{i}. **{idea.title}** — {idea.line}" for i, idea in enumerate(ideas, 1)]
    lines += ["", r"Draft one (up to 3 per run): `.\run.ps1 draft-post <n>`", ""]
    return "\n".join(lines)


def read_ideas(path: Path) -> dict[int, Idea]:
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _IDEA_LINE.match(raw.strip())
        if m:
            out[int(m.group(1))] = Idea(title=m.group(2), line=m.group(3))
    return out


def propose(srcs: sources_mod.Sources, guard: guard_mod.Guard,
            runner: claude.Runner = claude.run_claude) -> list[Idea]:
    totals = claude.UsageTotals()
    base = srcs.as_prompt()
    prompt = base
    for attempt in (1, 2):
        reply, seconds = claude.call_with_retry(runner, PROMPT, prompt, "post ideas")
        claude.log_usage("publisher step=ideas", reply, seconds, totals)
        try:
            ideas = parse_reply(str(reply.get("result") or ""))
        except ValueError as exc:
            problems = [str(exc)]
        else:
            problems = [f"idea {i}: {p}" for i, idea in enumerate(ideas, 1)
                        for p in guard.check_content(f"{idea.title}. {idea.line}", srcs.all_text())]
            if not problems:
                return ideas
        log.warning("Post ideas attempt %d rejected: %s", attempt, "; ".join(problems))
        prompt = base + "\n\nYour earlier reply had these problems; fix every one:\n- " + "\n- ".join(problems)
    raise ValueError("post ideas failed the checks twice:\n  " + "\n  ".join(problems))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.publisher.ideas", description=__doc__.split("\n")[0])
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true", help="show the sources' sizes; call nothing")
    args = parser.parse_args(argv)

    with runlog.log_to(runlog.log_path(LOG_NAME, args.log_dir)) as log_file:
        try:
            srcs = sources_mod.load()
            if args.dry_run:
                print(f"Sources: resume {len(srcs.resume.split())} words, README {len(srcs.readme.split())} words,"
                      f" git log {len(srcs.git_log.splitlines())} commits, notes {len(srcs.notes.split())} words")
                return 0
            ideas = propose(srcs, guard_mod.load())
        except (claude.ClaudeUnavailable, claude.ClaudeCallFailed, ValueError, OSError, RuntimeError) as exc:
            log.error("Post ideas failed: %s", exc)
            print(f"Error: {exc}")
            return 1
        day = date.today()
        path = ideas_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = render(ideas, day)
        path.write_text(text, encoding="utf-8")
        log.info("Post ideas written to %s", path)
    print(text)
    print(f"Saved: {path.relative_to(ROOT)}")
    print(f"Log: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
