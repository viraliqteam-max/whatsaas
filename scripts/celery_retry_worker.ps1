$env:DEBUG = "True"

venv\Scripts\celery.exe `
  -A config worker `
  -Q retry_queue `
  --pool=solo `
  --loglevel=info
