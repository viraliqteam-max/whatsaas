$env:DEBUG = "True"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

venv\Scripts\celery.exe `
  -A config worker `
  -Q default `
  --pool=solo `
  --hostname=default@%h `
  --loglevel=info `
  --logfile=logs\celery_default.log
