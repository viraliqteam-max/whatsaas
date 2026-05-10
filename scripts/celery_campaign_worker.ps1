$env:DEBUG = "True"

venv\Scripts\celery.exe `
  -A config worker `
  -Q campaign_queue `
  --pool=solo `
  --loglevel=info
