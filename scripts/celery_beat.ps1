$env:DEBUG = "True"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

venv\Scripts\celery.exe `
  -A config beat `
  --loglevel=info `
  --logfile=logs\celery_beat.log
