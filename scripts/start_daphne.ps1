$ErrorActionPreference = "Continue"

Set-Location "c:\Users\admin\Desktop\Backend_modular"
$env:DEBUG = "True"

.\venv\Scripts\python.exe -m daphne -b 127.0.0.1 -p 8000 config.asgi:application > daphne.combined.log 2>&1
