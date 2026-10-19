"""Write post ideas as LinkedIn posts and save them to Typefully as drafts.

    python -m agents.publisher.draft <n> [<n> ...] [--ideas FILE] [--dry-run]

<n> is an idea's number in the newest drafts/post-ideas-*.md (or --ideas FILE). At most
MAX_PER_RUN ideas a run. For each: one stripped `claude -p` call (Sonnet, no tools, prompt
prompts/post_draft.md) with the idea and the public sources, then the guard's checks
(agents/publisher/guard.py). A post that fails is rewritten once with the problems listed;
if it still fails it's saved locally only and not sent. A passing post goes to Typefully as
a LinkedIn draft (never publish_at: you publish by hand), is printed, and is saved to
drafts/post-YYYY-MM-DD-<n>.md.

The free plan publishes 10 posts a month. Before any Claude call, the run reads Typefully's
publishing_quota and counts this month's drafts in drafts/typefully-drafts.jsonl; it stops
when the quota is used up or the drafts would pass MONTHLY_LIMIT, and logs both counts.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from agents.common import claude, runlog
from agents.publisher import guard as guard_mod
from agents.publisher import ideas as ideas_mod
from agents.publisher import sources as sources_mod
from agents.publisher import typefully

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "prompts" / "post_draft.md"
LEDGER_NAME = "typefully-drafts.jsonl"
MAX_PER_RUN = 3
MONTHLY_LIMIT = 10


class LimitReached(RuntimeError):
    pass


@dataclass
class Result:
    saved: list[tuple[int, typefully.Draft | None, str]] = field(default_factory=list)  # (n, draft, text)
    rejected: list[tuple[int, list[str]]] = field(default_factory=list)


def month_count(ledger: Path, today: date | None = None) -> int:
    """Drafts this Publisher saved to Typefully in the current (local) month."""
    if not ledger.is_file():
        return 0
    month = (today or date.today()).strftime("%Y-%m")
    count = 0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("month") == month:
                count += 1
        except json.JSONDecodeError:
            continue
    return count


def record(ledger: Path, n: int, idea: ideas_mod.Idea, draft: typefully.Draft) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    entry = {"month": date.today().strftime("%Y-%m"), "saved_at": datetime.now().isoformat(timespec="seconds"),
             "idea": n, "title": idea.title, "draft_id": draft.id, "private_url": draft.private_url}
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def check_limits(client: typefully.Typefully, set_id: int, ledger: Path, wanted: int) -> str:
    """Raise LimitReached if this run would pass the plan; otherwise the line to log."""
    quota = client.quota(set_id)
    drafted = month_count(ledger)
    line = (f"Monthly count: {quota.used} published, {quota.remaining} left (resets {quota.resets_at});"
            f" {drafted}/{MONTHLY_LIMIT} drafts saved by the Publisher this month")
    log.info(line)
    if quota.remaining <= 0:
        raise LimitReached(f"Typefully's publishing quota is used up until {quota.resets_at}. {line}")
    if drafted + wanted > MONTHLY_LIMIT:
        raise LimitReached(f"{wanted} more would pass {MONTHLY_LIMIT} drafts this month. {line}")
    return line


def write_post(idea: ideas_mod.Idea, srcs: sources_mod.Sources, guard: guard_mod.Guard,
               runner: claude.Runner = claude.run_claude) -> tuple[str, list[str]]:
    """(text, problems): problems is empty when the post passed the guard."""
    totals = claude.UsageTotals()
    base = f"<idea>\n{idea.title}: {idea.line}\n</idea>\n\n{srcs.as_prompt()}"
    prompt, text = base, ""
    for attempt in (1, 2):
        reply, seconds = claude.call_with_retry(runner, PROMPT, prompt, "post draft")
        claude.log_usage("publisher step=draft", reply, seconds, totals)
        try:
            text = str(claude.extract_json(str(reply.get("result") or ""), dict).get("text") or "").strip()
        except ValueError as exc:
            problems = [str(exc)]
        else:
            problems = guard.check(text, srcs.all_text()) if text else ["the reply had no text"]
            if not problems:
                return text, []
        log.warning("Post draft attempt %d rejected: %s", attempt, "; ".join(problems))
        prompt = (base + "\n\n<earlier_version>\n" + text + "\n</earlier_version>\n\n"
                  "That version had these problems; fix every one:\n- " + "\n- ".join(problems))
    return text, problems


def save_local(drafts_dir: Path, n: int, idea: ideas_mod.Idea, text: str, note: list[str]) -> Path:
    path = drafts_dir / f"post-{date.today().isoformat()}-{n}.md"
    head = [f"# {idea.title}", "", f"Idea {n}: {idea.line}", "", *note, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(head) + "---\n\n" + text + "\n", encoding="utf-8")
    return path


def run(numbers: list[int], ideas_file: Path, *, dry_run: bool = False, drafts_dir: Path = ideas_mod.DRAFTS_DIR,
        srcs: sources_mod.Sources | None = None, guard: guard_mod.Guard | None = None,
        client: typefully.Typefully | None = None, runner: claude.Runner = claude.run_claude,
        config_path: Path = typefully.LOCAL_CONFIG) -> Result:
    if not numbers:
        raise ValueError("give at least one idea number")
    if len(numbers) > MAX_PER_RUN:
        raise ValueError(f"at most {MAX_PER_RUN} drafts per run, got {len(numbers)}")
    if len(set(numbers)) != len(numbers):
        raise ValueError("an idea number is repeated")
    available = ideas_mod.read_ideas(ideas_file)
    missing = [n for n in numbers if n not in available]
    if missing:
        raise ValueError(f"no idea {', '.join(map(str, missing))} in {ideas_file.name} (it has {sorted(available)})")

    ledger = drafts_dir / LEDGER_NAME
    set_id = 0
    if not dry_run:
        client = client or typefully.Typefully(typefully.api_key())
        set_id = typefully.social_set_id(client, config_path)
        check_limits(client, set_id, ledger, len(numbers))
    srcs = srcs or sources_mod.load()
    guard = guard or guard_mod.load()

    result = Result()
    for n in numbers:
        idea = available[n]
        text, problems = write_post(idea, srcs, guard, runner)
        if problems:
            save_local(drafts_dir, n, idea, text,
                       ["**Not saved to Typefully.** It failed the checks:", *[f"- {p}" for p in problems]])
            result.rejected.append((n, problems))
            continue
        if dry_run:
            save_local(drafts_dir, n, idea, text, ["Dry run: not sent to Typefully."])
            result.saved.append((n, None, text))
            continue
        # On disk first, so the text survives a failed upload.
        save_local(drafts_dir, n, idea, text, ["Passed the checks; not yet saved to Typefully."])
        draft = client.create_draft(set_id, text)
        record(ledger, n, idea, draft)
        log.info("Idea %d saved to Typefully as draft %d", n, draft.id)
        save_local(drafts_dir, n, idea, text, [f"Typefully draft {draft.id}: {draft.private_url}"])
        result.saved.append((n, draft, text))
    if not dry_run and result.saved:
        log.info("Monthly count: %d/%d drafts saved by the Publisher this month", month_count(ledger), MONTHLY_LIMIT)
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m agents.publisher.draft", description=__doc__.split("\n")[0])
    parser.add_argument("numbers", type=int, nargs="+", help=f"idea numbers (at most {MAX_PER_RUN})")
    parser.add_argument("--ideas", type=Path, default=None, help="ideas file (default: the newest post-ideas-*.md)")
    parser.add_argument("--log-dir", type=Path, default=runlog.DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true", help="write and check the posts; don't call Typefully")
    args = parser.parse_args(argv)

    ideas_file = args.ideas or ideas_mod.latest_ideas_path()
    if ideas_file is None or not ideas_file.is_file():
        print(r"Error: no post ideas yet; run .\run.ps1 post-ideas first")
        return 2
    with runlog.log_to(runlog.log_path(ideas_mod.LOG_NAME, args.log_dir)) as log_file:
        try:
            result = run(args.numbers, ideas_file, dry_run=args.dry_run)
        except ValueError as exc:
            print(f"Error: {exc}")
            return 2
        except (LimitReached, typefully.TypefullyError, typefully.ForbiddenRequest, claude.ClaudeUnavailable,
                claude.ClaudeCallFailed, OSError, RuntimeError) as exc:
            log.error("draft-post failed: %s", exc)
            print(f"Error: {exc}")
            return 1
    for n, draft, text in result.saved:
        where = f"Typefully draft {draft.id}: {draft.private_url}" if draft else "dry run, not sent"
        print(f"=== Idea {n} ({where}) ===\n\n{text}\n\n({len(text.split())} words)\n")
    for n, problems in result.rejected:
        print(f"=== Idea {n}: NOT saved to Typefully, it failed the checks twice ===")
        print("\n".join(f"  - {p}" for p in problems))
        print(f"  Text kept in drafts/post-{date.today().isoformat()}-{n}.md\n")
    print(f"Log: {log_file}")
    return 1 if result.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
