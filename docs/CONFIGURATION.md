# Configuration reference

Everything that makes job-agent yours lives in two gitignored files, `profile\profile.yaml` and `profile\preferred_companies.yaml`, which `scripts\setup.ps1` copies from the fictional `*.example.yaml` files. Two committed files, `config\source_trust.yaml` and `config\publisher.yaml`, and the prompts in `prompts\` tune behavior for everyone.

- [profile.yaml](#profileyaml)
  - [About you: identity, skills, experience](#about-you-identity-skills-experience)
  - [Targets and seniority](#targets-and-seniority)
  - [Hard filters](#hard-filters)
  - [Posting-age limits](#posting-age-limits)
  - [Scoring weights](#scoring-weights)
  - [Commute scoring](#commute-scoring)
  - [Shortlist minimum, Waiting expiry and holds](#shortlist-minimum-waiting-expiry-and-holds)
  - [Feedback, sanitize and publisher](#feedback-sanitize-and-publisher)
- [preferred_companies.yaml](#preferred_companiesyaml)
- [config/source_trust.yaml](#configsource_trustyaml)
- [config/publisher.yaml](#configpublisheryaml)
- [Prompt files](#prompt-files)
- [After changing settings](#after-changing-settings)
- [Adapting it to yourself](#adapting-it-to-yourself)

Edit the files in a text editor, keep them UTF-8, and keep the YAML indentation (two spaces, no tabs).

## profile.yaml

### About you: identity, skills, experience

These go to Claude as they are, in every scoring call and every research brief. Nothing in code reads individual fields, so write them for a reader: clear, specific and true.

| Key | What it does | Example |
|---|---|---|
| `identity.name` | Your name. Also replaced automatically when `tools/sanitize_email.py` saves a test fixture | `Alex Rivera` |
| `identity.current_title` | Your current job title | `Backend Engineer` |
| `identity.years_experience` | Years of relevant experience; the scorer compares it with the years a posting asks for | `6` |
| `identity.summary` | Three or four sentences: what you do and what you're moving toward | see the example file |
| `identity.education` | Degrees and courses, one line each | `B.S., Computer Science, Example State University, 2019` |
| `skills.core` | What you use daily and can be interviewed on. Weighs most in `skills_match` | `Python`, `PostgreSQL` |
| `skills.familiar` | What you've used but wouldn't lead with | `Kubernetes` |
| `skills.learning` | What you're picking up; a gap here counts less | `Model fine-tuning` |
| `experience` | Jobs, newest first: `company`, `title`, `years`, `highlights` (results, with numbers where you have them) | see the example file |

### Targets and seniority

Also read by Claude; they drive `role_interest`, `seniority_fit` and `company_similarity`.

| Key | What it does | Example |
|---|---|---|
| `targets.roles` | The roles you want, most wanted first. A job that matches none of them still scores; it just rates lower on role interest | `Applied AI Engineer`, `Backend Engineer` |
| `targets.seniority` | Levels you'd take. Free text Claude reads: `junior`, `mid`, `senior`, `staff`, `lead`, `principal` | `[mid, senior]` |
| `targets.industries_like` | Industries that get a domain-fit boost; an empty list means no preference | `Developer tools` |
| `targets.industries_avoid` | Industries rated down | `[gambling]` |
| `targets.company_size` | Sizes you're open to: `startup`, `mid`, `enterprise` | `[startup, mid, enterprise]` |

To rule a level out completely, use `exclude_title_words` below; `targets.seniority` only lowers the score.

### Hard filters

`hard_filters` drop postings that clearly don't fit, before any Claude call. **Unknown data passes**: a posting with no location or salary is kept and left to the scorer, since a dropped job is never seen. Rejections are counted in `.\run.ps1 stats`.

| Key | What it does | Default | Example |
|---|---|---|---|
| `require_title_words` | A title must contain at least one of these. Whole words, case-insensitive; a multi-word term also matches hyphenated ("full stack" matches "Full-Stack") | off (empty or missing) | `[engineer, developer, software, ai, ml]` |
| `exclude_title_words` | A title containing any of these is dropped. Whole words, case-insensitive. `senior` also catches Sr, Sr. and Snr; `junior` catches Jr and Jnr; `manager` catches Mgr | none | `[principal, director, vp, intern]` |
| `locations` | Places you'd work. A posting matches if its location text contains the name or one of its aliases. `Remote-US` is a label for remote work (handled by `remote_ok`) | none: no location filter | `[Portland, Seattle, Remote-US]` |
| `location_aliases` | Other names for each place: cities, suburbs, airport codes. Matched as whole words; short all-caps aliases (`SF`, `PDX`) are matched case-sensitively; an alias can name another key to include all of its aliases | none | `Portland: [Portland, PDX, Beaverton]` |
| `remote_ok` | Keep remote postings wherever they're based (in the US) | `false` | `true` |
| `min_salary` | Drop postings whose stated **maximum** is below this, in USD a year. Ignored for job-site estimates and other currencies | none | `120000` |
| `exclude_companies` | Companies you never want. Compared after normalizing (case, punctuation, Inc/LLC/Corp, a leading "The") | `[]` | `[Initech]` |
| `needs_sponsorship` | Reserved; not used by the code yet | `false` | `false` |

How a location is judged: a place you listed (or one of its aliases) matches; a foreign city or another US state fails; a remote posting matches when `remote_ok` is true; anything unclear ("United States", no location) passes. A job listing several places ("Seattle or Remote") matches if any of them does.

### Posting-age limits

Old postings are rarely worth applying to. Each job stores a date and where it came from, best first:

1. `ats` – the ATS's own data (a polled feed, the free board check, Workable/BambooHR/Breezy JSON)
2. `posting_page` – the official posting page (JSON-LD `datePosted`, published-date metadata, or the date the details lookup read there)
3. `alert_badge` – an alert card's "3 days ago" or "30+ days ago" (Indeed, Glassdoor), counted back from the email's date; "30+" counts as 30
4. `email_date` – the alert email's own date: the posting is at least that old, but the real date is unverified

A better source replaces a worse one, never the other way round. Dates from aggregator pages are ignored.

| Key (under `hard_filters`) | What it does | Default |
|---|---|---|
| `max_posting_age_days` | The limit for most jobs. Also turns on the age filter at fetch | `10` |
| `max_official_open_age_days` | The limit for an official posting confirmed open: `source_trust` official and pass 2 doesn't say it may be closed | `30` |
| `max_official_open_age_days_preferred` | The same, at a company in `preferred_companies.yaml` | `45` |

A job over its limit is filtered out at fetch, refused by the shortlist and Shortlist now, and closed by the daily cleanup with detail `posting too old: N days`. Applied and interviewing jobs are never touched; held jobs are only flagged ("older posting, check it's still open"); a job you reopen after an age close is left alone. A job known only by its email date can still be shortlisted, labeled "date unverified". `.\run.ps1 posting-age --dry-run` lists what would close today.

### Scoring weights

`scoring_weights` is **required**. The final score (0–100) is the weighted average of seven components; the weights don't have to add up to 100, only their ratios matter.

| Component | Rated by | What it measures |
|---|---|---|
| `skills_match` | Claude | How well your skills and experience cover what the job needs |
| `seniority_fit` | Claude | Whether the level and years asked match yours and `targets.seniority` |
| `role_interest` | Claude | How close the role is to `targets.roles`. The prompt rates roles centered on AI, agentic or LLM work highest; change that in the prompts if your field is different |
| `employer_quality` | Claude | The employer's reputation, stability and engineering culture, as far as known |
| `company_pref` | Code | Your tier for a preferred company (dream 100, strong 80, interested 65); for others, Claude's `company_similarity` (how much the company resembles the ones you prefer) |
| `location` | Code | 100 for a matching location or remote, 50 otherwise; with `commute`, the commute score below |
| `compensation` | Code | 100 at or above `min_salary`, 20 below it, 50 when the salary is unknown |

Then −10 for a staffing agency and −5 for a sponsored listing, clamped to 0–100, and a dealbreaker Claude finds (for example a clearance you can't get) caps the score at 20.

Example, for someone who cares most about the work itself:

```yaml
scoring_weights:
  skills_match: 30
  seniority_fit: 20
  role_interest: 15
  employer_quality: 10
  company_pref: 10
  location: 10
  compensation: 5
```

### Commute scoring

Optional. Without `commute`, every matching location scores 100. With it, a matching job scores the best of its places:

| Key | What it does | Example |
|---|---|---|
| `commute.remote` | Score for remote work | `100` |
| `commute.places.<place>` | A flat score for that place, or scores by office days per week. `<place>` must be one of `hard_filters.locations` | `Portland: 100` |
| office-day keys | `"1-2"`, `3`, `"4-5"` (a day count or range) and **`unknown`** (required): used until pass 2 reads the office days from the posting, or when it doesn't say | see below |

```yaml
commute:
  remote: 100
  places:
    Portland: 100        # lives here: any schedule
    Seattle:
      1-2: 75
      3: 50
      4-5: 20
      unknown: 55
```

A job the ATS marks remote counts as 0 office days, which scores as remote.

### Shortlist minimum, Waiting expiry and holds

| Key | What it does | Default |
|---|---|---|
| `shortlist_min_score` | A job needs at least this final score to be shortlisted (also the bar for Near misses and Shortlist now). Up to 3 a day; fewer is fine | `60` |
| `waiting_expiry_days` | The daily cleanup closes a shortlisted or researched job with no action from you (rating, hold, skip, posting URL, applying) this many days after it was shortlisted. Held jobs are never closed | `10` |

**Holds** aren't a setting: put a job on hold from the review app or with `.\run.ps1 hold <id> "reason" [--until YYYY-MM-DD]`. A held job keeps its status, stays out of the shortlist, Still waiting and the cleanup, and comes back by itself on the date (or with `unhold`).

### Feedback, sanitize and publisher

| Key | What it does |
|---|---|
| `feedback.liked_examples`, `feedback.disliked_examples` | Written by 👍/👎 in the review app or `.\run.ps1 rate`: the last 20 of each, with your note. Both scoring passes see them. Leave them empty to start; you don't edit them by hand |
| `sanitize.replace` | Pairs of personal text → placeholder that `tools/sanitize_email.py` replaces when it saves a real email as a test fixture (your name and Gmail address are replaced automatically) |
| `publisher.blocked_terms` | Private names from your work that may never appear in a LinkedIn draft: customers, suppliers, internal systems. Case-insensitive, whole words. Example: `[Northwind Fleet Portal, ShipTrack Internal]` |

## preferred_companies.yaml

A scoring reference, not a fetch list: jobs still come from your alerts. Every job's company is matched against this list and the tier is stored on the job.

```yaml
companies:
  - name: Acme Robotics            # canonical name
    aliases: [Acme Robotics Labs]  # other names it posts under
    tier: dream                    # dream | strong | interested
    why: "Ships LLM agents to warehouse operators"
  - name: Globex
    tier: strong
    poll_feed: true                # optional: also fetch its public ATS board every day
    ats: greenhouse                # with poll_feed: greenhouse | lever | ashby
    slug: globex                   # with poll_feed: the board name in the ATS's URL
```

| Key | What it does |
|---|---|
| `name`, `aliases` | Matched ignoring case, punctuation, a leading "The" and suffixes (Inc, LLC, Corp, Global Tech, Technologies, …), so "Globex Global Tech" matches "Globex" |
| `tier` | `dream` (company score 100), `strong` (80) or `interested` (65). Also gives the posting-age limit of 45 days instead of 30 |
| `why` | For you and for the research brief |
| `poll_feed`, `ats`, `slug` | Fetch the company's public Greenhouse, Lever or Ashby board directly. The slug is the part after the ATS domain in its job-board URL, e.g. `boards.greenhouse.io/globex` → `globex` |

Companies on the list also feed the Publisher's guard: no draft may name a company you prefer.

## config/source_trust.yaml

Decides which links count as **official** (only official jobs can be shortlisted). Committed and shared; edit it when a real employer's site is labeled wrong, then run `retrust`.

| Key | What it holds |
|---|---|
| `ats_domains` | Applicant tracking systems. A domain covers its subdomains; an entry with a path counts only under that path (`oraclecloud.com/hcmUI/CandidateExperience`) |
| `public_sector_domains` | Government and school job boards |
| `company_domains` | A company's careers domains when they can't be told from its name: `"Sitewire by Hexacorp": [sitewire.com, hexacorp.com]`. A host label equal to the company's name, joined up or its first word (`jobs.supermicro.com` for Supermicro), is recognized without an entry |
| `ats_sender_domains` | Domains ATSs send candidate email from, for the application tracker |
| `blocked_fetch_domains` | Never fetched by the details lookup (LinkedIn) |

A posting is official when either of its links (the page read, or the apply link) is official, and the official one becomes the apply link.

## config/publisher.yaml

The Publisher guard's lists. The per-user ones ship with fictional examples; replace them with yours.

| Key | What it holds |
|---|---|
| `hype_words` | Words a post may not use ("game-changer", "thrilled to announce") |
| `common_word_companies` | **Per user.** Preferred or applied-to companies whose names are ordinary words; matched only as written, so "a beacon" passes and "Beacon" doesn't |
| `allowlist` | **Per user.** Public phrases from your resume exempt from the company and security checks, such as a former employer or a product you used. IP addresses, ports, hostnames and tunnel IDs are still refused around them |
| `security_terms` | Security setup that stays out of posts (firewall, VPN, credentials, …) |
| `application_patterns` | Regexes for application and recruiter phrases ("I applied to", "interviewing with") |

## Prompt files

Edit these to change how Claude judges or writes. Run the matching command afterwards to apply the change to existing jobs.

| File | Controls | Apply with |
|---|---|---|
| `prompts/score_pass1.md` | The scale for each rated component; what counts as role interest, a dealbreaker, a staffing agency | `rescore` |
| `prompts/score_pass2.md` | Pass 2 (full posting): repeats pass 1's scales plus office days, red flags, possibly closed, posting type. **Keep the two in step** | `pass2` on the next run, or `set-posting` for one job |
| `prompts/details.md` | Where and in what order to look for the official posting | next `details` |
| `prompts/research_company.md` | Company research: sources, search budget, citation rules | `research <id> --refresh-company` |
| `prompts/research_role.md` | The brief, including its **template** between `<!-- template -->` and `<!-- /template -->`. The validator reads the headings from there, so change the template only in this file | `research <id>` |
| `prompts/track_classify.md` | How application emails are categorized | next `track` |
| `prompts/post_ideas.md`, `prompts/post_draft.md` | Post ideas and drafts (the code guard still applies) | next `post-ideas` / `draft-post` |

## After changing settings

| You changed | Run |
|---|---|
| `hard_filters` (titles, locations, aliases, salary, excluded companies) | `.\run.ps1 refilter --dry-run` to see which `new`/`scored` jobs would close (and which filtered ones would come back), then `.\run.ps1 refilter` |
| `scoring_weights`, `commute`, `targets`, `skills`, tiers, or `score_pass1.md` | `.\run.ps1 rescore --dry-run`, then `.\run.ps1 rescore`: every `scored` job goes back to `new` and is scored again (Claude calls, in batches). Shortlisted and later jobs are never touched. Pass 2 reruns on the new top 15 in the next daily run |
| `config/source_trust.yaml` | `.\run.ps1 retrust --dry-run` lists every open job whose label would change, then `.\run.ps1 retrust` |
| posting-age limits | `.\run.ps1 posting-age --dry-run`; the daily run applies them |
| `shortlist_min_score`, `waiting_expiry_days` | Nothing: the next daily run uses them |

All the `--dry-run` forms only print; they change nothing.

## Adapting it to yourself

### Let Claude Code write your profile

Put your resume in `profile\resume.md` (plain text or markdown; gitignored), open Claude Code in the project folder (`claude`), and paste this prompt:

````text
Help me set up job-agent's profile for my own job search. Work only in this repository.

1. Read docs/CONFIGURATION.md, profile/profile.example.yaml, profile/preferred_companies.example.yaml
   and my resume, profile/resume.md. Do not open anything under data/, digest/, briefs/, review/,
   drafts/, archive/, logs/ or secrets/.

2. Interview me ONE question at a time and wait for my answer before asking the next. For each
   question, propose an answer drawn from my resume so I can simply say "yes" or correct it.
   Cover, in this order, skipping anything my answers already settled:
   - the roles I want, most wanted first, and the levels I'd take (and any I'd refuse outright);
   - where I can work: cities and suburbs, remote, and how many office days a week I'd accept
     for each place (for commute scoring);
   - my salary floor in USD per year;
   - title words a job must contain, and words that rule it out;
   - industries I like or avoid, company sizes, and companies I never want;
   - dealbreakers (clearance, travel, relocation, visa);
   - companies I'd love to work for, with a tier each (dream, strong, interested) and why;
     for each, whether it has a public Greenhouse, Lever or Ashby board to poll;
   - what matters most to me, to set scoring_weights;
   - how strict to be on posting age, and whether the defaults 10/30/45 suit me;
   - private names from my work that must never appear in a LinkedIn post.
   Keep it to about 15 questions.

3. Draft profile/profile.yaml and profile/preferred_companies.yaml with the same structure and
   comments as the example files. Rules:
   - identity, skills and experience come only from my resume and my answers; never invent
     facts, numbers or employers;
   - every place under commute.places must also be in hard_filters.locations, and each
     office-day rule needs an "unknown" score;
   - scoring_weights may only use skills_match, seniority_fit, role_interest, employer_quality,
     company_pref, location and compensation;
   - leave feedback.liked_examples and feedback.disliked_examples empty;
   - fill sanitize.replace with my name, my email addresses, my phone number and my town,
     each mapped to a neutral placeholder.
   Show me both files and a short summary of the choices, and wait for my OK before saving.

4. After saving, check them:
   .venv\Scripts\python.exe -c "from agents.scout import filters, score, companies; p = filters.load_profile(); score.check_commute(p); assert set(p['scoring_weights']) <= set(score.COMPONENTS); print(len(companies.load_preferred()), 'preferred companies; profile OK')"
   Then run .\run.ps1 refilter --dry-run and tell me what it would close.

Do not change any other file, do not run commands that call Claude (daily, score, rescore,
details, pass2, research, track), and do not commit anything.
````

Review the result yourself before the first run: the files decide what you see.

### Starting fresh (a new person, or a clean slate)

The database, digests and briefs belong to whoever's profile produced them. To hand the setup to someone else, or start over:

1. **Pause the schedule:** `Disable-ScheduledTask "job-agent daily"; Disable-ScheduledTask "job-agent track"`.
2. **Archive the old data** into one gitignored folder, and never read from it into the new setup:
   ```powershell
   $dest = "archive\old-$(Get-Date -Format yyyy-MM-dd)"
   New-Item -ItemType Directory -Force $dest, "$dest\profile" | Out-Null
   Move-Item data\jobs.db* $dest                       # the database (and any -wal/-shm files)
   foreach ($d in "digest", "briefs", "review", "drafts") { if (Test-Path $d) { Move-Item $d $dest } }
   Move-Item profile\profile.yaml, profile\preferred_companies.yaml $dest\profile
   foreach ($f in "profile\resume.md", "profile\post_notes.md") { if (Test-Path $f) { Move-Item $f $dest\profile } }
   ```
3. **Create a new, empty database:** `.venv\Scripts\python.exe -m agents.db init`.
4. **Write the new profile:** rerun `scripts\setup.ps1` to copy the examples back, add the new resume, and use the prompt above.
5. **New job alerts:** create the alerts in the new person's LinkedIn, Indeed and Glassdoor accounts, and the Gmail filter for the `JobAlerts` label (see [SETUP.md](SETUP.md#5-set-up-job-alerts-and-the-jobalerts-label)).
6. **Reconnect Gmail:** delete `secrets\token.json`. For a different Google account, add it as a test user in the Google Cloud project (or create a new project and `secrets\credentials.json` for it). Then run `.venv\Scripts\python.exe -m agents.common.gmail auth` and `.\run.ps1 gmail-check`.
7. **Typefully (if used):** replace the key in `.env` and delete the `typefully_social_set_id` line in `config\local.yaml`.
8. **First run:** `.\run.ps1 daily`, check the digest, then `Enable-ScheduledTask "job-agent daily"; Enable-ScheduledTask "job-agent track"`.
