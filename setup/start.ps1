# Start Shadowfenced locally: run the doctor, pick a profile, launch uvicorn.
#
# Usage:
#   setup\start.ps1                  # use the doctor's recommended profile
#   setup\start.ps1 -Profile light   # force full | light | code
#
# Sets SF_JUDGE_MODEL for the full/light profiles, starts uvicorn on
# 127.0.0.1:8765, and prints the URL. Code profile needs no judge model.
param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Profiles = @("full", "light", "code")
if ($Profile -ne "" -and $Profiles -notcontains $Profile) {
    Write-Output "Unknown profile '$Profile'. Use one of: full, light, code."
    exit 1
}

# 1. Doctor first: show the machine report (never prints key values).
Write-Output "--- doctor ---"
python setup/check.py
if ($LASTEXITCODE -ne 0) {
    Write-Output "Python is too old (need >= 3.11). Stopping."
    exit 1
}

# 2. Profile: explicit -Profile wins, else the doctor's recommendation.
if ($Profile -eq "") {
    $Report = python setup/check.py --json | ConvertFrom-Json
    $Profile = $Report.verdict.profile
    Write-Output "--- profile: $Profile (doctor's recommendation) ---"
} else {
    Write-Output "--- profile: $Profile (your choice) ---"
}

# 3. Judge model for the profile.
$Judges = @{
    full  = "muse-glimmer:30b"
    light = "llama3.2:3b"
    code  = ""
}
$Judge = $Judges[$Profile]
if ($Judge -ne "") {
    $env:SF_JUDGE_MODEL = $Judge
    Write-Output "SF_JUDGE_MODEL=$Judge"
} else {
    Write-Output "Code-only profile: no judge model needed (hard cases are held)."
}

# 4. Launch.
Write-Output "Starting Shadowfenced on http://127.0.0.1:8765 ..."
Write-Output "Open that URL in your browser. Stop with Ctrl+C."
python -m uvicorn shadowfenced.app:app --app-dir src --host 127.0.0.1 --port 8765
