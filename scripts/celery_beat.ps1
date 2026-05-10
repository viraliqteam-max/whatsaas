$env:DEBUG = "True"

venv\Scripts\celery.exe `
  -A config beat `
  --loglevel=info
