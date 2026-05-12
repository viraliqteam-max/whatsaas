$env:DEBUG = "True"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

venv\Scripts\celery.exe `
  -A config worker `
  -Q retry_queue `
  --pool=solo `
  --hostname=retry@%h `
  --loglevel=info `
  --logfile=logs\celery_retry.log
