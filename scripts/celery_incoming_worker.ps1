$env:DEBUG = "True"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

venv\Scripts\celery.exe `
  -A config worker `
  -Q incoming_queue `
  --pool=solo `
  --hostname=incoming@%h `
  --loglevel=info `
  --logfile=logs\celery_incoming.log
