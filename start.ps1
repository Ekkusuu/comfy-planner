# One command: install deps (uv), build the UI if it isn't built yet, run the app on 127.0.0.1:8765, open the browser.
Set-Location $PSScriptRoot
uv sync
if (-not (Test-Path "ui/dist")) {
    Write-Host "Building the UI (first run)..."
    Push-Location ui
    npm ci
    if ($LASTEXITCODE -eq 0) { npm run build }
    Pop-Location
}
$port = 8765
$page = if (Test-Path "ui/dist") { "" } else { "docs" }   # no UI (build failed): open the API docs instead
Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 3; Start-Process $u } -ArgumentList "http://127.0.0.1:$port/$page" | Out-Null
uv run uvicorn app.main:app --host 127.0.0.1 --port $port
