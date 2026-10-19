<#
.SYNOPSIS
    Pack the project into job-agent-bundle-YYYYMMDD.zip on the Desktop, for moving it to
    another Windows machine.

.DESCRIPTION
    Includes the whole project with its git history (.git), the database (data/jobs.db, as
    a consistent snapshot taken with SQLite's backup API), the Gmail OAuth client
    (secrets/credentials.json), and the gitignored personal files (profile.yaml, briefs,
    digests).

    Excludes: .venv, logs, .pytest_cache, every __pycache__, secrets/token.json (log in
    to Gmail again on the new machine), config/local.yaml (machine-specific; setup.ps1
    rewrites it), and .env / .env.* files.

    The zip holds personal data and the OAuth client secret: keep it private.
    On the new machine: unzip, then run scripts\setup.ps1 from the job-agent folder.

.PARAMETER OutDir
    Where to write the zip (default: your Desktop).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\export_bundle.ps1
#>
param(
    [string]$OutDir = [Environment]::GetFolderPath("Desktop")
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$zipPath = Join-Path $OutDir ("job-agent-bundle-{0}.zip" -f (Get-Date -Format "yyyyMMdd"))
$top = "job-agent"  # the folder the zip unpacks into

# Excluded paths, relative to the project root, with forward slashes.
$excludedDirs = @(".venv", "logs", ".pytest_cache", "data/backups")
$excludedDirNames = @("__pycache__")
$excludedFiles = @("secrets/token.json", "config/local.yaml", "data/jobs.db")  # the db is added as a snapshot
$excludedFilePatterns = @("^\.env$", "^\.env\..+", "/\.env$", "/\.env\.[^/]+$", "^data/.+\.db-(wal|shm|journal)$")

function Get-RelativePath([string]$path) {
    return $path.Substring($root.Length).TrimStart('\', '/').Replace('\', '/')
}

function Get-BundleFiles([string]$dir) {
    foreach ($item in Get-ChildItem -LiteralPath $dir -Force) {
        $rel = Get-RelativePath $item.FullName
        if ($item.PSIsContainer) {
            if ($excludedDirs -contains $rel -or $excludedDirNames -contains $item.Name) { continue }
            Get-BundleFiles $item.FullName
        } else {
            if ($excludedFiles -contains $rel) { continue }
            $skip = $false
            foreach ($pattern in $excludedFilePatterns) { if ($rel -match $pattern) { $skip = $true; break } }
            if (-not $skip) { $item }
        }
    }
}

Push-Location -LiteralPath $root
try {
    $dirty = & git status --porcelain
    if ($dirty) { Write-Host "Note: uncommitted changes are included as files, not in git history:" -ForegroundColor Yellow; $dirty | ForEach-Object { "  $_" } }

    # A consistent copy of the database even if something has it open.
    $snapshot = $null
    $db = Join-Path $root "data\jobs.db"
    if (Test-Path -LiteralPath $db) {
        $snapshot = Join-Path ([IO.Path]::GetTempPath()) ("jobs-snapshot-{0}.db" -f [guid]::NewGuid())
        $venvPython = Join-Path $root ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $venvPython) {
            $code = "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()"
            & $venvPython -c $code $db $snapshot
            if ($LASTEXITCODE -ne 0) { throw "database snapshot failed" }
        } else {
            Copy-Item -LiteralPath $db -Destination $snapshot
        }
    } else {
        Write-Host "Note: data\jobs.db not found; the bundle has no database." -ForegroundColor Yellow
    }

    $files = @(Get-BundleFiles $root)
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    $count = 0
    try {
        foreach ($file in $files) {
            $entry = "$top/" + (Get-RelativePath $file.FullName)
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $file.FullName, $entry, [System.IO.Compression.CompressionLevel]::Optimal)
            $count++
        }
        if ($snapshot) {
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $snapshot, "$top/data/jobs.db", [System.IO.Compression.CompressionLevel]::Optimal)
            $count++
        }
    } finally {
        $zip.Dispose()
        if ($snapshot -and (Test-Path -LiteralPath $snapshot)) { Remove-Item -LiteralPath $snapshot -Force }
    }

    $size = (Get-Item -LiteralPath $zipPath).Length
    $credentials = Test-Path -LiteralPath (Join-Path $root "secrets\credentials.json")
    Write-Host ""
    Write-Host "Bundle: $zipPath"
    Write-Host ("Size:   {0:N1} MB ({1:N0} bytes)" -f ($size / 1MB), $size)
    Write-Host "Files:  $count"
    Write-Host ("Has:    .git history, data/jobs.db: {0}, secrets/credentials.json: {1}" -f [bool]$snapshot, $credentials)
    Write-Host "Left out: .venv, logs, .pytest_cache, __pycache__, secrets/token.json, config/local.yaml, .env"
    Write-Host ""
    Write-Host "Keep it private: it holds your profile, job data and the Gmail OAuth client secret." -ForegroundColor Yellow
    Write-Host "On the new machine: unzip, cd job-agent, then: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
} finally {
    Pop-Location
}
