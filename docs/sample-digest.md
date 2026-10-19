# Job digest 2026-10-19

**Last run:** 2026-10-19 00:01 – 00:01 · fetch ok, track ok, score ok, details ok, pass2 ok, cleanup ok, shortlist ok, research ok, digest ok, review ok

## Warnings

None.

## Application updates

**New today**

- 2026-10-10 #9 **Wide World Importers**: Software Engineer II, confirmation: Application received for Software Engineer II
- 2026-10-18 #9 **Wide World Importers**: Software Engineer II, interview invite: Invitation to a 30-minute recruiter screen (next: Pick a time by Friday) → now **interviewing**

**Consider a follow-up**

- #10 **Adventure Works**: Data Platform Engineer, no reply for 16 days

## Today's top 3

### 1. Acme Robotics: Applied AI Engineer (86.4)

- **Location:** Portland, OR (Hybrid)
- **Posted:** Posted 4 days ago (from posting)
- **Salary:** $145,000–$180,000
- **Why:** Agent tooling for warehouse operators matches your LLM triage work and Python/AWS backend depth.
- **Gaps:** No production fine-tuning experience; Posting asks for some ROS exposure
- **Apply:** https://jobs.example.com/acmerobotics/1001
- **Source:** official
- **Brief:** [Read brief](../briefs/2026-10-19-acme-robotics-applied-ai-engineer.md)
- **Sub-scores:** skills 92 · seniority 85 · role 90 · employer 80 · company 100 · location 100 · comp 100 (pass 2)
- Job #1. `.\run.ps1 rate 1 up` / `down`, `.\run.ps1 skip 1 "reason"`

### 2. Globex: Senior Backend Engineer, LLM Platform (79.2)

- **Location:** Seattle, WA (Hybrid)
- **Posted:** Posted 9 days ago (from posting)
- **Salary:** $160,000–$205,000
- **Why:** Platform work behind LLM features; your API and PostgreSQL experience fits the core of the role.
- **Gaps:** Seattle 3 days a week
- **Apply:** https://jobs.example.com/globex/1002
- **Source:** official
- **Brief:** [Read brief](../briefs/2026-10-19-globex-senior-backend-engineer-llm-platform.md)
- **Sub-scores:** skills 84 · seniority 80 · role 82 · employer 85 · company 80 · location 50 · comp 100 (pass 2)
- Job #2. `.\run.ps1 rate 2 up` / `down`, `.\run.ps1 skip 2 "reason"`

### 3. Tailspin Toys: ML Platform Engineer (77.0)

- **Location:** Remote (US)
- **Posted:** Posted 2 days ago (from posting)
- **Salary:** not stated
- **Why:** Remote platform role around model serving; good match for your Python and Docker background.
- **Gaps:** Kubernetes listed as required
- **Apply:** https://jobs.example.com/tailspintoys/1003
- **Source:** official
- **Brief:** not written yet; research retries it next run (`.\run.ps1 research 3`)
- **Sub-scores:** skills 78 · seniority 80 · role 75 · employer 70 · company 65 · location 100 · comp 50 (pass 2)
- Job #3. `.\run.ps1 rate 3 up` / `down`, `.\run.ps1 skip 3 "reason"`

## Worth checking yourself

These would have made the top 3, but no open official posting is confirmed, so they weren't shortlisted.

- #4 81.0 **Fabrikam**: AI Engineer (Remote (US)), found only on aggregator — run set-posting if you find the official page; aggregator posting: https://www.builtin.com/job/ai-engineer/4. Posted ~3 days ago (alert). Alert link: https://www.linkedin.com/jobs/view/5000000004/

## Still waiting

- #7 **Contoso Logistics**: Backend Engineer, Routing (74.1), waiting 2 days, researched. Posted 6 days ago (from posting). https://jobs.example.com/contosologistics/1005

On hold, with a posting past the age limit (held jobs are never closed):

- #8 **Northwind Traders**: Forward Deployed Engineer (72.5), Posted 34 days ago (from posting) — older posting, check it's still open

## Cleaned up today

Closed today: shortlisted jobs with no action from you (no rating, hold, skip or apply), and postings too old (limit 10 days; 30 for a confirmed open official posting, 45 at a preferred company). Take one back with `.\run.ps1 reopen <id> "reason"`.

- #11 **Proseware**: Backend Engineer, AI Search (66.4), stale: no action 10 days
- #12 **Lucerne Publishing**: AI Engineer (64.0), posting too old: 14 days

## Runners-up 4-10

4. #5 68.9 **Woodgrove Bank**: Software Engineer, GenAI (Seattle, WA)
5. #13 63.1 **Coho Winery**: Software Engineer, Internal Tools (Portland, OR)
6. #14 61.7 **Fourth Coffee**: Python Engineer (Remote (US))
7. #15 60.2 **Margie's Travel**: Backend Developer (Seattle, WA)
8. #6 58.0 **Litware**: Backend Engineer (Python) (Remote (US))

## Run stats

- Emails read: 14
- Jobs found: 212
- Application emails classified: 2
- Filtered out: 131
- Scored (pass 1): 81
- Postings found: 9 (7 official, 2 aggregator); rescored in pass 2: 12
- Briefs written: 2
- Steps: fetch ok, track ok, score ok, details ok, pass2 ok, cleanup ok, shortlist ok, research ok
- Claude usage: 20 calls, 102,880 input tokens (54,000 from cache), 28,800 output tokens, 24 web searches (≈ $0.78 at API prices)
  - claude-haiku-5-5: input=0 cache_creation=0 cache_read=0 output=1700 web_searches=24 cost_usd=0.0700
  - claude-sonnet-5-5: input=31880 cache_creation=17000 cache_read=54000 output=27100 cost_usd=0.7100

## Commands

| Command | What it does |
|---|---|
| `.\run.ps1 daily` | Whole morning run: fetch, track, score, details, pass 2, cleanup (stale, too old), shortlist, research, digest |
| `.\run.ps1 track --since-days 7` | Read application emails again and update applied jobs |
| `.\run.ps1 review` | The review app: tabs, Read panel (brief, posting, paste the official URL), rate, hold, skip, shortlist now, "I applied" (127.0.0.1 only) |
| `review\index.html` | Read-only copy for your phone: every job in play with its brief rendered (`.\run.ps1 review --static` rebuilds it) |
| `.\run.ps1 research <id>` | (Re)write one job's brief |
| `.\run.ps1 top 15` | Highest-scoring open jobs |
| `.\run.ps1 show shortlisted` | Jobs by status |
| `.\run.ps1 rate <id> up "note"` | Thumbs up; teaches the scorer (also `down`) |
| `.\run.ps1 skip <id> "reason"` | Close a job you won't pursue |
| `.\run.ps1 reopen <id> "reason"` | Take back a close, e.g. one under "Cleaned up today" (closed -> scored) |
| `.\run.ps1 hold <id> "reason" --until YYYY-MM-DD` | Out of the shortlist and Still waiting until then (`unhold <id>` to undo) |
| `.\run.ps1 set-posting <id> <url>` | You found the official posting: store it, rescore, rewrite the brief |
| `.venv\Scripts\python.exe -m agents.db mark-applied <id> --method ats` | You applied (only you do this) |
| `.\run.ps1 rescore` | Rescore everything after prompt or weight changes |
| `.\run.ps1 refilter --dry-run` | Preview hard-filter changes after editing profile.yaml |
| `.\run.ps1 posting-age --dry-run` | Which open jobs have a posting too old (daily closes them) |
| `.\run.ps1 gmail-check` | Check the Gmail connection |
