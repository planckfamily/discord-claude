$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

& "$ScriptDir\venv\Scripts\Activate.ps1"

while ($true) {
    python "$ScriptDir\bot.py" @args
    if ($LASTEXITCODE -ne 42) {
        exit $LASTEXITCODE
    }
    Write-Host "Bot requested restart (exit code 42). Restarting..."
}
