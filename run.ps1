<#
.SYNOPSIS
    Entry point for the job agent. Always runs the project's .venv Python from the project root.

.EXAMPLE
    .\run.ps1 help
    .\run.ps1 fetch --since-days 3
    .\run.ps1 show new
#>

# No param() block on purpose: arguments such as --since-days must reach Python untouched,
# and a param() block would make PowerShell try to bind them as its own parameters.
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# The dispatch table: one entry per command, in the order help lists them. Py is the
# Python argument list (extra arguments are appended); StatusArg moves a leading
# non-option argument to --status. help is generated from this table.
$commands = [ordered]@{
    "help"        = @{ Usage = "help";                     Help = "This list (also: no arguments, -h, --help)" }
    "daily"       = @{ Usage = "daily [args]";             Help = "The whole morning: fetch, track, score, details, pass 2, cleanup (stale waiting jobs, postings too old), shortlist, research, digest";
                       Py = @("-m", "agents.daily") }
    "fetch"       = @{ Usage = "fetch [args]";             Help = "Run Scout, e.g. fetch --since-days 3";
                       Py = @("-m", "agents.scout.fetch") }
    "show"        = @{ Usage = "show [status] [args]";     Help = "List jobs, optionally by status, e.g. show new --limit 20";
                       Py = @("-m", "agents.db", "show"); StatusArg = $true }
    "top"         = @{ Usage = "top [N]";                  Help = "The N highest-scoring jobs that aren't closed (default 15)";
                       Py = @("-m", "agents.db", "top") }
    "refilter"    = @{ Usage = "refilter [args]";          Help = "Re-apply the hard filters to jobs already in the database";
                       Py = @("-m", "agents.scout.refilter") }
    "posting-age" = @{ Usage = "posting-age [--dry-run]";  Help = "Close open jobs whose posting is too old (10 days; official and open 30, preferred 45); --dry-run previews";
                       Py = @("-m", "agents.scout.age") }
    "backfill-dates" = @{ Usage = "backfill-dates [--dry-run]"; Help = "One-time: real posting dates for open official jobs (description, ATS JSON, page metadata; no Claude)";
                       Py = @("-m", "agents.scout.backfill_dates") }
    "score"       = @{ Usage = "score [args]";             Help = "Score new jobs with Claude (claude -p, Sonnet), e.g. score --limit 30";
                       Py = @("-m", "agents.scout.score") }
    "rescore"     = @{ Usage = "rescore [args]";           Help = "Reset scored jobs to new and score them again (after prompt or weight changes)";
                       Py = @("-m", "agents.scout.score", "--rescore") }
    "details"     = @{ Usage = "details [args]";           Help = "Find official postings for the top 15 (free ATS check, then web search)";
                       Py = @("-m", "agents.scout.details") }
    "ats-check"   = @{ Usage = "ats-check <id> [<id> ...]"; Help = "Free ATS check (Greenhouse, Lever, Ashby APIs) for jobs; --refresh retries cached misses";
                       Py = @("-m", "agents.scout.ats_api") }
    "retrust"     = @{ Usage = "retrust [args]";           Help = "Re-check source trust (official/aggregator) of open jobs from both links, after editing source_trust.yaml";
                       Py = @("-m", "agents.scout.retrust") }
    "set-posting" = @{ Usage = "set-posting <id> <url>";   Help = "Give a job's official posting URL: stored, rescored (pass 2), brief rewritten";
                       Py = @("-m", "agents.scout.posting") }
    "pass2"       = @{ Usage = "pass2 [args]";             Help = "Rescore the top 15 jobs from their full postings";
                       Py = @("-m", "agents.scout.pass2") }
    "shortlist"   = @{ Usage = "shortlist [args]";         Help = "Shortlist up to 3 official jobs scoring at least shortlist_min_score";
                       Py = @("-m", "agents.scout.shortlist") }
    "research"    = @{ Usage = "research [id] [args]";     Help = "Write briefs for shortlisted jobs (or one job), e.g. research 40";
                       Py = @("-m", "agents.research.brief") }
    "digest"      = @{ Usage = "digest [args]";            Help = "Write digest/YYYY-MM-DD.md (no run stats; daily writes it with them)";
                       Py = @("-m", "agents.digest") }
    "review"      = @{ Usage = "review [--static]";        Help = "Open the review app (127.0.0.1 only): tabs, Read panel, paste a posting URL, shortlist now; --static rewrites review\index.html";
                       Py = @("-m", "agents.review_app") }
    "rate"        = @{ Usage = "rate <id> up|down [note]"; Help = "Thumbs up or down; adds the job to profile.yaml feedback examples";
                       Py = @("-m", "agents.feedback", "rate") }
    "skip"        = @{ Usage = "skip <id> `"reason`"";     Help = "Close a job you won't pursue";
                       Py = @("-m", "agents.feedback", "skip") }
    "hold"        = @{ Usage = "hold <id> `"reason`"";     Help = "Put a job on hold: out of the shortlist and Still waiting; --until YYYY-MM-DD brings it back that day";
                       Py = @("-m", "agents.feedback", "hold") }
    "unhold"      = @{ Usage = "unhold <id>";              Help = "Take a job off hold";
                       Py = @("-m", "agents.feedback", "unhold") }
    "track"       = @{ Usage = "track [args]";             Help = "Read application emails and update applied jobs, e.g. track --since-days 7 (--dry-run: headers only)";
                       Py = @("-m", "agents.tracker.track") }
    "reopen"      = @{ Usage = "reopen <id> `"reason`"";   Help = "Take back a close (skip, stale: no action): closed -> scored (you only)";
                       Py = @("-m", "agents.feedback", "reopen") }
    "stats"       = @{ Usage = "stats [--days N]";         Help = "What the agent saw and did in the last N days (default 7); --json for tuning";
                       Py = @("-m", "agents.stats") }
    "post-ideas"  = @{ Usage = "post-ideas";               Help = "Propose 5 LinkedIn post ideas from your public sources (drafts\post-ideas-YYYY-MM-DD.md)";
                       Py = @("-m", "agents.publisher.ideas") }
    "draft-post"  = @{ Usage = "draft-post <n> [<n> ...]"; Help = "Write post ideas (at most 3) and save them to Typefully as LinkedIn drafts; never publishes";
                       Py = @("-m", "agents.publisher.draft") }
    "gmail-check" = @{ Usage = "gmail-check";              Help = "List the 10 newest JobAlerts message headers";
                       Py = @("-m", "agents.common.gmail", "check") }
}

function Show-Help {
    $width = ($commands.Values | ForEach-Object { $_.Usage.Length } | Measure-Object -Maximum).Maximum + 2
    "Usage: .\run.ps1 <command> [args...]"
    ""
    "Commands:"
    foreach ($name in $commands.Keys) {
        "  " + $commands[$name].Usage.PadRight($width) + $commands[$name].Help
    }
    ""
    "Any extra arguments are passed through to Python."
}

if ($args.Count -eq 0 -or $args[0] -in @("help", "-h", "--help", "/?")) {
    Show-Help
    exit 0
}

$command = "$($args[0])"
$rest = @($args | Select-Object -Skip 1)
if (-not $commands.Contains($command)) {
    Write-Error "Unknown command '$command'."
    Show-Help
    exit 2
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "No virtual environment at $python. Create it with: py -3.12 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$entry = $commands[$command]
$pyArgs = @($entry.Py)
if ($entry.StatusArg -and $rest.Count -gt 0 -and -not "$($rest[0])".StartsWith("-")) {
    $pyArgs += @("--status", $rest[0])
    $rest = @($rest | Select-Object -Skip 1)
}
$pyArgs += $rest

Push-Location -LiteralPath $PSScriptRoot
try {
    & $python @pyArgs
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $code
