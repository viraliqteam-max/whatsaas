@echo off
REM ============================================================
REM  WhatsApp Automation Backend — one-time setup script
REM  Run from the Backend folder: .\setup.bat
REM ============================================================

echo [1/5] Creating virtual environment...
python -m venv venv
call venv\Scripts\activate.bat

echo [2/5] Installing dependencies...
pip install -r requirements.txt

echo [3/5] Copying .env (edit .env with your credentials before next step)
IF NOT EXIST .env (
    copy .env.example .env
    echo .env created — EDIT IT NOW before running migrations!
    pause
)

echo [4/5] Running Django migrations...
python manage.py migrate

echo [5/5] Creating superuser (follow the prompts)...
python manage.py createsuperuser

echo.
echo ============================================================
echo  Setup complete!
echo  Start the dev server:  python manage.py runserver
echo  Start Celery worker:   powershell -ExecutionPolicy Bypass -File scripts\celery_default_worker.ps1
echo  Incoming worker:       powershell -ExecutionPolicy Bypass -File scripts\celery_incoming_worker.ps1
echo  API docs:              http://127.0.0.1:8000/api/docs/
echo ============================================================
