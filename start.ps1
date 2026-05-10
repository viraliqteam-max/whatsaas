# Start Daphne — kills any stale process on port 8000 first.
# Both the UI (frontend/index.html served at /) and WebSocket connections
# are handled by Daphne on the same port via config/asgi.py ProtocolTypeRouter.

$PORT = 8000
$BIND = "127.0.0.1"

# Kill whatever is holding the port
$pids = (netstat -ano | Select-String ":$PORT\s.*LISTENING") -replace '.*\s+(\d+)$','$1' | Select-Object -Unique
foreach ($p in $pids) {
    if ($p -match '^\d+$' -and [int]$p -ne 0) {
        Write-Host "Killing PID $p on port $PORT..."
        taskkill /PID $p /F | Out-Null
        Start-Sleep -Milliseconds 500
    }
}

# Activate venv if present
if (Test-Path ".\venv\Scripts\Activate.ps1") {
    . .\venv\Scripts\Activate.ps1
}

Write-Host "Starting Daphne on ${BIND}:${PORT}  (UI + WebSocket on same port)"
daphne -b $BIND -p $PORT config.asgi:application
