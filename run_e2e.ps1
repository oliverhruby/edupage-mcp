<#
.LOCAL-ONLY: this script runs the live integration suite against real EduPage
schools and writes a report containing real student data to ./reports (and
optionally validates identities from tests/e2e/.local.e2e.json). It must never
run in a public CI or on a machine whose output could be published.

Requirements: EDUPAGE_USERNAME, EDUPAGE_PASSWORD, EDUPAGE_SUBDOMAINS set in the
current shell. EDUPAGE_SUBDOMAINS should be 'zssturovamalacky,iprskola'.
cvcmalacky is out of scope and is automatically stripped.
#>
$ErrorActionPreference = "Stop"

$Required = @("EDUPAGE_USERNAME", "EDUPAGE_PASSWORD", "EDUPAGE_SUBDOMAINS")
$Missing = @($Required | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })
if ($Missing) {
    Write-Error "Missing env var(s): $($Missing -join ', '). Set them (e.g. from .env) then re-run."
    exit 1
}

$Subs = @($env:EDUPAGE_SUBDOMAINS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($Subs -contains "cvcmalacky") {
    Write-Warning "cvcmalacky is out of scope for the live suite; stripping it from EDUPAGE_SUBDOMAINS."
    $env:EDUPAGE_SUBDOMAINS = ($Subs | Where-Object { $_ -ne "cvcmalacky" }) -join ","
}

$env:EDUPAGE_E2E = "1"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
if (-not (& $Py -m pytest --version 2>$null)) {
    Write-Error "pytest not available for $Py. Install it (e.g. python -m pip install pytest)."
    exit 1
}

& $Py -m pytest -m e2e -v tests/e2e
$Code = $LASTEXITCODE
Write-Host ""
Write-Host "e2e report: $Root\reports\e2e-report.json  (gitignored - contains real student data)"
exit $Code