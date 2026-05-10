$env:DEBUG = "True"

venv\Scripts\celery.exe `
  -A config worker `
  -Q incoming_queue `
  --pool=solo `
  --loglevel=info
