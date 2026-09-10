# Eversoft HRMS

A full‑stack **Human Resource Management System** — recruitment, AI‑assisted interviews,
attendance, leave, tasks, onboarding, payroll and role‑based access control — built on
Django REST Framework with a pre‑built React/Vite single‑page front end.

| Layer | Technology |
|---|---|
| Backend | Django 5.x + Django REST Framework |
| Database | MySQL (via `PyMySQL`) |
| Frontend | Pre‑built React / Vite SPA (served from `dist/`) |
| Auth | Custom OTP login + Google OAuth + JWT (no `django.contrib.auth`) |
| AI | Anthropic Claude API (interview question generation) with offline fallback |
| Email | Resend API or SMTP |
| Static serving | WhiteNoise |
| Deployment | GitHub Actions → GoDaddy cPanel (Phusion Passenger) |

---

## Features

- **Recruitment** — job posts, candidate interview links, resume scoring, question sets, social auto‑posting (LinkedIn / X).
- **AI interviews** — Claude‑generated interview questions with a local fallback when no API key is set; WebRTC live interview signalling (peer‑to‑peer, no media server).
- **Employees** — attendance (check‑in/out, breaks, remote/office switch), leave requests & balances, task tracker, work submissions.
- **Onboarding** — candidate onboarding, document collection, custom form builder.
- **Notifications** — in‑app notification centre.
- **RBAC** — modules, roles, permission groups and per‑permission enforcement, with legacy role fallback.

---

## Repository Project Layout 

```
HRMS/
├── manage.py                 # Django entry point
├── passenger_wsgi.py         # cPanel / Phusion Passenger WSGI entry point
├── requirements.txt          # Python dependencies
├── hrms_project/             # Django project package
│   ├── settings.py           # All settings (reads .env)
│   ├── urls.py               # Root URLs (api/ + SPA catch-all)
│   ├── wsgi.py / asgi.py
│   └── test_settings.py      # Settings used for the test suite
├── api/                      # Single Django app
│   ├── models.py             # All DB models
│   ├── views.py              # Main API views
│   ├── auth_views.py         # OTP / Google OAuth / password reset
│   ├── attendance_views.py   # Attendance & presence
│   ├── onboarding_views.py   # Candidate onboarding
│   ├── job_form_views.py     # Job / form builder
│   ├── live_views.py         # WebRTC signalling
│   ├── ai.py                 # Anthropic Claude integration + fallback
│   ├── mailer.py             # Resend / SMTP email
│   ├── social_poster.py      # LinkedIn + X auto-posting
│   ├── permissions.py        # RBAC decorators
│   ├── authentication.py     # JWT / header auth helpers
│   ├── serializers.py
│   ├── urls.py               # All /api/ routes
│   └── management/commands/  # seed_rbac, seed_data, seed_attendance_policies, ...
├── dist/dist/                # Pre-built React SPA (checked in, served at site root)
│   ├── index.html
│   └── assets/               # JS bundles (index-*.js + hrms-*.js modules)
└── .github/workflows/deploy.yml   # CI/CD to cPanel
```

> The front end is **pre‑built and committed** under `dist/dist/`. There is no Node build step in
> this repo — the bundles are edited/replaced directly. If you re‑introduce a React source build,
> uncomment the Node build step in `deploy.yml`.

---

## Prerequisites

- Python 3.10+
- MySQL 5.7+ / 8.x (a `hrms` database and user)
- pip / virtualenv

---

## Local Development

```bash
# 1. Clone
git clone <repo-url> HRMS
cd HRMS

# 2. Create & activate a virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create a .env file in the project root (see "Environment Variables" below)

# 5. Run migrations (creates auth/RBAC/attendance/onboarding tables)
python manage.py migrate

# 6. Seed default roles, permissions and modules
python manage.py seed_rbac

# 7. Start the dev server (serves the API and the React SPA together)
python manage.py runserver 0.0.0.0:8000
```

The app is then available at <http://localhost:8000> and the API under `http://localhost:8000/api/`.

> **Email must be configured** (Resend API key or SMTP) before OTP login will work, since login
> sends a one‑time code by email.

---

## Environment Variables

Create a `.env` file in the project root. It is git‑ignored — **never commit real secrets.**

| Variable | Description |
|---|---|
| `DJANGO_SECRET_KEY` | Django secret key |
| `DJANGO_DEBUG` | `true` / `false` |
| `DJANGO_ALLOWED_HOSTS` | Comma‑separated hostnames |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | MySQL connection |
| `ANTHROPIC_API_KEY` | Claude API key (AI question generation) |
| `ANTHROPIC_MODEL` | Generation model override (default `claude-sonnet-4-5`) |
| `ANTHROPIC_VALIDATION_MODEL` | Validation model override (default `claude-haiku-4-5`) |
| `RESEND_API_KEY` / `RESEND_FROM_EMAIL` / `RESEND_FROM_NAME` | Resend email API (preferred) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | SMTP fallback |
| `REACT_BUILD_DIR` | Override path to the built SPA (default `dist/dist`) |

---

## Management Commands

```bash
python manage.py seed_rbac              # Seed modules, roles, permission groups, permissions
python manage.py seed_data              # Seed demo data
python manage.py seed_attendance_policies  # Seed default attendance policies
python manage.py cleanup_demo_data      # Remove demo data
python manage.py list_notifications <email>  # List a user's notifications
```

---

## API Overview

All endpoints live under the `/api/` prefix and use **no trailing slashes** (`APPEND_SLASH = False`).
Identity is passed via the `X-User-Email` (or `X-Actor-Email`) request header; RBAC is enforced by
decorators in `api/permissions.py`. Route groups:

| Group | Prefix | Source |
|---|---|---|
| Auth (OTP, Google, password reset) | `/api/auth/…` | `auth_views.py` |
| Recruitment (jobs, interviews, resume scores, recordings) | `/api/jobs`, `/api/interviews`, … | `views.py` |
| AI | `/api/ai/…` | `ai.py` + `views.py` |
| Attendance / Leave / Tasks / Submissions | `/api/attendance`, `/api/leave`, `/api/tasks`, `/api/submissions` | `attendance_views.py`, `views.py` |
| Onboarding | `/api/onboarding/…` | `onboarding_views.py` |
| Notifications | `/api/notifications/…` | `views.py` |
| RBAC (roles, permissions, modules, companies) | `/api/roles`, `/api/permissions`, `/api/rbac/…` | `views.py` |
| WebRTC live interview | `/api/live/…` | `live_views.py` |
| Utility | `/api/config`, `/api/health` | `views.py` |

See `api/urls.py` for the authoritative, complete list of routes.

### Candidate sourcing MVP

The sourcing API ranks resumes already stored in HRMS against an existing job
or a supplied job description. It extracts common skills and minimum experience,
stores an explainable score for every result, and supports recruiter feedback and
shortlisting. Currently `internal` is the only enabled source; external job-board
connectors require provider-approved API access.

```http
POST /api/sourcing/searches
X-User-Email: recruiter@example.com
Content-Type: application/json

{"jobId": 42, "sources": ["internal"]}
```

Other endpoints are `GET /api/sourcing/searches`,
`GET /api/sourcing/searches/<id>`, `GET /api/sourcing/searches/<id>/results`,
`POST /api/sourcing/results/<id>/shortlist`, and
`POST /api/sourcing/results/<id>/feedback`.

---

## Deployment (GoDaddy cPanel + GitHub Actions)

Pushing to `master` triggers `.github/workflows/deploy.yml`, which:

1. Rsyncs the repository to the cPanel server over SSH (excluding `.git`, `.github`, `.venv`,
   `node_modules`, `db.sqlite3`, and `.env`).
2. Activates the cPanel Python virtualenv and runs:
   ```bash
   pip install -r requirements.txt
   python manage.py migrate --noinput
   python manage.py collectstatic --noinput
   touch tmp/restart.txt      # restart Phusion Passenger
   ```

`passenger_wsgi.py` is the Passenger entry point (sets `DJANGO_SETTINGS_MODULE=hrms_project.settings`).

### Required GitHub Actions secrets

| Secret | Purpose |
|---|---|
| `SSH_PRIVATE_KEY` | Deploy key for the cPanel host |
| `REMOTE_HOST` | Server hostname / IP |
| `REMOTE_USER` | cPanel SSH user |
| `REMOTE_PORT` | SSH port (defaults to `22`) |
| `REMOTE_TARGET_DIR` | App directory under the user's home |

### First‑time production setup

After the first deploy, seed RBAC once on the server:

```bash
python manage.py seed_rbac
```

---

## Security Notes

- **Environment secrets** live only in `.env` (git‑ignored) and GitHub Actions secrets. Do not commit them.
- The app stores `app_users.password` in plain text to match the legacy admin "show password"
  behaviour — treat the database as sensitive and restrict access accordingly.
- If any private key was ever committed to this repository's history, **rotate it** and purge it
  from history; removing the file from the working tree does not remove it from past commits.

---

## Key Conventions

- No `django.contrib.auth` — `AppUser` (the `app_users` table) is the only user model; identity comes from the `X-User-Email` header.
- `USE_TZ = False` — datetimes are naive.
- Large uploads are allowed (base64 video / documents) — see `FILE_UPLOAD_MAX_MEMORY_SIZE` in `settings.py`.
- RBAC is additive: legacy `role` strings (`admin`/`hr`/`recruitment`) map to `Super Admin`/`HR Manager`/`HR Executive` when no `role_ref` is set.
