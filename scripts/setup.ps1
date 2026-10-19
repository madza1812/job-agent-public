<#
.SYNOPSIS
    Set up the job agent on this machine. Safe to rerun.

.DESCRIPTION
    1. Checks that Python (3.12 by default) and Git are installed.
    2. Creates .venv with that Python, unless it exists already with the same version.
    3. Installs requirements.txt into .venv.
    4. Finds claude.exe and writes its absolute path to config/local.yaml (gitignored).
    5. Creates data/, logs/ and secrets/ if missing, and runs the database migrations
       (only pending ones; an existing data/jobs.db keeps its data).
    6. Runs the tests.
    7. Prints what's left to do (Gmail login, first run).

.PARAMETER PythonVersion
    The Python version for .venv (default 3.12).

.PARAMETER RecreateVenv
    Delete and rebuild .venv when it was made with another Python version.

.PARAMETER SkipTests
    Don't run pytest.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -RecreateVenv
#>
param(
    [string]$PythonVersion = "3.12",
    [switch]$RecreateVenv,
    [switch]$SkipTests
)

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$localConfig = Join-Path $root "config\local.yaml"
$problems = New-Object System.Collections.Generic.List[string]

function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "    $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "    $text" -ForegroundColor Yellow; $problems.Add($text) }
function Fail([string]$text) { Write-Host ""; Write-Host "SETUP STOPPED: $text" -ForegroundColor Red; Pop-Location; exit 1 }

function Get-PythonVersion([string]$exe, [string[]]$prefix = @()) {
    # "3.12" for the interpreter, or $null when it can't run.
    try {
        $out = & $exe @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return "$out".Trim() }
    } catch { }
    return $null
}

Push-Location -LiteralPath $root

# 1. Python and Git ----------------------------------------------------------------
Step "Checking Python $PythonVersion and Git"
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    if ((Get-PythonVersion "py" @("-$PythonVersion")) -eq $PythonVersion) {
        $python = (& py "-$PythonVersion" -c "import sys; print(sys.executable)").Trim()
    }
}
if (-not $python) {
    foreach ($cmd in @(Get-Command python, python3 -CommandType Application -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source -like "*WindowsApps*") { continue }  # the Microsoft Store stub
        if ((Get-PythonVersion $cmd.Source) -eq $PythonVersion) { $python = $cmd.Source; break }
    }
}
if (-not $python) {
    Fail "Python $PythonVersion not found. Install it (winget install Python.Python.$PythonVersion), then rerun."
}
Ok "Python $PythonVersion`: $python"

$git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $git) { Fail "Git not found. Install it (winget install Git.Git), then rerun." }
Ok "Git: $($git.Source) ($((& git --version) -replace 'git version ', ''))"

# 2. Virtual environment -----------------------------------------------------------
Step "Virtual environment (.venv)"
if (Test-Path -LiteralPath $venvPython) {
    $have = Get-PythonVersion $venvPython
    if ($have -ne $PythonVersion) {
        if (-not $RecreateVenv) {
            Fail ".venv uses Python $have, not $PythonVersion. Rerun with -RecreateVenv to rebuild it, or with -PythonVersion $have to keep it."
        }
        Write-Host "    removing .venv (Python $have)"
        Remove-Item -LiteralPath $venv -Recurse -Force
    }
}
if (Test-Path -LiteralPath $venvPython) {
    Ok ".venv exists (Python $PythonVersion), kept"
} else {
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) { Fail "creating .venv failed" }
    Ok "created .venv with Python $PythonVersion"
}

# 3. Dependencies ------------------------------------------------------------------
Step "Installing requirements.txt"
& $venvPython -m pip install --disable-pip-version-check --quiet -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (see above)" }
Ok "dependencies installed"

# 4. claude.exe --------------------------------------------------------------------
Step "Locating the Claude Code CLI"
$claude = Get-Command claude -CommandType Application -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -like "*.exe" } | Select-Object -First 1 -ExpandProperty Source
if (-not $claude) {
    $guess = Join-Path $HOME ".local\bin\claude.exe"
    if (Test-Path -LiteralPath $guess) { $claude = $guess }
}
if ($claude) {
    $claude = (Resolve-Path -LiteralPath $claude).Path
    $yamlPath = $claude.Replace("'", "''")
    $content = "# Machine-specific settings, written by scripts/setup.ps1. Not in git; rerun setup to refresh.`r`n" +
               "claude_path: '$yamlPath'`r`n"
    # Keep other settings already there (e.g. typefully_social_set_id, public_repo).
    if (Test-Path -LiteralPath $localConfig) {
        foreach ($line in [IO.File]::ReadAllLines($localConfig)) {
            if ($line -notmatch '^\s*(#|claude_path:)' -and $line.Trim()) { $content += "$line`r`n" }
        }
    }
    [IO.File]::WriteAllText($localConfig, $content, (New-Object Text.UTF8Encoding $false))
    Ok "claude: $claude"
    Ok "written to config\local.yaml"
} else {
    Warn "claude.exe not found. Install Claude Code and log in (claude), then rerun setup. Scoring, details and research need it."
}

# 5. Folders and database ----------------------------------------------------------
Step "Folders and database"
foreach ($dir in @("data", "logs", "secrets")) {
    $path = Join-Path $root $dir
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path | Out-Null; Ok "created $dir\" }
}
foreach ($name in @("profile.yaml", "preferred_companies.yaml")) {
    $target = Join-Path $root "profile\$name"
    $example = Join-Path $root ("profile\" + ($name -replace '\.yaml$', '.example.yaml'))
    if (-not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $example)) {
        Copy-Item -LiteralPath $example -Destination $target
        Ok "created profile\$name from the example (edit it before the first run)"
    }
}
& $venvPython -m agents.db init
if ($LASTEXITCODE -ne 0) { Fail "database init failed" }
Ok "migrations applied (existing data kept)"

# 6. Tests ---------------------------------------------------------------------------
if ($SkipTests) {
    Step "Tests skipped (-SkipTests)"
} else {
    Step "Running the tests"
    & $venvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { Warn "some tests failed (see above)" } else { Ok "all tests passed" }
}

# 7. Next steps ----------------------------------------------------------------------
Step "Next steps"
$n = 1
if (-not (Test-Path -LiteralPath (Join-Path $root "profile\profile.yaml"))) {
    Write-Host "  $n. Copy your profile\profile.yaml here (it's gitignored)."; $n++
} else {
    Write-Host "  $n. Edit profile\profile.yaml and profile\preferred_companies.yaml (both gitignored)."; $n++
}
if (-not (Test-Path -LiteralPath (Join-Path $root "secrets\credentials.json"))) {
    Write-Host "  $n. Put the Gmail OAuth client in secrets\credentials.json."; $n++
}
if (-not (Test-Path -LiteralPath (Join-Path $root "secrets\token.json"))) {
    Write-Host "  $n. Log in to Gmail once (opens a browser, read-only scope):"
    Write-Host "       .venv\Scripts\python.exe -m agents.common.gmail auth"; $n++
}
if (-not $claude) { Write-Host "  $n. Install Claude Code, log in, and rerun this script."; $n++ }
Write-Host "  $n. Check Gmail:   .\run.ps1 gmail-check"; $n++
Write-Host "  $n. First run:     .\run.ps1 daily"; $n++
Write-Host "  $n. All commands:  .\run.ps1 help"

Pop-Location
if ($problems.Count -gt 0) {
    Write-Host ""
    Write-Host "Setup finished with $($problems.Count) warning(s):" -ForegroundColor Yellow
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    exit 2
}
Write-Host ""
Write-Host "Setup finished." -ForegroundColor Green
exit 0
