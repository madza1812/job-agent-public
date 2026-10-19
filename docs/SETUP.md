# First-time setup on Windows

This guide takes a fresh Windows 10 or 11 machine to a scheduled daily run. Allow about an hour, most of it in the Google Cloud console. Every command runs in **Windows PowerShell** from the project folder unless it says otherwise.

1. [Install the prerequisites](#1-install-the-prerequisites)
2. [Get the code and run setup](#2-get-the-code-and-run-setup)
3. [Log Claude Code in to your Claude plan](#3-log-claude-code-in-to-your-claude-plan)
4. [Connect Gmail (Google Cloud)](#4-connect-gmail-google-cloud)
5. [Set up job alerts and the JobAlerts label](#5-set-up-job-alerts-and-the-jobalerts-label)
6. [Fill in your profile](#6-fill-in-your-profile)
7. [Optional: Typefully for LinkedIn drafts](#7-optional-typefully-for-linkedin-drafts)
8. [Schedule it with Task Scheduler](#8-schedule-it-with-task-scheduler)
9. [Verify checklist](#9-verify-checklist)
10. [Troubleshooting](#10-troubleshooting)

## 1. Install the prerequisites

You need Python 3.12, Git and Claude Code. Python and Git install with `winget`, which ships with Windows 11 and current Windows 10:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Claude Code has its own installer for Windows ([install docs](https://docs.claude.com/en/docs/claude-code/setup)):

```powershell
irm https://claude.ai/install.ps1 | iex
```

**Close PowerShell and open a new window** after installing. A window that was open during the install still has the old `PATH` and won't find `python`, `git` or `claude`. Then check:

```powershell
py -3.12 --version     # Python 3.12.x
git --version
claude --version
```

## 2. Get the code and run setup

```powershell
cd $HOME
git clone https://github.com/<you>/job-agent.git
cd job-agent
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`scripts\setup.ps1` is safe to rerun at any time. It:

- checks for Python 3.12 (`py -3.12`, else `python` on `PATH`) and Git;
- creates `.venv` and installs `requirements.txt`;
- finds `claude.exe` and writes its full path to `config\local.yaml` (scheduled tasks don't use your `PATH`, so the code only ever uses this path);
- creates `data\`, `logs\` and `secrets\`;
- copies `profile\profile.example.yaml` and `profile\preferred_companies.example.yaml` to `profile.yaml` and `preferred_companies.yaml` if those don't exist yet (it never overwrites them);
- creates the SQLite database and runs the tests.

It ends with a list of the remaining steps. Options: `-RecreateVenv` rebuilds `.venv` (for example after a Python upgrade), `-PythonVersion 3.14` uses another Python, `-SkipTests` skips the test run.

## 3. Log Claude Code in to your Claude plan

The agents call `claude -p` in headless mode, so they use whatever login Claude Code has. It should be your **Claude plan** (Pro or Max), not an API key.

1. Run `claude` once. When it asks how to log in, choose your Claude account with a subscription and finish the login in the browser. Type `/status` to confirm it shows your plan, then `/exit`.
2. **Make sure `ANTHROPIC_API_KEY` is not set.** When it is, Claude Code uses the key instead of your plan, and every call is billed to the API account:
   ```powershell
   $env:ANTHROPIC_API_KEY                                                 # this window
   [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "User")     # your account
   [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", "Machine")  # the whole machine
   ```
   All three should print nothing. To remove it:
   ```powershell
   [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $null, "User")
   Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
   ```
   A machine-level variable needs an administrator PowerShell to remove. Open a new window afterwards.
3. Test a headless call:
   ```powershell
   claude -p "Reply with the word OK" --model sonnet
   ```

## 4. Connect Gmail (Google Cloud)

The agent reads your alert emails through the Gmail API with the **read-only** scope (`gmail.readonly`). You create your own OAuth client in your own Google Cloud project; nothing is shared with anyone. Use the Google account whose Gmail receives the alerts.

### 4.1 Create a project and enable the Gmail API

1. Open [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. In the project picker at the top, choose **New project**, name it (for example `job-agent`) and create it. Make sure it's selected.
3. Go to **APIs & Services → Library**, search for **Gmail API**, open it and click **Enable**.

### 4.2 Configure the consent screen

Google's console calls this the **OAuth consent screen** (newer projects show it as **Google Auth Platform**, with Branding, Audience, Data access and Clients pages).

1. Click **Get started** (or **Configure consent screen**). Enter an app name (for example `job-agent`) and your email as the support email.
2. **Audience: External.** "Internal" is only for Google Workspace organizations.
3. Enter your email as the developer contact, accept the policy, and create.
4. On **Audience → Test users**, click **Add users** and add your own Gmail address. While the app is in Testing mode, only test users can sign in.
5. Optional: on **Data access**, click **Add or remove scopes** and add `.../auth/gmail.readonly`. The agent asks for exactly this scope either way.

### 4.3 Create a Desktop OAuth client

1. Go to **Clients** (or **APIs & Services → Credentials**), click **Create client** (or **Create credentials → OAuth client ID**).
2. **Application type: Desktop app.** Name it and create it.
3. Click **Download JSON** and save the file as `secrets\credentials.json` in the project folder. `secrets\` is gitignored; never commit this file.

### 4.4 Log in once

```powershell
.venv\Scripts\python.exe -m agents.common.gmail auth
```

A browser opens. Pick your account. Google warns **"Google hasn't verified this app"**: it's your own app, so click **Continue**. Allow the read-only access. The token is saved to `secrets\token.json`. The agent refuses a token with any scope other than `gmail.readonly`.

Check it works:

```powershell
.\run.ps1 gmail-check      # the 10 newest JobAlerts headers (empty until step 5)
```

### 4.5 The 7-day expiry: Testing vs. In production

**In Testing mode, Google expires the login after 7 days.** Scheduled runs never open a browser, so on day 8 the run reads no email and the digest's Warnings section says the Gmail login expired. The fix is to run the `auth` command from 4.4 again. That's fine for a trial, but a weekly chore.

To get a login that lasts, **publish the app** (Audience → **Publish app** → In production). Google asks for public pages before it lets you publish; GitHub Pages hosts them for free:

1. Create a GitHub repository named `<your-username>.github.io` (or turn on **Settings → Pages** for any public repository).
2. Add three short pages: `index.html` (what the app is: a personal tool that reads your own job alert emails, read-only), `privacy.html` (it reads only your own Gmail with the read-only scope, stores data locally on your computer, and shares nothing) and `terms.html` (personal use, no warranty).
3. On the **Branding** page, fill in the **Application home page**, **Privacy policy link** and **Terms of service link** with those URLs, and add `<your-username>.github.io` under **Authorized domains**. If the console asks you to prove you own the domain, verify it in [Google Search Console](https://search.google.com/search-console) (the HTML-file method works on GitHub Pages).
4. Publish the app, then run the `auth` command from 4.4 once more to get a new token.

`gmail.readonly` is a restricted scope. Without Google's verification process, a published app keeps showing the "hasn't verified" warning at sign-in and is limited to a small number of users, which is fine for one person. You don't need to submit it for verification for your own use. Google changes these screens and rules from time to time, so follow what the console says if it differs from this.

## 5. Set up job alerts and the JobAlerts label

Scout reads only the Gmail label **JobAlerts**, and recognizes alerts by their exact sender:

| Site | Sender |
|---|---|
| LinkedIn | `jobalerts-noreply@linkedin.com`, `jobs-noreply@linkedin.com` |
| Indeed | `donotreply@jobalert.indeed.com` (saved searches), `donotreply@match.indeed.com` (matches) |
| Glassdoor | `noreply@glassdoor.com` |

**Create the alerts.** Keep each search fairly broad (role words and your locations); the hard filters and scoring do the narrowing.

- **LinkedIn:** search Jobs, set the location and filters (Remote, Date posted), turn on **Set alert**, and choose **Daily** by email in the alert settings.
- **Indeed:** search, then use **Get new jobs for this search by email** (or the bell icon) and keep email on.
- **Glassdoor:** search Jobs, then **Create job alert** with daily email.

**Create the Gmail filter.** In Gmail: **Settings (gear) → See all settings → Filters and Blocked Addresses → Create a new filter**. In **From**, paste:

```
jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com OR donotreply@jobalert.indeed.com OR donotreply@match.indeed.com OR noreply@glassdoor.com
```

Click **Create filter**, tick **Apply the label** and create a new label named exactly `JobAlerts`. Optionally tick **Skip the Inbox** and **Also apply filter to matching conversations**. Other senders that land in the label are logged and ignored.

After the first alerts arrive, `.\run.ps1 gmail-check` lists them.

## 6. Fill in your profile

Edit `profile\profile.yaml` and `profile\preferred_companies.yaml` (both gitignored). Every key is explained in [CONFIGURATION.md](CONFIGURATION.md), which also has a ready-to-paste Claude Code prompt that interviews you and writes both files from your resume.

Edit them in a text editor (VS Code, Notepad) and keep them UTF-8.

## 7. Optional: Typefully for LinkedIn drafts

The Publisher (`post-ideas`, `draft-post`) turns public facts about your work into LinkedIn **drafts** in [Typefully](https://typefully.com). It never publishes. Skip this section if you don't want it.

1. Create a Typefully account and connect your LinkedIn account. The free plan is enough.
2. In Typefully's settings, create an **API key**.
3. Create `.env` in the project folder (gitignored) with one line:
   ```powershell
   Set-Content -Path .env -Value "TYPEFULLY_API_KEY=<your key>" -Encoding ASCII
   ```
4. Optional inputs: put your resume in `profile\resume.md` (only the part from `### Summary` on is read, with contact details removed) and notes in `profile\post_notes.md`. Replace the fictional per-user lists in `config\publisher.yaml` with your own (see [CONFIGURATION.md](CONFIGURATION.md#configpublisheryaml)).
5. Try it: `.\run.ps1 post-ideas --dry-run`, then `.\run.ps1 post-ideas` and `.\run.ps1 draft-post 1`. The first call finds the social set with LinkedIn connected and caches its ID in `config\local.yaml`.

## 8. Schedule it with Task Scheduler

Two tasks: the whole **daily** run at 6 AM, and an extra **track** run at noon to catch replies to your applications. Run this once in PowerShell from the project folder (no administrator needed):

```powershell
$root = (Get-Location).Path
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)

foreach ($t in @(@{ Name = "job-agent daily"; Cmd = "daily"; At = "6:00AM" },
                 @{ Name = "job-agent track"; Cmd = "track"; At = "12:00PM" })) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\run.ps1`" $($t.Cmd)"
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.At
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings `
        -Description "job-agent: .\run.ps1 $($t.Cmd)"
}
```

- **Run as you, only when logged on** (the default for tasks you register this way). Claude Code's login and the Gmail token belong to your Windows account, and the Windows notification needs your desktop. A task set to "run whether the user is logged on or not" has no desktop, so you'd get no notification.
- **WakeToRun** wakes the PC from sleep for the run, but only if wake timers are allowed: **Control Panel → Power Options → Change plan settings → Change advanced power settings → Sleep → Allow wake timers → Enable** (on a laptop, for both battery and plugged in). Or from PowerShell:
  ```powershell
  powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
  powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
  powercfg /setactive SCHEME_CURRENT
  ```
- **StartWhenAvailable** ("Run task as soon as possible after a scheduled start is missed") runs a missed 6 AM run when the PC comes back on.
- A second daily run while one is still going doesn't start (`data\daily.lock`); a lock untouched for 2 hours is treated as stale and taken over.
- The exit code is 0 only when every step passed, so Task Scheduler's **Last Run Result** shows `0x0` on a clean run and `0x1` when something needs a look in the digest.

Test the daily task right away with **Run** in Task Scheduler (or `Start-ScheduledTask "job-agent daily"`), then check the result in `digest\` and `logs\daily-*.log`.

## 9. Verify checklist

- [ ] `py -3.12 --version`, `git --version` and `claude --version` work in a new PowerShell window.
- [ ] `scripts\setup.ps1` finished without warnings and the tests passed.
- [ ] `config\local.yaml` exists and `claude_path` points to an existing `claude.exe`.
- [ ] `claude -p "Reply with the word OK" --model sonnet` prints OK, and `ANTHROPIC_API_KEY` is unset everywhere.
- [ ] `secrets\credentials.json` and `secrets\token.json` exist, and `.\run.ps1 gmail-check` lists JobAlerts headers.
- [ ] `profile\profile.yaml` and `profile\preferred_companies.yaml` are yours, not the examples.
- [ ] `.\run.ps1 fetch` reports emails read and jobs found per sender.
- [ ] `.\run.ps1 daily` finishes, `digest\YYYY-MM-DD.md` exists, and its **Last run** line shows every step `ok`.
- [ ] `.\run.ps1 review` opens the review app in your browser.
- [ ] Both scheduled tasks exist; a manual **Run** of the daily task ends with Last Run Result `0x0` and a Windows notification.
- [ ] Optional: `.\run.ps1 post-ideas --dry-run` shows the size of each source.

## 10. Troubleshooting

**`python`, `git` or `claude` is not recognized.** Open a new PowerShell window after installing. If `python` opens the Microsoft Store, turn off the store stubs in **Settings → Apps → Advanced app settings → App execution aliases** (`python.exe`, `python3.exe`), or use `py -3.12`. The agents themselves never depend on `PATH`: they use `.venv` and the `claude_path` in `config\local.yaml`.

**"Wrong Python — use .\run.ps1 or .venv\Scripts\python.exe".** You ran the code with another interpreter. Use `.\run.ps1 <command>`, or `.venv\Scripts\python.exe -m ...` for the commands without a verb. If `.venv` is broken or was made with another Python version, run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -RecreateVenv`.

**"running scripts is disabled on this system".** Windows blocks local scripts by default. Either start each script with `powershell -ExecutionPolicy Bypass -File ...` (as the scheduled tasks do), or allow your own scripts once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

If a downloaded zip's scripts are still blocked, run `Get-ChildItem -Recurse | Unblock-File` in the project folder.

**Garbled characters (â€" instead of —).** Windows PowerShell 5.1 doesn't read files as UTF-8 by default. Use `Get-Content -Encoding UTF8 digest\2026-10-19.md`. When you write a file from PowerShell, pass `-Encoding` explicitly; for `.env` use `-Encoding ASCII`, because `-Encoding UTF8` in Windows PowerShell adds a byte-order mark and the first line is then not recognized ("`.env` has no TYPEFULLY_API_KEY"). The YAML files are fine with or without the mark.

**"Gmail login expired or was revoked".** The digest shows it under Warnings, and the run still does everything else. Run:

```powershell
.venv\Scripts\python.exe -m agents.common.gmail auth
```

It happens every 7 days while the Google app is in Testing mode (see [4.5](#45-the-7-day-expiry-testing-vs-in-production)), or after you revoke access in your Google account. If it complains about scopes, delete `secrets\token.json` and run it again.

**Claude usage limit reached.** When your plan's limit is hit, each Claude step stops at its first call without retrying, and the digest lists the failed steps. Nothing is lost: jobs stay where they were. Wait for the limit to reset and run the remaining steps by hand (`.\run.ps1 score`, `details`, `pass2`, `shortlist`, `research`, `digest`), or let tomorrow's run pick them up. `.\run.ps1 stats --days 7` shows the calls and tokens per day.

**"claude.exe can't be found" / every Claude step failed.** `config\local.yaml` is missing or points to an old path (Claude Code updates can move it). Rerun `scripts\setup.ps1`.

**"Not started: …" from `daily`.** A run is still going, or one crashed less than 2 hours ago. Wait, or delete `data\daily.lock` if you're sure nothing is running.

**No Windows notification.** The task runs whether you're logged on or not (it must run only when you're logged on), or Focus Assist is on. `--no-toast` turns notifications off on purpose.

**The scheduled run didn't happen.** Check the task's **History** tab and Last Run Result. If the PC was asleep, check that wake timers are enabled (section 8); if it was off, StartWhenAvailable runs it at the next start.
