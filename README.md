# WhatsApp Automation Backend — Django REST Framework

A production-ready REST API to run **multiple GoLogin browser profiles**, automate **WhatsApp Web messaging**, manage **contacts & campaigns**, with a **PostgreSQL** database and **Celery** async task queue.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│              Django REST Framework API            │
│                                                  │
│  /api/profiles/   GoLogin profile CRUD + launch  │
│  /api/sessions/   WhatsApp session + QR code     │
│  /api/contacts/   Contact & group management     │
│  /api/messaging/  Templates, campaigns, logs     │
└──────────────┬───────────────────────────────────┘
               │
       ┌───────┴────────┐
       │                │
  GoLogin API      PostgreSQL DB
  (remote Chrome)
       │
  Selenium WebDriver
       │
  WhatsApp Web (web.whatsapp.com)
```

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.11+ |
| PostgreSQL | 14+ |
| Redis | 6+ (for Celery) |
| Chrome | latest |
| GoLogin account | (for real browser profiles) |

---

## Quick Start

### 1. Clone / enter the project directory

```powershell
cd C:\Users\admin\Desktop\Backend
```

### 2. Create & activate virtual environment

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment

Edit `.env` with your credentials:

```ini
SECRET_KEY=your-secret-key
DB_NAME=whatsapp_automation
DB_USER=postgres
DB_PASSWORD=postgres
DB_HOST=localhost
DB_PORT=5432
GOLOGIN_API_TOKEN=your-token-from-gologin-dashboard
CELERY_BROKER_URL=redis://localhost:6379/0
```

### 4. Create PostgreSQL database

```sql
CREATE DATABASE whatsapp_automation;
```

### 5. Run migrations & create superuser

```powershell
python manage.py migrate
python manage.py createsuperuser
```

### 6. Start services

**Terminal 1 — Django dev server:**
```powershell
python manage.py runserver
```

**Terminal 2 — Celery worker (Windows):**
```powershell
.\scripts\celery_default_worker.ps1
```

For the dedicated incoming-message queue, run:

```powershell
.\scripts\celery_incoming_worker.ps1
```

If you start Celery manually on Windows, always include `--pool=solo`. Without
that flag Celery uses `prefork`, which can fail on Windows with
`PermissionError: [WinError 5] Access is denied` from billiard multiprocessing.

```powershell
venv\Scripts\celery.exe -A config worker -Q incoming_queue --pool=solo --loglevel=info -n incoming@%h
```

---

## API Workflow (Postman)

Import `postman_collection.json` into Postman. Then follow this order:

### Step 1 — Authenticate

```
POST /api/auth/token/
Body: { "username": "admin", "password": "yourpassword" }
→ Save the "access" token → set as Bearer Token
```

### Step 2 — Create a GoLogin Profile

```
POST /api/profiles/
Body: { "name": "Profile 1", "os_type": "win", "sync_with_gologin": true }
→ Get profile ID
```

> Set `sync_with_gologin: false` if you don't have a GoLogin token yet
> (profile will be saved locally only, browser features won't work)

### Step 3 — Launch the Browser

```
POST /api/profiles/{id}/launch/
→ This starts the GoLogin Chrome profile (or local headless Chrome)
```

### Step 4 — Link WhatsApp Session

```
POST /api/sessions/
Body: { "profile": <profile_id> }

POST /api/sessions/{id}/check_status/
→ Returns status + qr_code_base64 if login is needed
→ Decode the base64 image and scan it with your phone
```

### Step 5 — Send a Direct Message

```
POST /api/messaging/send/
Body: {
  "profile_id": <profile_id>,
  "phone_number": "12025550123",
  "message": "Hello from the API!"
}
```

### Step 6 — Run a Campaign

```
# Create contacts
POST /api/contacts/bulk_import/
Body: { "contacts": [{"name": "Alice", "phone_number": "14155550100"}, ...] }

# Create campaign
POST /api/messaging/campaigns/
Body: {
  "name": "My Campaign",
  "profile": <profile_id>,
  "custom_message": "Hi {name}!",
  "target_contacts": [1, 2, 3],
  "delay_seconds": 10
}

# Run synchronously (good for Postman testing)
POST /api/messaging/campaigns/{id}/start_sync/

# OR run async via Celery (production)
POST /api/messaging/campaigns/{id}/start/

# Monitor
GET /api/messaging/campaigns/{id}/stats/
GET /api/messaging/campaigns/{id}/logs/
```

---

## Complete API Reference

### Auth
| Method | URL | Description |
|--------|-----|-------------|
| POST | `/api/auth/token/` | Get JWT access + refresh tokens |
| POST | `/api/auth/token/refresh/` | Refresh access token |

### GoLogin Profiles (`/api/profiles/`)
| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/profiles/` | List your profiles |
| POST | `/api/profiles/` | Create profile (optionally sync to GoLogin) |
| GET | `/api/profiles/{id}/` | Retrieve profile |
| PUT | `/api/profiles/{id}/` | Update profile |
| DELETE | `/api/profiles/{id}/` | Delete profile (+ GoLogin remote) |
| POST | `/api/profiles/{id}/launch/` | Start browser for this profile |
| POST | `/api/profiles/{id}/stop/` | Stop browser |
| GET | `/api/profiles/{id}/driver_status/` | Is the Selenium driver alive? |
| GET | `/api/profiles/active/` | All profiles with active drivers |
| GET | `/api/profiles/gologin_list/` | Raw list from GoLogin API |

### WhatsApp Sessions (`/api/sessions/`)
| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/sessions/` | List sessions |
| POST | `/api/sessions/` | Create session for a profile |
| GET | `/api/sessions/{id}/` | Retrieve session |
| POST | `/api/sessions/{id}/check_status/` | Check login status + return QR |
| POST | `/api/sessions/{id}/screenshot/` | Screenshot current browser state |

### Contacts (`/api/contacts/`)
| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/contacts/` | List contacts (?search=name&tags=vip) |
| POST | `/api/contacts/` | Create contact |
| GET | `/api/contacts/{id}/` | Retrieve contact |
| PUT | `/api/contacts/{id}/` | Update contact |
| DELETE | `/api/contacts/{id}/` | Delete contact |
| POST | `/api/contacts/bulk_import/` | Import list of contacts |
| GET | `/api/contacts/groups/` | List contact groups |
| POST | `/api/contacts/groups/` | Create group |
| POST | `/api/contacts/groups/{id}/add_contacts/` | Add contacts to group |
| POST | `/api/contacts/groups/{id}/remove_contacts/` | Remove contacts from group |

### Messaging (`/api/messaging/`)
| Method | URL | Description |
|--------|-----|-------------|
| POST | `/api/messaging/send/` | Send single message instantly |
| GET | `/api/messaging/templates/` | List templates |
| POST | `/api/messaging/templates/` | Create template with {name} placeholders |
| POST | `/api/messaging/templates/{id}/preview/` | Render template with sample data |
| GET | `/api/messaging/campaigns/` | List campaigns |
| POST | `/api/messaging/campaigns/` | Create campaign |
| GET | `/api/messaging/campaigns/{id}/` | Retrieve campaign |
| POST | `/api/messaging/campaigns/{id}/start/` | Launch async (Celery) |
| POST | `/api/messaging/campaigns/{id}/start_sync/` | Launch synchronously |
| POST | `/api/messaging/campaigns/{id}/pause/` | Pause campaign |
| GET | `/api/messaging/campaigns/{id}/stats/` | Sent/failed/pending counts |
| GET | `/api/messaging/campaigns/{id}/logs/` | Message logs for campaign |
| GET | `/api/messaging/logs/` | All message logs (?status=sent) |

### API Docs (Swagger UI)
- `GET /api/docs/` — Interactive Swagger UI
- `GET /api/redoc/` — ReDoc
- `GET /api/schema/` — OpenAPI schema

---

## Project Structure

```
Backend/
├── manage.py
├── requirements.txt
├── .env                         ← your credentials (gitignored)
├── postman_collection.json      ← import into Postman
├── config/
│   ├── settings.py              ← Django config
│   ├── celery.py                ← Celery app
│   └── urls.py                  ← root URL routing
├── apps/
│   ├── profiles/                ← GoLogin profile management
│   │   ├── models.py
│   │   ├── views.py             ← launch/stop/CRUD
│   │   └── urls.py
│   ├── sessions/                ← WhatsApp Web session
│   │   ├── models.py
│   │   ├── views.py             ← QR code, status check
│   │   └── urls.py
│   ├── messaging/               ← Templates, campaigns, logs
│   │   ├── models.py
│   │   ├── views.py             ← send, campaign run
│   │   ├── tasks.py             ← Celery async tasks
│   │   └── urls.py
│   └── contacts/                ← Contact & group management
│       ├── models.py
│       ├── views.py
│       └── urls.py
└── utils/
    ├── gologin_manager.py       ← GoLogin API + Selenium driver pool
    └── whatsapp_automation.py   ← WhatsApp Web Selenium helpers
```

---

## Notes

- **QR Code:** The first time a profile connects to WhatsApp, `check_status` returns `qr_required` + a base64 PNG. Decode it and scan with your phone. After that, the session is stored in the GoLogin profile cookies and won't need re-scanning.
- **Delay between messages:** The `delay_seconds` field on campaigns is important — too fast will get your number banned by WhatsApp.
- **Multiple profiles:** Each GoLogin profile maps to a separate WhatsApp account. Profiles are isolated (different fingerprints, cookies, IPs via proxy).
- **Without GoLogin token:** Set `sync_with_gologin: false` when creating profiles. The system will use a local headless Chrome instead.
- **Admin panel:** `http://127.0.0.1:8000/admin/`
