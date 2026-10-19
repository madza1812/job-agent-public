# CLAUDE.md

Local multi-agent job search assistant. Runs on Windows with Python 3.12 and SQLite; scripts and scheduling use PowerShell.

## Hard rules

- **Never send emails.** Gmail access is read-only. Don't request or use send, draft, modify, or delete scopes.
- **Never submit applications.** Don't fill or submit application forms, and don't call any endpoint that applies on the user's behalf.
- **Only the user marks a job `applied`.** No agent or automated code path may set a job's status to `applied`. The user does it by hand.
- **Never publish or schedule posts.** The Publisher only creates Typefully drafts: no request may carry `publish_at` or `plan_at`, and only GETs and draft creation are allowed. The user publishes by hand in Typefully.
- **Posts use public sources only.** The Publisher never reads `data/`, `briefs/`, `digest/` or the database into a prompt, and never mentions job applications, companies being applied to, recruiters, or confidential details about the user's employer.
- Never commit credentials, OAuth tokens, `.env` files, or the SQLite database (see `.gitignore`).

## Layout

| Path | Purpose |
|---|---|
| `run.ps1` | Entry point for every command (see [Running](#running)) |
| `agents/` | Agent code: Scout, Research analyst, Tracker, Publisher |
| `profile/profile.yaml` | The user's profile. Scout filters and scores jobs against it, and builds its search queries from it (gitignored; `scripts/setup.ps1` copies it from `profile.example.yaml`) |
| `profile/resume.md` | Optional: the user's resume, a source for `profile.yaml` (gitignored) |
| `profile/post_notes.md` | Optional notes for LinkedIn posts, read by the Publisher (gitignored) |
| `profile/preferred_companies.yaml` | Preferred companies with tiers. A scoring reference, not a fetch list (gitignored; copied from `preferred_companies.example.yaml`) |
| `profile/*.example.yaml` | Fictional examples of both files, the only files in `profile/` that are committed |
| `secrets/` | Gmail OAuth client (`credentials.json`) and token (`token.json`), gitignored |
| `tools/` | Developer tools, e.g. `sanitize_email.py` for test fixtures |
| `prompts/` | Claude instructions: `score_pass1.md`, `details.md`, `score_pass2.md`, `research_company.md`, `research_role.md` (holds the brief template), `track_classify.md`, `post_ideas.md`, `post_draft.md` |
| `config/source_trust.yaml` | ATS (optionally with a path), public-sector and company domains that count as official; domains the details lookup may never fetch; ATS sender domains for the tracker |
| `config/publisher.yaml` | The Publisher guard's lists: hype words, security terms, application phrases |
| `config/local.yaml` | Machine-specific settings (`claude_path`; also `typefully_social_set_id`, optional `public_repo`), written by `scripts/setup.ps1`, gitignored |
| `.env` | `TYPEFULLY_API_KEY` for the Publisher (gitignored) |
| `scripts/` | `setup.ps1` (set up or repair a machine) and `export_bundle.ps1` (zip for moving machines) |
| `briefs/` | Research briefs written by the Research analyst (gitignored: they quote the profile) |
| `review/` | `index.html`, the static review page for reading on a phone (gitignored; rebuilt by `daily` and `.\run.ps1 review --static`) |
| `digest/` | Digest output |
| `drafts/` | Publisher output: post ideas, post texts and the monthly draft ledger (gitignored) |
| `data/` | SQLite database (`data/*.db`, gitignored), the daily run's lock file (`data/daily.lock`, gitignored) and `data/backups/` (pre-migration copies, gitignored) |
| `logs/` | Run logs (gitignored), kept 30 days |
| `.venv/` | Python 3.12 virtual environment (gitignored) |

## Agents

### Scout
Run with `.\run.ps1 fetch` (wraps `python -m agents.scout.fetch`). Code is in `agents/scout/`.

- **Discovery:**
  - **Job alert emails are the primary source.** Scout reads Gmail messages matching `label:JobAlerts newer_than:<N>d` (`--since-days`, default 3) through `agents/common/gmail.py`. That module only ever requests `gmail.readonly`, and refuses a stored token with any other scope.
  - OAuth: the client is `secrets/credentials.json`, and the token is `secrets/token.json`. The first consent is `python -m agents.common.gmail auth`; `.\run.ps1 gmail-check` lists the 10 newest JobAlerts headers (never bodies). Scheduled runs never open a browser.
  - `agents/scout/email_alerts.py` matches parsers by exact sender address: `jobalerts-noreply@linkedin.com` and `jobs-noreply@linkedin.com` (LinkedIn), `donotreply@match.indeed.com` and `donotreply@jobalert.indeed.com` (Indeed matches and digests), `noreply@glassdoor.com` (Glassdoor). Workday-style and Taleo-style parsers come later, once real emails exist.
  - Senders in `IGNORED_SENDERS` (`invitations@linkedin.com`, `updates-noreply@linkedin.com`) reach the label but never carry jobs. Their emails are recorded in `processed_emails` with `jobs_found = 0` and counted as "ignored", not unparsed.
  - Sponsored cards in Indeed digests (`/pagead/clk/dl` links) carry only an opaque ad token, no job key, and following the tracking redirect is off limits. They're stored with `sponsored = 1`, `needs_details = 1`, an Indeed search link (`indeed.com/jobs?q=<title> <company>&l=<location>`) as `url`, and the ID `email:indeed:sponsored-<hash of company|title|location>`.
  - Write a parser only against real alert emails. Save them first with `python -m tools.sanitize_email "<gmail query>" <name>`, which writes `tests/fixtures/email_<name>_<n>.json`. It redacts tracking tokens (every query value except job IDs: Indeed `jk`, Glassdoor `jobListingId`; HTML `href`/`src` values are cleaned whole, since some senders put raw spaces in links), refuses to write a fixture with any other unredacted query value, and replaces the Gmail address, `identity.name` and the pairs in `sanitize.replace` in `profile/profile.yaml`. Keep personal terms there (gitignored), never in code or tests. Review fixtures before committing.
  - A parser extracts title, company, location and job link. Links are cleaned to the plain job page (`linkedin.com/jobs/view/<id>/`, `indeed.com/viewjob?jk=<key>`, `glassdoor.com/job-listing/j?jl=<id>`). Indeed's redirect token is decoded locally; never request tracking links.
  - Email jobs get `source = email:<type>`, an empty description and `needs_details = 1`. The Research analyst must open the link for the full posting.
  - **Age badges:** Indeed digests and Glassdoor alerts show one on each card ("Just posted", "Today", "3 days ago", "30+ days ago"; Glassdoor "10h", "3d", "30d+"; `email_alerts.age_badge`). It becomes `posted_at` counted back from the email's date, with `posted_at_source = alert_badge` ("30+" counts as 30). LinkedIn alerts and Indeed's single-job matches show none (checked against the fixtures), so those jobs get the email's date (`email_date`).
  - The `processed_emails` table makes sure an email is never parsed twice. Indeed's "is now active" confirmations are skipped and recorded. An email from a sender with no parser is logged by address and not treated as a failure. Such emails, and emails whose parser fails or finds nothing, aren't recorded, so a later run retries them. They do go to `unparsed_emails` (for stats), and each new posting a hard filter rejects goes to `filtered_postings`.
  - The summary has one line per sender: emails read, jobs found, unparsed (and skipped).
  - **Expired Gmail login:** non-interactive runs raise `GmailAuthExpired` (refresh fails, or the API answers 401) instead of opening a browser. Scout records the `gmail_auth_expired` notice in the `notices` table and still runs the feeds. A later run that reads Gmail successfully clears the notice.
- **ATS feeds are an addition.** Only companies in `preferred_companies.yaml` with `poll_feed: true` (plus `ats` and `slug`) are fetched from the **public** Greenhouse, Lever or Ashby board APIs. Feeds run first, so their fuller copy wins over an email duplicate. No scraping of logged-in pages.
- **Failing:** the runner exits with an error only when no discovery source is configured (no Gmail token and no `poll_feed` company). An empty preferred list is fine.
- **Preferred companies** are a scoring reference.
  - `companies.normalize_company(name)` is the one comparison key for company names, used by preferred matching, `dedupe_key` and `exclude_companies`: lowercase, drop punctuation, a leading "the" and trailing suffixes (Inc, LLC, Ltd, Corp, Global Tech, Technologies, …), so "Globex Global Tech" matches "Globex". The first word always stays. The original name is what's stored and shown.
  - `companies.match_preferred(name)` checks each company's normalized name and aliases.
  - It stores `preferred_match` and `preferred_tier` (`dream` / `strong` / `interested`) on the job, and refreshes them whenever the job is seen again.
- **Filters:** hard filters (`filters.py`) drop only postings that clearly don't fit, and unknown data passes. Never filter on salary when `salary_is_predicted` is set (a job site's estimate). A posting older than its age limit (see Posting age) is filtered at fetch.
  - `exclude_title_words` match whole words, case-insensitive. A seniority word also catches its abbreviations: `senior` catches Sr, Sr. and Snr (`_SENIORITY`).
  - `require_title_words`: a title must contain at least one (whole word, case-insensitive; a multi-word term also matches hyphenated, "full stack" ↔ "full-stack"). An empty or missing list turns the filter off.
- **Statuses:** inserts new jobs as `new` and moves them to `scored` once scored. Scoring never shortlists.
- **Posting age** (`agents/scout/age.py`; `.\run.ps1 posting-age --dry-run` previews it):
  - **Where the date came from** (`jobs.posted_at_source`, migration 18, which backfilled email jobs as `email_date` and feed jobs as `ats`): `ats` (an ATS's data: feeds, the free board check, Workable/BambooHR/Breezy JSON), `posting_page` (the posting page's JSON-LD `datePosted`, or the date the details lookup read on an official page), `alert_badge`, or `email_date` (the alert email's date: the posting is at least that old, so the date is unverified).
  - **Better dates win** (`db.offer_posted_at`, used by upsert, `touch_job` and `store_details`): ats/posting_page beat alert_badge, which beats email_date. A worse source never replaces a better one, and alert dates never replace each other (the first sighting is the best bound); a posting's date replaces another posting's. Dates from an aggregator's page are ignored.
  - **Limits** (`profile.yaml` `hard_filters`; `age.limit_for`): `max_posting_age_days` (10). For a posting confirmed open (`source_trust` `official`, which feed jobs are, and pass 2 doesn't say possibly closed): `max_official_open_age_days` (30), or `max_official_open_age_days_preferred` (45) when the company matches `preferred_companies.yaml` (`jobs.preferred_match`; at fetch the match is made before the filters).
  - **Closing:** a `new`, `scored`, `shortlisted` or `researched` job over its limit is closed by scout with detail `posting too old: <N> days`. Held jobs are never closed (the app and digest flag them: "older posting, check it's still open"), applied and interviewing jobs never checked, and a job the user reopened after such a close is left alone. It runs in the daily run's cleanup step and whenever a better date arrives: the details lookup (given the profile), set-posting (after pass 2), and the review app's Find posting and paste URL.
  - The shortlist and Shortlist now refuse a job over its limit. A job with only an `email_date` is allowed, and its label says "date unverified".
  - **Backfill** (`.\run.ps1 backfill-dates [--dry-run]`, `agents/scout/backfill_dates.py`, one-time, no Claude): for each open job with `source_trust` `official`, a date stated in the stored description ("Posted Aug 21, 2026", "Date posted: 2026-08-21"; `posting_page`), else the official posting read again: the ATS's public JSON (Workable/BambooHR/Breezy via `posting.api_posting`; Greenhouse, Lever and Ashby job APIs; `ats`), else the page's JSON-LD `datePosted` or published-date metadata (`posting.page_date`, which set-posting uses too; `posting_page`). Dates are stored by the better-source rule with a `posted_at_backfilled` event, then the age check runs. `--dry-run` reads the pages but writes nothing (not even the migration) and lists each job's old and new date, source, age and whether it would close; a real run backs up the database before migrating.
  - **Labels** (`age.label`, in the app and the digest): "Posted 12 days ago (from posting)", "Posted ~3 days ago (alert)", "Posted at least 2 days ago (email date, date unverified)" or "Date unknown".
- **Scoring, pass 1** (`.\run.ps1 score [--limit N] [--batch-size N] [--dry-run]`, `agents/scout/score.py`): rates `new` jobs against `profile.yaml` in batches, one `claude -p` call per batch, and sets them `scored`. `scored` jobs without a `score_detail` are rescored in place.
  - `.\run.ps1 rescore [--dry-run]` resets every `scored` job to `new` (clearing its score), then scores them all again. Use it after changing `prompts/score_pass1.md` or `scoring_weights`. Never touches `shortlisted` or later.
  - Claude's instructions, including the scale for each rated component, are in `prompts/score_pass1.md`. Claude rates only `skills_match`, `seniority_fit`, `role_interest`, `company_similarity`, `employer_quality` (0-100), `staffing_agency` and `dealbreaker`. `compute_final()` computes the final score in code and stores the breakdown as JSON in `jobs.score_detail`. It's a weighted average with `scoring_weights`: `company_pref` = tier score (dream 100 / strong 80 / interested 65) when the job matched a preferred company, otherwise `company_similarity`; `location` = 100 for a matching location or remote (`filters.location_matches`), otherwise 50, unless `profile.yaml` has `commute` (`score.location_score`): then a match scores `commute.remote`, or its place's entry under `commute.places` (a flat score, or scores by office days per week with an `unknown` fallback; office days come from pass 2, and 0 counts as remote), taking the best when several places match; `compensation` = 50 when unknown (100 at or above `min_salary`, 20 below). Then −10 for a staffing agency and −5 if sponsored, clamped to 0-100. A dealbreaker caps the score at 20.
  - The prompt includes `profile.yaml` `feedback.liked_examples` / `disliked_examples` (from `.\run.ps1 rate`).
- **Details** (`.\run.ps1 details [--dry-run]`, `agents/scout/details.py`, prompt `prompts/details.md`): among the top 15 `scored` jobs, it looks up those with `needs_details = 1` or low confidence (description under 800 characters) and no `details_status` yet, one call per job.
  - **Free check first** (`details.free_check`, no Claude call, no tokens):
    - **Cached board on another ATS** (JazzHR, Workable, …; `company_boards`, keyed by `normalize_company`): `posting.find_on_board()` reads the board's HTML for the exact title, or for a JavaScript-rendered board its JSON listing (`posting.board_listing`: Workable `api/v1/widget/accounts/{account}`, BambooHR `/careers/list`, Breezy `/json`). When a title is listed more than once, the location decides; if it can't, the check gives up.
    - **Otherwise the public APIs** (`agents/scout/ats_api.py`): Greenhouse `boards-api.greenhouse.io/v1/boards/{slug}/jobs`, Lever `api.lever.co/v0/postings/{slug}?mode=json`, Ashby `api.ashbyhq.com/posting-api/job-board/{slug}`.
      - It uses the cached board, or else board-name guesses from the normalized company name (`slug_guesses`: joined, first word, hyphenated, …).
      - **A board counts** only if it returns jobs and names the company: Greenhouse's board name; for Lever and Ashby, which name no company, the name (case-sensitive) must appear in at least half of the postings.
      - **Title match:** similarity ≥ 0.85 (difflib) and a compatible location (`location_score`): same city, then a US or unspecified remote copy. Another country's copy never counts, since companies post one title per country.
      - **Cache:** hits and misses per company in `ats_probes`. Hits also go to `company_boards`. A miss is retried after 30 days.
    - A match is stored as an `official` posting (ATS `source_url`) with its verbatim description, and flagged for pass 2.
    - Pool jobs that are aggregator-only or not found (they can't be shortlisted), or possibly closed with a cached board, get this free check too.
    - `.\run.ps1 ats-check <id> [<id> ...] [--refresh] [--dry-run]` runs it for given jobs, prints what each board guess returned, stores matches and reruns pass 2 for them.
    - Tests never reach the internet (`tests/conftest.py` makes real HTTP fail); use a fake session.
  - **Otherwise one Claude call:** same minimal flags, but `--tools WebSearch,WebFetch` (pre-approved with `--allowedTools`) and `--max-turns 10`. It only reads pages.
    - Search order in `details.md`: the known board, then the ATS domains (filled in from `config/source_trust.yaml`), the careers site, public-sector boards, and only then other job boards.
    - A job board marking a posting closed is not proof.
    - WebFetch sees an empty page on Workable, BambooHR and Breezy, so the prompt lets Claude return such a URL with no description, and `details.with_api_posting()` replaces the find with that ATS's JSON (see JavaScript-rendered boards below).
  - **JavaScript-rendered boards** (`posting.api_url_for` / `api_posting`): postings on these ATSs are read from public JSON instead of the page, by set-posting, the free check and the Claude lookup alike.
    - Workable `apply.workable.com/{account}/j/{shortcode}` → `apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}`: description + requirements HTML, location from `locations`/`workplace`, date from `published`. A short `apply.workable.com/j/{shortcode}` link names no account and can't be read.
    - BambooHR `{company}.bamboohr.com/careers/{id}` → `/careers/{id}/detail` (`locationType` "1" or `isRemote` is remote, "2" hybrid).
    - Breezy `{company}.breezy.hr/p/{id}-…` → `/json?verbose=true`, picked by id (the page is server-rendered, but its description isn't where the parser looks).
    - The location gets "(Remote)" or "(Hybrid)"; remote stores `jobs.office_days = 0`, which beats pass 2's `office_days` in the location score. Hybrid and on-site days are left to pass 2. Location replaces the stored one, and `posted_at` (as `ats`) any date that came from an alert.
    - Checked and left on HTML: Greenhouse, Lever and Ashby pages carry JSON-LD (and `ats_api` reads their boards); Recruitee and JazzHR render on the server.
  - **Board cache:** an `official` find on an ATS caches the company's board (`posting.board_url_for`: JazzHR, Greenhouse, Lever, Ashby, Workable, BambooHR, Breezy, Recruitee and SmartRecruiters; not Workday, which needs JavaScript). A board the user set is never replaced by a lookup's.
  - Any new official posting flags `needs_pass2` in `score_detail`, so pass 2 rescores the job.
  - `source_trust` (`agents/scout/source_trust.py`; domain lists in `config/source_trust.yaml`, never in code):
    - A found posting has two links, `source_url` (the page read) and `apply_url`. It's `official` when either one is (`source_trust.classify_posting`, `source_url` first), and the official link is stored as `apply_url` unless the apply link is official itself.
    - `official`: the company's own domain (by name match or `company_domains`), a known ATS, or a public-sector board. An `ats_domains` entry may carry a path, counting only URLs under it: Oracle Recruiting Cloud is `oraclecloud.com/hcmUI/CandidateExperience`. Salary and `still_open` are used only when `source_url` itself is official: a closed posting closes the job (scout, detail `posting closed`).
    - `aggregator`: anything else, including LinkedIn and Indeed. The description is used for scoring only, salary stays unknown, `still_open` is ignored, and the digest labels the link "aggregator, verify before applying".
    - `not_found`: no posting found, out of turns, or a reply that says found but has no description or link. The job is kept and retried once on a later day's run, then never again (`details_attempts`).
    - Feed jobs are `official` from the start.
  - A found posting stores the plain-text `description`, `apply_url`, `source_url` and `years_required`, then sets `needs_details = 0` and `details_status = 'found'`. The lookup also returns the date the page says the job was posted (`posted_date`; the prompt gets today's date to count back "posted 5 days ago"); like salary, it's kept only from an official `source_url`, as `posting_page`.
  - LinkedIn is never fetched: the details call gets `--disallowedTools WebFetch(domain:…)` for each `blocked_fetch_domains` entry, and `posting.check_url` refuses those domains. Descriptions from a Claude lookup are WebFetch summaries; those from the board check or `set-posting` are verbatim.
- **Retrust** (`.\run.ps1 retrust [--dry-run]`, `agents/scout/retrust.py`): re-derives `source_trust` from both links for every job that isn't closed and has a found posting, e.g. after editing `config/source_trust.yaml`, and prints what changed. An official job gets the official link as `apply_url`; a newly official one caches its board. Postings the user set (`posting_set`) stay official. Each change logs a `trust_changed` event (actor `scout`); a shortlisted or researched job that drops to aggregator is moved back by the next shortlist.
- **Set posting** (`.\run.ps1 set-posting <job_id> <url>`, `agents/scout/posting.py`): you give a job's official posting URL.
  - It fetches the page once (or the ATS's JSON for a JavaScript-rendered board, which also sets location, `posted_at` and remote) and stores the verbatim description, `apply_url = source_url = url`, `source_trust = official`, `needs_details = 0`, and a salary if the JSON-LD states one.
  - It logs a `posting_set` event (actor `user`) and caches the company's board.
  - It reruns pass 2 for that job only (any status, any description length), then checks the job's age with the posting's date (`age.recheck`; a posting too old closes the job and skips the brief), and rewrites its brief if it has one, reusing the cached company research.
  - Web searches run on Haiku, so each call logs a `usage` line per model, including `web_searches`.
- **Pass 2** (`.\run.ps1 pass2`, `agents/scout/pass2.py`, prompt `prompts/score_pass2.md`): rescores jobs in the same top-15 pool that have a full description and no pass 2 score yet.
  - Same schema as pass 1, plus `posting_type` (`job` / `talent_pool` / `general_application`: talent networks, talent pools, general applications, "future opportunities" and postings that promise no interview are `talent_pool`), `years_required`, `office_days` (0-5 days a week in the office, null if not stated; feeds the location score), `stack_match`, `red_flags`, `possibly_closed`, `why` and `gaps`. The final score comes from the same `compute_final()`, and the extras go into `score_detail` (`"pass": 2`). Status stays `scored`.
  - `score_pass2.md` repeats pass 1's definitions. Keep the two in step.
- **Shortlist** (`.\run.ps1 shortlist`, `agents/scout/shortlist.py`): the top 3 verified `scored` jobs with a final score of at least `shortlist_min_score` (`profile.yaml`, 65) that were never shortlisted. Verified means a pass 2 score and `source_trust` `official`; aggregator-only jobs are never shortlisted. Jobs on hold (`db.is_held`) and jobs whose posting is over its age limit are never shortlisted either. Fewer than 3 is fine. Jobs the user shortlists in the review app (Shortlist now) count toward the same 3 a day.
  - **Recruiter pools are never shortlisted** (`shortlist.is_recruiter_pool`): a pass 2 `posting_type` other than `job`, or a title saying talent network, talent pool, general application or future opportunities. They're not "worth checking" either; the digest lists them on their own.
  - **Possibly closed:** pass 2's `possibly_closed`, or a red flag saying the posting is closed, expired, filled or no longer accepting applications (`shortlist.possibly_closed()`).
    - Such a job is never shortlisted.
    - A `shortlisted` or `researched` one is first moved back to `scored` (actor `scout`, detail `possibly closed`). Its score and brief are kept.
    - The same happens to a `shortlisted` or `researched` job whose `source_trust` is `aggregator`, with detail `aggregator only`.
    - It no longer counts toward today's 3, so the next-best verified job takes its place and is researched in the same daily run.
    - **Re-check rule:** a job board marking a posting closed only means "check it". A moved-back job (either detail) that later passes may be shortlisted again, still within the 3 per day. Passing means an official posting (set-posting, the free check) and, after pass 2 reran on it, not possibly closed. Shortlisted again, a job whose brief already exists goes straight to `researched` with no calls.
  - Unverified, aggregator-only and possibly closed jobs that would have made it go to the digest's "Worth checking yourself" section. A job moved back today is always listed there.
- **Claude calls** (`agents/common/claude.py`):
  - Always `--model sonnet`. Every call is stripped to our prompt plus data (`CLAUDE_FLAGS`): `--tools ""`, `--system-prompt-file` (replaces Claude Code's prompt), `--strict-mcp-config` with no servers, `--setting-sources ""`, `--disable-slash-commands`, `--no-session-persistence`. That cut overhead from ~30k input tokens to ~600 per call. `--bare` is not an option: it accepts only an API key, not the Pro login. Don't use `--json-schema`: it adds a tool and a second turn. The reply is plain JSON and is validated in Python.
  - Each batch's usage (model, input, cache creation, cache read and output tokens, cost) goes to `logs/score-YYYY-MM-DD.log` as a `usage batch=…` line.
  - Jobs missing from a reply stay `new`. A usage-limit error stops the run; other failures retry with backoff.
- **Dedupe:**
  - Source + external job ID (`{source}:{slug}:{native_id}`; for email jobs `email:<type>:<job id from the link>`) prevents duplicates on re-runs.
  - `dedupe_key` (company|title|location) skips a new posting that matches a job that isn't closed.
- **Closing removed postings:**
  - Any fetched job already in the database is marked seen, even if it now fails a filter.
  - Only feed jobs unseen for 3 days are closed as "posting removed", and only for feeds that fetched successfully in this run. `applied` and `interviewing` jobs are never touched.
  - Email jobs are never closed for no longer appearing in alerts.
- **Refilter** (`.\run.ps1 refilter [--dry-run]`, `agents/scout/refilter.py`): re-runs the hard filters on stored jobs, e.g. after editing `profile.yaml`.
  - Only `new` and `scored` jobs are checked. Failures are closed by `scout` with detail `filtered: <reason>`.
  - Jobs whose latest close detail starts with `filtered:` and that now pass go back to `new`, unless another open job has the same `dedupe_key`.
  - Never touches `shortlisted` or later, or jobs closed for any other reason. `--dry-run` prints what would close or reopen and writes nothing (not even pending migrations).
  - Also recomputes every stored `dedupe_key`, so a normalization change reaches jobs stored before it.

### Digest
- `agents/digest.py` writes `digest/YYYY-MM-DD.md`. Under the title is a **Last run** line (`agents/common/lastrun.py`, from the `daily_runs` table): today's latest daily run with its start time (local), finish time and each step's status, `not finished` for a run still going or killed, or `No run today yet`. Then these sections, in order:
  1. Warnings: active notices (`db.get_active_notices()`, e.g. an expired Gmail login with the fix command), failed steps and source errors.
  2. Application updates (`agents/tracker/report.py`):
     - Offers first, flagged.
     - New today: emails processed today, with any status change.
     - Needs review, with a Gmail link.
     - Did you apply?
     - Recruiter outreach.
     - Consider a follow-up, and Check in.
     - Lists cover the last 14 days. With no application yet it says so.
  3. Today's top 3: jobs first shortlisted today and not moved back, each with score, location, the posting date's label (`age.label`), salary, why, gaps, the direct apply link, `source_trust`, a "Read brief" link and sub-scores.
  4. Worth checking yourself: unverified, aggregator-only and possibly closed jobs that would have made the top 3, plus any job moved back today. Each has its alert link, and the posting link for aggregator-only and possibly closed ones. Aggregator-only jobs are labeled "found only on aggregator — run set-posting if you find the official page". Each shows its date label. Jobs over their age limit aren't listed. They're left out of the runners-up.
  5. Recruiter pools (optional, low effort): `new` and `scored` recruiter pools, one line each (company, title, link), only when there are any. Left out of the runners-up.
  6. Still waiting: earlier shortlisted jobs still `shortlisted` or `researched` and not on hold, with days waiting and the date label. Below them, held jobs over their age limit ("older posting, check it's still open"; `age.held_too_old`).
  7. Cleaned up today: jobs the daily run closed today as stale (`waiting.cleaned_up`) or with a posting too old (`age.closed_on`), with the `reopen` command.
  8. Runners-up 4–10.
  9. Run stats: emails read, jobs found, application emails classified, filtered, scored, and Claude usage per model, with web searches.
  10. A command cheat sheet.
- Run stats only exist when `.\run.ps1 daily` builds the digest.

### Daily run
- `.\run.ps1 daily` (`agents/daily.py`) runs fetch → track → score → details → pass 2 → cleanup → shortlist → research → digest, then rewrites the review page.
- **Cleanup** has two parts:
  - Stale waiting jobs (`agents/scout/waiting.py`; `python -m agents.scout.waiting --dry-run` previews it): closes `shortlisted`/`researched` jobs shortlisted at least `waiting_expiry_days` ago (`profile.yaml`, default 10) with no user action since (no event with actor `user` after the latest shortlisting: a rating, hold, unhold, posting set, skip or apply), actor `scout`, detail `stale: no action 10 days`. Held jobs are never touched; applied and interviewing jobs aren't in scope.
  - Postings too old (`age.close_too_old`, see Posting age): detail `posting too old: <N> days`. A failed step is logged and shown as a warning in the digest, and the run continues. The digest is always written.
- Logs go to `logs/daily-*.log`. Fetch also logs to `scout-*.log`, research to `research-*.log`, and the other Claude steps to `score-*.log`.
- **Exit code:** 0 only if every step passed (fetch through research, the digest and the review page all `ok`); 1 if any step failed or finished with problems, or the run didn't start. `run.ps1` passes it through.
- **Unattended runs (Task Scheduler):**
  - **Lock:** `data/daily.lock` (`agents/common/runlock.py`, gitignored) is created atomically with the user's pid and start time. A second run while it's held prints `Not started: …` and exits 1. The run touches the lock before each step; a lock untouched for 2 hours is stale (a crashed or killed run) and is taken over. The lock is removed when the run ends.
  - **claude.exe:** found only through `claude_path` in `config/local.yaml` (see Environment). If that's missing or wrong, the Claude steps (track, score, details, pass 2, research) are marked `failed` with one warning naming the fix (`scripts\setup.ps1`), and fetch, shortlist, digest and review still run.
  - **Run history:** each run gets a `daily_runs` row (migration 13): `started_at`, then the steps' statuses as they finish, and `finished_at` once the last step is done (just before the digest and review page are written, so their Last run line shows it). Digest and review statuses are added afterwards.
  - **Toast:** the track step may show a reply toast (see Tracker). When the run ends, a Windows notification (`agents/common/toast.py`: Windows PowerShell's WinRT toast API, no dependencies) says `Job digest ready: N new picks` (N = jobs this run shortlisted) if every step passed, else `Daily run had problems — see digest`. Any failure to show it is logged and ignored; `--no-toast` turns it off. A task set to run whether the user is logged on or not has no desktop, so no toast appears.
  - **Log retention:** at the start of each run, `logs/*.log` older than 30 days are deleted (`runlog.prune_logs`; by the date in the file name, else the file's modified time).
  - **Scheduling:** a task whose action is `powershell.exe` with arguments `-NoProfile -ExecutionPolicy Bypass -File "<project folder>\run.ps1" daily` and start-in set to the project folder. Run `scripts\setup.ps1` first so `config/local.yaml` exists.

### Feedback
- `.\run.ps1 rate <id> up|down ["note"]` sets `user_rating` and appends `{title, company, note}` to `feedback.liked_examples` / `disliked_examples` in `profile.yaml`. It keeps the last 20 of each, and rating the same job again moves it to the other list. Each rating logs a `rated` event (detail `up`/`down`) for stats.
  - Only the `feedback:` block is rewritten: comments elsewhere and CRLF line endings are kept, and the file isn't saved if anything else would change.
- `.\run.ps1 skip <id> "reason"` closes the job with actor `user` and detail `skipped: <reason>`.
- `.\run.ps1 reopen <id> "reason"` takes a close back (a skip, a stale close, …): `closed → scored`, actor `user` only, detail `reopened: <reason>`. The score stays.
- `.\run.ps1 hold <id> "reason" [--until YYYY-MM-DD]` puts a job on hold (`feedback.hold` → `db.set_hold`; migration 17 added `jobs.held_until` and `jobs.hold_reason`).
  - A hold is not a status: the job keeps its status. Only `new`, `scored`, `shortlisted` and `researched` jobs can be held (`db.HOLDABLE`).
  - While held (`db.is_held`: a reason is set and `held_until`, if any, is after today), the job is left out of the shortlist, "Worth checking yourself", the digest's Still waiting and the batch research run.
  - It comes back by itself on `held_until`; without a date it stays held until `.\run.ps1 unhold <id>`. Both log an event (`held` with the date and reason, `unheld`; actor `user`).

### Stats
`.\run.ps1 stats [--days 7] [--json]` (`agents/stats.py`): what the agent saw and did over the last N days, the input for tuning. `--json` prints the same data for a script or a Claude session to read.

- **Alert emails per sender:** read, jobs found, skipped (not job lists), unparsed, and how many of those are still unparsed. From `processed_emails`, which keeps the sender address since migration 16 (older rows show the parser type), and `unparsed_emails`, where fetch records each email no parser could read; it still stays out of `processed_emails`, so it's retried.
- **Jobs:**
  - Found, by source (`first_seen_at`).
  - Filtered out at fetch, by reason with the most common detailed reasons. From `filtered_postings`: one row per new posting the hard filters rejected, recorded by fetch since migration 16. A feed job seen daily counts once.
  - Closed by refilter, and scored (`new → scored` events).
- **Scores:** average, how many were rescored by pass 2, and the top 5 of the jobs scored in the window.
- **Pipeline:** jobs shortlisted, applied (`applications`) and skipped, with the skip reasons.
- **Your ratings:** up and down given in the window (`rate` now logs a `rated` event), and for every rated job the average sub-scores and final score of liked vs disliked jobs, with the gap.
- **Claude usage per day** and model: calls, tokens, web searches and cost. Read from the `usage` lines `agents.common.claude` writes to `logs/*.log`, so only as far back as the 30 days of logs. A line copied into several logs (`daily-*.log` and a step's log) counts once.

### Publisher
LinkedIn post drafts for the user to review in Typefully (`agents/publisher/`). Run by hand, weekly; not part of `daily`.

- **Sources** (`sources.py`, the only input): `profile/resume.md` from `### Summary` on (the contact header is dropped, and any email or phone number removed), `job-agent-public`'s `README.md` and `git log` (date and subject only, 50 commits), and `profile/post_notes.md` if it exists. The public repo is `../job-agent-public` unless `config/local.yaml` sets `public_repo`. `read_public()` refuses any path under `data/`, `briefs/`, `digest/`, `review/`, `archive/`, `secrets/` or `logs/`, and the module never imports `agents.db` (a test checks its imports).
- **`.\run.ps1 post-ideas [--dry-run]`** (`ideas.py`, prompt `prompts/post_ideas.md`): one stripped `claude -p` call (Sonnet, no tools) proposes exactly 5 ideas, a title and one line each, written to `drafts/post-ideas-YYYY-MM-DD.md` as `n. **Title** — line`. Ideas get the guard's content checks, with one retry. `--dry-run` prints the sources' sizes and calls nothing.
- **`.\run.ps1 draft-post <n> [<n> ...] [--ideas FILE] [--dry-run]`** (`draft.py`, prompt `prompts/post_draft.md`): writes ideas from the newest ideas file as posts, at most 3 per run, one call each. Each post must pass the guard; one that fails is rewritten once with the problems listed, and if it fails again it's kept in `drafts/` only and never sent (exit 1). A passing post is saved to `drafts/post-YYYY-MM-DD-<n>.md` before upload, sent to Typefully, and printed with its word count. `--dry-run` writes and checks but never calls Typefully.
- **Guard** (`guard.py`, lists in `config/publisher.yaml`), checked in code on every post:
  - 120-220 words, at most 2 hashtags, at most 2 emoji, no hype words.
  - No company the user prefers or is applying to: names and aliases in `preferred_companies.yaml`, and companies of jobs `shortlisted`, `researched`, `applied` or `interviewing`, with a brief, or with an `applications` row. The database is opened read-only (`mode=ro`) for these names only; nothing from it reaches a prompt. Matched as whole words ignoring case, plus the name's core ("Globex Global Tech" → "Globex"). Names that are also common words (`common_word_companies`, set per user; the committed list is fictional: Beacon, Summit, Harbor, Anchor) match only as written, so "beacon" passes and "Beacon" doesn't.
  - No application or recruiter phrases (`application_patterns`), no security setup (`security_terms`; always IP addresses, "port <n>", internal hostnames such as `.local`/`.lan`/`.internal`, `cfargotunnel.com` hostnames and tunnel IDs (UUIDs)), no private terms (`publisher.blocked_terms` in `profile.yaml`, for supplier or customer names), no email or phone number, no link that isn't in the sources.
  - Every number must appear in the sources, so no internal figure can get in.
  - **Allowlist** (`allowlist`, set per user from their resume; the committed examples are fictional: Northwind, Acme Tunnel): a company or security-term match lying entirely inside an allowlisted phrase is exempt. Nothing else is: "Acme Tunnel ID" still trips `tunnel ID`, and the IP, port, hostname and tunnel-ID patterns always apply.
- **Typefully API v2** (`typefully.py`): base `https://api.typefully.com`, `Authorization: Bearer <TYPEFULLY_API_KEY from .env>`.
  - The social set is the one with LinkedIn connected (`GET /v2/social-sets`, then `GET /v2/social-sets/{id}/` for `platforms.linkedin`), cached as `typefully_social_set_id` in `config/local.yaml` (appended, other lines kept; `setup.ps1` keeps it too).
  - A draft is `POST /v2/social-sets/{id}/drafts` with `{"platforms": {"linkedin": {"enabled": true, "posts": [{"text": ...}]}}}`; the 201 reply's `id`, `status` and `private_url` are read, and a status other than `draft` raises. Shapes checked against the live API on 2026-10-18.
  - `_request()` refuses `publish_at` or `plan_at` anywhere in a query or body, any method but GET and POST, and any POST but draft creation, before anything is sent. A POST is never resent after a network error; a 429 is retried with backoff.
- **Limits:** the free plan publishes 10 posts a month (`publishing_quota` on the social set: `used`, `remaining`, `resets_at`; drafts don't use it). Before any Claude call, `draft-post` stops if `remaining` is 0, or if this month's drafts in `drafts/typefully-drafts.jsonl` plus this run's would pass 10, and logs both counts as a `Monthly count:` line.
- Logs and usage lines (`usage publisher step=ideas|draft`) go to `logs/publisher-YYYY-MM-DD.log`.
- **Tests:** `tests/test_publisher.py` uses a fake Typefully session with the live shapes; `test_no_request_ever_includes_publish_at` fails if any request in a full run carries `publish_at` or `plan_at`.

### Research analyst
`.\run.ps1 research [job_id] [--refresh-company] [--dry-run]` (`agents/research/brief.py`). Without an id it takes every `shortlisted` job with no `brief_path` that isn't on hold. With an id it takes that job if it's scored, shortlisted, researched, applied or interviewing, and rewrites the brief; a scored job keeps its status.

- **Company step** (`prompts/research_company.md`): reuses the newest `company_research` row for `normalize_company(company)` if it's under 30 days old. Otherwise it makes one call with only WebSearch and WebFetch (LinkedIn denied, as in details) and `--max-turns 20`. The prompt sets an 8-search budget; the CLI has no hard cap, so going over is logged as a warning. The reply is JSON `{markdown, sources}`, cached in `company_research` (older rows are kept).
- **Role step** (`prompts/research_role.md`): one call with no tools. It gets:
  - the company research and its sources;
  - the job description and apply link;
  - `score_detail`: sub-scores, why, gaps, red flags, stack match;
  - `identity`, `skills`, `experience` and `targets` from `profile.yaml`.

  It writes the whole brief.
- **Brief template:** the `##` headings between `<!-- template -->` and `<!-- /template -->` in `research_role.md`. The validator reads the headings from there, so the template is edited only in that file.
- **Rules in both prompts:**
  - Label each claim (stated) or (inferred).
  - Cite [n] for every company fact.
  - Prefer official sources, then reputable news.
  - News must be within the last 12 months, with dates.
  - Never name employees or suggest contacting anyone.
  - Say "unknown" rather than guess.
- **Validation** (`validate_brief`): every template heading, a markdown table under the skills heading, at least 3 source URLs under the sources heading, and the job's apply link. A failed check, or an unusable company reply, retries once with the problems listed.
- Saves `briefs/YYYY-MM-DD-<company>-<role>.md`, stores the path in `jobs.brief_path`, and moves a `shortlisted` job to `researched` (actor `research`). A failure leaves the job `shortlisted` for the next run.
- Usage goes to `logs/research-YYYY-MM-DD.log`: one `usage research job=… step=company|role` line per call and model, plus a `usage brief job=…` total per brief.

### Review page
- **The app** (`.\run.ps1 review`, `agents/review_app.py`): a standard-library `ThreadingHTTPServer`, always bound to `127.0.0.1` (the host is fixed in code; `make_server` takes no host), on a free port unless `--port`. It opens the browser (`--no-browser` skips that) and runs until Ctrl+C. Logs go to `logs/review-*.log`.
  - **Tabs**, each with a count (`review_app.tabs`; a job is on one tab at most):
    - **Today:** `shortlisted`/`researched`, shortlisted today.
    - **Near misses:** `scored`, not held, score 55 or more: `official` or `not_found` below `shortlist_min_score`; `aggregator` at any score, with a "Needs official posting" badge; and `official` jobs at the bar that Shortlist now can take (`shortlist.ready`). Best score first.
    - **On hold:** every held job, whatever its status, and only there.
    - **Waiting:** `shortlisted`/`researched`, shortlisted before today.
    - **Applied:** `applied`/`interviewing`, newest application first, with the tracker timeline.
    - A **Rated** filter shows all, unrated or rated jobs.
  - **Cards:** score, company, title, location, the posting date's label (`age.label`; a held job over its age limit adds "older posting, check it's still open"), salary, why, gaps, red flags, `source_trust`, the apply link, sub-score bars and the brief's date (from the file's modified time, "Brief: Oct 18"; the time is in the tooltip, so a rewrite is visible). One **Read** button opens the side panel.
  - **Read panel** (`review_app.read_panel`, `GET /api/jobs/<id>/read`):
    - With a brief: two tabs, **Brief** (the default; Rewrite brief, which first asks in an in-page dialog: "Write a new brief? Takes about a minute and uses some Claude allowance.") and **Posting**.
    - With a description but no brief: opens on Posting, with **Write brief** at the top (no confirmation). It switches to the Brief tab when the brief is done.
    - With neither: "No posting found yet", the alert link, and **Find posting**.
    - The Posting tab shows the stored description (`posting_html`: escaped, paragraphs kept, only http(s) URLs linked), the apply and source links, `source_trust`, the date label and salary, and for any job that isn't `applied`, `interviewing` or `closed` a **Paste official posting URL** box.
  - **Buttons** call the CLI's own functions, so `db.set_status()` still enforces every transition:
    - 👍/👎 with an optional note (`feedback.rate`).
    - Hold (optional date, reason required) and Unhold (`feedback.hold` / `unhold`).
    - Skip with a reason (`feedback.skip`).
    - **Background tasks**, one per job at a time (`App._start`), each with a spinner and message on the card and in the panel while it runs, then its outcome; a failure shows its error text (kept until the next task or a restart). The page polls `GET /api/jobs/<id>/task`.
      - Write / Rewrite brief: `brief.research` for that job ("Writing brief… (about 30–60 s)", then "Brief updated ✓" and the new brief opens in the panel). Scored jobs may get a brief too; they stay `scored`.
      - Find posting: `details.fetch_details(job_ids=[id])` for that job, whatever its pool rank or earlier attempts (the free board check, then the web search), then pass 2 for that job if a posting was found, then the age check with the new date (a posting too old closes the job, and the outcome says so).
      - Paste official posting URL: `posting.set_posting`, as `set-posting` does (fetch, store the verbatim description as `official`, cache the board, rerun pass 2, rewrite an existing brief). A blocked domain (LinkedIn) or a non-http(s) link is refused before anything runs. The outcome names the score change and the date label, or that the posting was too old and the job closed.
    - **Shortlist now**, on a card or in the panel when `shortlist.ready` (scored, `official`, score at least `shortlist_min_score`, not held): `shortlist.shortlist_now` moves it `scored → shortlisted` (actor `user`), then writes its brief in the background (or, if it already has one, moves it to `researched` at once). It's refused, with the reason shown, for no pass 2 score, a possibly closed posting, a recruiter pool, a posting over its age limit ("the posting is 50 days old (the limit is 30 days)"), or when 3 jobs were already shortlisted today (`shortlist.shortlisted_today`, the same count the daily shortlist uses).
    - ✅ I applied, on every open job (`new`, `scored`, `shortlisted`, `researched`; `db.USER_APPLY_FROM`): method (`ats`/`referral`/`email`) and notes, `db.mark_applied` with actor `user`, which also ends a hold. It's the user's own click; nothing in the app sets `applied` on its own.
  - Every action answers with the job's new card, its tab and all counts; the page swaps that card in place and shows a short confirmation. A card's buttons are disabled while its request runs, and the server allows one action per job at a time (a second gets 409).
  - **Safety:** only requests whose `Host` is `127.0.0.1:<port>` or `localhost:<port>` are answered (no DNS rebinding). Every `/api` request needs the random per-run token embedded in the page (`X-Review-Token` header) and a POST needs a JSON body, so another website can't trigger actions. Responses carry a strict Content-Security-Policy.
  - **Endpoints:** `GET /`; `GET /favicon.ico` and `/favicon.svg` (an inline SVG, no token); `GET /api/jobs/<id>/card|read|task`; `POST /api/jobs/<id>/rate|hold|unhold|skip|applied|shortlist|research|find|posting`. `tests/test_review_app.py` runs the real server on a free port and covers each one, with fake research, find and posting functions.
- **The static export** (`.\run.ps1 review --static` or `python -m agents.review`; `daily` writes it too): `agents/review.py` writes `review/index.html`, a static, read-only page for reading on a phone. The same Last run line as the digest's is at the top (highlighted when a step didn't pass). Then an **Applications** view: every job applied to (an `applications` row, or `applied`/`interviewing`), newest first, with its status, the date applied, a follow-up or check-in note when it's gone quiet, and a timeline, oldest first, of its status changes from `applied` on and its tracker emails (category, sender domain, summary, next action, deadline; needs review and offers highlighted). Then "Jobs in play": it lists every `shortlisted`, `researched`, `applied` and `interviewing` job, best first, with sub-scores, why, gaps, the labeled apply link, and its brief rendered inline.
- Briefs are rendered by `agents/common/mdhtml.py`, which escapes all HTML and only turns http(s) URLs into links.

### Tracker
`.\run.ps1 track [--since-days N] [--dry-run]` (`agents/tracker/track.py`), and the `track` step of `daily`, right after fetch. Gmail stays `gmail.readonly`: it never sends, labels, archives or deletes mail, and never sets `applied`.

- **Window:** the last 30 days on the first run, and again whenever the set of `applied`/`interviewing` jobs (its signature, e.g. `14,36`, kept in `tracker_runs`) changed since the last run, so older unmatched mail is checked again. Otherwise since the last run, minus a day of overlap. `--since-days N` overrides it. Every query has `-label:JobAlerts -from:me after:<epoch>`.
- **Queries** (`agents/tracker/match.py`):
  - One per `applied` or `interviewing` job: `from:(<company domains>) OR (from:(<ATS senders>) "<company>")`. Company domains are the hosts of `apply_url`, `source_url` and the cached board that `source_trust` counts as the company's own (not an ATS, public-sector board or aggregator), reduced to the registrable domain, plus `company_domains`.
  - All mail from ATS senders (for "Did you apply?"), and recruiter-sounding mail in the primary inbox (`category:primary`, recruiter terms and role terms).
  - **ATS sender domains** are `ats_sender_domains` in `config/source_trust.yaml`; a domain covers its subdomains. Real ATS mail comes from subdomains such as `us.greenhouse-mail.io` and `candidates.workablemail.com`.
- **Headers first:** each message not yet checked against the current jobs signature (`tracker_header_checks`) is fetched as metadata only (From, Subject, Date) and scored against every job (`score_header`): company's domain 50, ATS sender 20, company in subject 30 (or in the sender name 20), title in subject 20, and 10 when that job's query (which names the company) found it.
  - **Likely** (`is_likely`): 50 or more *and* the sender is the company's domain or an ATS. Job alerts missing the JobAlerts label name the company and title in the subject, so the subject alone never counts.
  - A likely match, any other ATS email, and a recruiter-looking email (a person, not a no-reply/notification sender or a job board, with recruiter words in the subject) have their body fetched. Once fetched, a job match stands only if the subject, sender name or body names the company.
  - Security and verification codes are recorded as `ignored`. Everything else is **unmatched**: only its header check is stored, never a `tracker_messages` row. When the jobs signature changes, it's checked again, headers only and with no Claude call, so a confirmation that arrived before you marked the job applied matches on the next run.
  - A likely match whose (message, job) pair was already processed isn't fetched again.
- **Classify** (`agents/tracker/classify.py`, prompt `prompts/track_classify.md`): one `claude -p` call per batch of 5 (Sonnet, the stripped flags, no tools). The body goes in as plain text, up to 3,000 characters.
  - The reply gives a category (`confirmation`, `assessment`, `interview_invite`, `scheduling`, `rejection`, `offer`, `recruiter_outreach`, `other`), confidence 0-100, `about_job`, company, role, a one-line summary, a deadline (YYYY-MM-DD) and the next action. It's validated in Python.
  - A message's header check is stored only once it's resolved (classified, ignored or unmatched). A message missing from the reply has none, so the next run retries it.
- **Stored** in `tracker_messages` (migrations 14 and 15), one row per (message, job): message ID, job, date, sender domain, match score, category, confidence, summary, deadline, next action, company, role, outcome. Never the subject or body. A processed pair is never classified again; the same message may be classified again for another job.
- **Outcomes** (`track.decide`):
  - **needs_review:** confidence under 70, or `about_job` false. No status change.
  - **offer:** no automatic change; flagged first in the digest.
  - **status_change** on an `applied` or `interviewing` job: `interview_invite`, `assessment` or `scheduling` moves `applied → interviewing` (detail `<category>: <summary>`); a rejection closes it with detail `rejected`.
  - **did_you_apply:** an application email (confirmation, assessment, invite, scheduling, rejection) for a job in the database that isn't `applied` or `interviewing`. It's listed until the job is marked applied. Such an item also lets the user run `mark-applied` on a **closed** job (`closed → applied`, below); the hint gives the `mark-applied` command (any open status, or closed).
  - **outreach:** `recruiter_outreach` not about a job you applied to.
  - **event:** everything else (a confirmation for an applied job, `other`, mail with no job).
- **The state machine** limits the tracker to `db.TRACKER_MOVES`: `applied → interviewing` (any detail), and `applied`/`interviewing → closed` only with detail `rejected`. Any other change by actor `tracker` raises `ActorNotAllowed`.
- **Staleness** (`agents/tracker/report.py`): an `applied` job with no reply (anything but a confirmation or `other`) for 14+ days since applying or the last reply → "Consider a follow-up"; an `interviewing` job with no email and no move into interviewing for 7+ days → "Check in".
- **Reply toast:** when a run classifies a new `interview_invite`, `assessment`, `scheduling`, `rejection` or `offer`, or something that needs review, a Windows toast names it, e.g. `Contoso: interview invite — check digest`. Two are named and the rest counted (`track.reply_toast`). With nothing new, there's no toast. `track` and `daily` show it; `--no-toast` turns it off.
- **Output:** the digest's Application updates section and the review page's Applications view (below). Usage goes to `logs/tracker-YYYY-MM-DD.log`.
- **Tests:** `tests/test_tracker.py` uses synthetic application emails modeled on real ATS layouts, `email_tracker_contoso_1/2.json` (a Greenhouse confirmation and security code) and `email_tracker_fabrikam_1.json` (a Workable confirmation), with fictional companies and roles. `tests/conftest.py` makes `gmail.get_service` fail in every test except `test_gmail.py`, so no test can open the real mailbox.

## Job lifecycle

```
new → scored → shortlisted → researched → applied → interviewing → closed
```

| Transition | Who |
|---|---|
| `new → scored` | Scout |
| `scored → new` | Scout only, for a rescore (`.\run.ps1 rescore`); clears the score; logged |
| `scored → shortlisted` | Scout's shortlist step after pass 2 (top 3, score ≥ `shortlist_min_score`, never shortlisted before) or the user (Shortlist now in the review app, within the same 3 a day) |
| `shortlisted`/`researched` → `scored` | Scout only, and only with detail `possibly closed` (the posting looks closed) or `aggregator only` (no official posting); the score stays; logged |
| `shortlisted → researched` | Research analyst (`research`), once a brief passes validation |
| `new`/`scored`/`shortlisted`/`researched` → `applied` | **The user only** (`mark-applied`, or ✅ I applied in the review app); agents never |
| `applied → interviewing` | Tracker (an interview invite, assessment or scheduling email) or the user |
| `→ closed` | The user (rejection, withdrawn, filled, `skipped: <reason>`); Tracker only from `applied` or `interviewing`, only with detail `rejected`; Scout (posting removed, posting closed, `filtered: <reason>`, `stale: no action <N> days`, `posting too old: <N> days`) |
| `closed → new` | Scout only, and only when the job's latest close was `filtered: …` (refilter); logged |
| `closed → scored` | The user only (`.\run.ps1 reopen`, detail `reopened: <reason>`); the score stays; logged |
| `closed → applied` | The user only (`mark-applied`), and only for a job with a tracker "Did you apply?" item; logged |

Enforce allowed transitions in one place in code, not separately in each agent.

## Running

`run.ps1` in the project root is the entry point. It always uses `.venv\Scripts\python.exe`, runs from the project root, and passes extra arguments through.

- Commands live in one ordered dispatch table (`$commands`: usage, help text, Python arguments).
- `.\run.ps1 help`, `-h`, `--help`, or no arguments prints the list generated from that table. Add a command there and help picks it up.

```powershell
.\run.ps1 daily                  # the whole morning: fetch, score, details, pass2, shortlist, research, digest
.\run.ps1 fetch                  # Scout; e.g. .\run.ps1 fetch --since-days 3
.\run.ps1 show new               # list jobs by status; e.g. .\run.ps1 show new --limit 20
.\run.ps1 refilter               # re-apply hard filters to stored jobs
.\run.ps1 posting-age --dry-run  # which open jobs have a posting too old (10 days; official+open 30, preferred 45)
.\run.ps1 backfill-dates --dry-run  # one-time: real posting dates for open official jobs (no Claude)
.\run.ps1 score                  # score new jobs with Claude; e.g. .\run.ps1 score --limit 30
.\run.ps1 rescore                # reset scored jobs to new and score again, after prompt/weight changes
.\run.ps1 top                    # 15 highest-scoring jobs that aren't closed; e.g. .\run.ps1 top 30
.\run.ps1 details                # find official postings for the top 15 (web search)
.\run.ps1 pass2                  # rescore the top 15 from full postings
.\run.ps1 shortlist              # shortlist up to 3
.\run.ps1 help                   # every command, generated from run.ps1's dispatch table
.\run.ps1 ats-check 49 42        # free Greenhouse/Lever/Ashby API check for given jobs
.\run.ps1 retrust --dry-run      # re-check official/aggregator for open jobs after editing source_trust.yaml
.\run.ps1 set-posting 33 <url>    # you found the official posting: store it, rescore, rewrite the brief
.\run.ps1 research               # briefs for shortlisted jobs; e.g. .\run.ps1 research 40
.\run.ps1 review                 # the review app on 127.0.0.1: tabs, rate, hold, skip, research, I applied
.\run.ps1 review --static        # rebuild the read-only review/index.html
.\run.ps1 hold 28 "reason" --until 2026-11-01   # out of the shortlist and Waiting until then
.\run.ps1 unhold 28              # take it off hold
.\run.ps1 digest                 # write digest/YYYY-MM-DD.md
.\run.ps1 rate 28 up "note"      # thumbs up/down, feeds the scorer
.\run.ps1 skip 28 "reason"       # close a job you won't pursue
.\run.ps1 reopen 28 "reason"     # take back a close (skip, stale, too old): closed -> scored
.\run.ps1 track --since-days 7   # read application emails, update applied jobs (--dry-run: headers only)
.\run.ps1 stats --days 7         # weekly stats: emails, filters, scores, pipeline, ratings, Claude usage
.\run.ps1 gmail-check            # 10 newest JobAlerts headers
.\run.ps1 post-ideas             # 5 LinkedIn post ideas from public sources -> drafts/post-ideas-YYYY-MM-DD.md
.\run.ps1 draft-post 2           # write idea 2 (up to 3 per run), save it to Typefully as a draft; never publishes
```

Importing `agents` under any other interpreter exits with "Wrong Python — use .\run.ps1 or .venv\Scripts\python.exe" (check in `agents/__init__.py`). Commands without a `run.ps1` verb (`gmail auth`, `db mark-applied`, `tools.sanitize_email`) run as `.venv\Scripts\python.exe -m ...` from the project root.

## Environment (Windows / PowerShell)

```powershell
# New machine, or after pulling changes: safe to rerun
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1      # -RecreateVenv, -PythonVersion 3.14, -SkipTests

# Move to another machine: writes job-agent-bundle-YYYYMMDD.zip to the Desktop
powershell -ExecutionPolicy Bypass -File scripts\export_bundle.ps1
```

- **`scripts/setup.ps1`** does the following:
  - Checks for Python 3.12 (`py -3.12`, else `python` on PATH) and Git.
  - Creates `.venv`. An existing one with the same Python version is kept; another version stops setup unless `-RecreateVenv`.
  - Installs `requirements.txt`.
  - Writes `claude_path` to `config/local.yaml`.
  - Creates `data/`, `logs/` and `secrets/`, and copies `profile/*.example.yaml` to `profile.yaml` and `preferred_companies.yaml` when those are missing (never overwrites).
  - Runs the migrations (`agents.db init`; an existing `jobs.db` keeps its data) and pytest.
  - Prints the remaining steps (Gmail auth, `gmail-check`, `daily`).
- **`scripts/export_bundle.ps1`** zips the project:
  - It includes `.git`, `data/jobs.db` (a SQLite backup-API snapshot) and `secrets/credentials.json`.
  - It leaves out `.venv`, `logs`, `.pytest_cache`, `__pycache__`, `data/backups`, `secrets/token.json`, `config/local.yaml` and `.env*`.
  - The zip is private: it holds the profile and the OAuth client secret.
- **`config/local.yaml`** (gitignored) holds machine-specific settings. `claude.claude_exe()` uses its `claude_path` for every `claude -p` call and never falls back to PATH (Task Scheduler runs with a different PATH): a missing file, no `claude_path`, a relative path or a path that doesn't exist raises `ClaudeNotConfigured` with the fix. Like a usage limit (both `ClaudeUnavailable`), it stops a step at once, with no retries. No code, config or docs may contain a user's home path: use paths relative to the project root, or `$HOME`.
- Use `pathlib` for paths. Don't hardcode `/` or `\` separators.
- Open text files with `encoding="utf-8"` explicitly, because the Windows default codepage isn't UTF-8.
- PowerShell 5.1 has no `&&`. Chain commands with `; if ($?) { ... }`.

## Conventions

- Use the standard-library `sqlite3` module. Keep schema changes in versioned migrations.
- **Shared database:** the review app and the scheduled daily run can use `data/jobs.db` at the same time. `db.connect()` sets WAL mode (readers never wait for a writer) and a 30-second busy timeout (`BUSY_TIMEOUT_S`: a writer waits for the other instead of failing with "database is locked"). Always open connections through `db.connect()`, keep each write in a short `with conn:` block, and never hold a transaction open across a Claude call, a web request or a Gmail read.
- **Backups:** before the review app applies a pending migration (`db.pending_migrations`), it copies the database with SQLite's backup API to `data/backups/jobs-YYYYMMDD-HHMM.db` (`db.backup`; `-1`, `-2`… within the same minute) and keeps the newest 10.
- Use parameterized SQL queries only. Never build SQL with string formatting.
- Respect API rate limits. Add backoff, and log to `logs/` instead of printing secrets.
