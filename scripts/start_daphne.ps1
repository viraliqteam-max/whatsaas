$ErrorActionPreference = "Continue"

Set-Location "c:\Users\admin\Desktop\Backend_modular"
$env:DEBUG = "True"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

$proc = Start-Process -FilePath ".\venv\Scripts\python.exe" `
    -ArgumentList "-m", "daphne", "-b", "127.0.0.1", "-p", "8000", "config.asgi:application" `
    -WorkingDirectory "c:\Users\admin\Desktop\Backend_modular" `
    -RedirectStandardOutput "logs\daphne.log" `
    -RedirectStandardError "logs\daphne_err.log" `
    -PassThru -NoNewWindow
Write-Host "Daphne started (PID $($proc.Id)). Tailing logs\daphne_err.log..."
$proc.WaitForExit()
