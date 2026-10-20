# Start the bot: API server + public tunnel.
#
#   .\start.ps1
#
# Set NGROK_DOMAIN in .env to your free static domain from
# https://dashboard.ngrok.com/domains  — then the public URL never changes
# and you never re-paste it into Meta again. Without it you get a random
# URL each run and Meta's webhook must be updated every time.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- read .env (KEY=VALUE, ignore comments) --------------------------------
$env_vars = @{}
if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $env_vars[$matches[1]] = $matches[2].Trim()
        }
    }
}
$domain = $env_vars["NGROK_DOMAIN"]

# --- Postgres --------------------------------------------------------------
Write-Host "checking postgres..." -NoNewline
# No 2>&1 here: in PowerShell 5.1 redirecting a native command's stderr wraps
# each line in an ErrorRecord, which $ErrorActionPreference='Stop' then throws
# on — even when docker exited 0.
$ErrorActionPreference = "Continue"
docker compose up -d | Out-Null
$dockerExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($dockerExit -ne 0) {
    Write-Host " FAILED - is Docker Desktop running?" -ForegroundColor Red
    exit 1
}
Write-Host " ok"

# --- API server ------------------------------------------------------------
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'uvicorn' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2
# Keep one generation. Start-Process TRUNCATES its redirect target, so
# without this a restart destroys the evidence of why the last run died —
# and a crash is exactly when you restart. server.prev.log already existed
# as a filename; nothing had ever written it.
foreach ($f in @("server.log", "server.err.log")) {
    if (Test-Path $f) {
        Move-Item $f ($f -replace '\.log$', '.prev.log') -Force
    }
}
Write-Host "starting api on :8000..." -NoNewline
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "-m","uvicorn","app:app","--port","8000","--log-level","warning" `
    -RedirectStandardOutput "server.log" -RedirectStandardError "server.err.log" `
    -WindowStyle Hidden

$ok = $false
foreach ($i in 1..20) {
    Start-Sleep -Seconds 2
    try {
        $h = Invoke-WebRequest http://127.0.0.1:8000/health -TimeoutSec 4 -UseBasicParsing
        if ($h.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if (-not $ok) {
    Write-Host " FAILED - see server.err.log" -ForegroundColor Red
    Get-Content server.err.log -Tail 15 -ErrorAction SilentlyContinue
    exit 1
}
Write-Host " ok"
Write-Host "  $($h.Content)" -ForegroundColor DarkGray

# --- tunnel ----------------------------------------------------------------
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
$ngrok_args = if ($domain) { @("http", "--url=$domain", "8000") } else { @("http", "8000") }
# winget puts ngrok on PATH, but a shell opened before the install still has
# the old PATH — so resolve the exe rather than trusting the name.
$ngrok_exe = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrok_exe) {
    $ngrok_exe = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ngrok.exe"
}
if (-not (Test-Path $ngrok_exe)) {
    Write-Host " ngrok not found - install with: winget install ngrok.ngrok" -ForegroundColor Red
    exit 1
}
Write-Host "starting tunnel..." -NoNewline
Start-Process -FilePath $ngrok_exe -ArgumentList $ngrok_args -WindowStyle Minimized
Start-Sleep -Seconds 6

try {
    $t = (Invoke-WebRequest http://127.0.0.1:4040/api/tunnels -TimeoutSec 8 -UseBasicParsing).Content | ConvertFrom-Json
    $url = $t.tunnels[0].public_url
    Write-Host " ok"
    Write-Host ""
    Write-Host "  WEBHOOK URL:  $url/webhook/whatsapp" -ForegroundColor Green
    if (-not $domain) {
        Write-Host "  (random URL - claim a free static domain and set NGROK_DOMAIN in .env" -ForegroundColor Yellow
        Write-Host "   so you stop re-pasting this into Meta every restart)" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "  logs:  Get-Content server.log -Wait -Tail 20"
} catch {
    Write-Host " tunnel started but its API is not answering yet - check the ngrok window"
}
