# Attest

Portable, signed proof that work shipped — AI-assisted or not — against clear
acceptance criteria.

Freelancers close fixed-price projects through a criteria → delivery → e-sign
flow; every signed attestation builds a public, portable **Capability Record**.

## Stack

- Django 6 + SQLite (dev) / Postgres via `DATABASE_URL` (prod)
- Django templates + HTMX (no JS build pipeline)
- Email magic links (freelancers) + signed expiring tokens (clients)
- SHA-256 hash of canonical attestation JSON; signed rows are append-only
- Stripe / AI drafting / S3 evidence behind interfaces (stubbed until keys exist)

## Development (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py runserver
```

Run tests (the canonical verification command):

```powershell
.\.venv\Scripts\python manage.py test
```

Lint/format:

```powershell
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format .
```

## Environment variables

| Variable | Purpose | Dev default |
|---|---|---|
| `DJANGO_SECRET_KEY` | Session/signing key | insecure dev key |
| `DJANGO_DEBUG` | `1`/`0` | `1` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts | empty |
| `DATABASE_URL` | `postgres://user:pass@host:port/name` | SQLite |
| `EMAIL_HOST` (+ PORT/USER/PASSWORD/USE_TLS) | SMTP | console backend |
| `DEFAULT_FROM_EMAIL` | From address | `Attest <noreply@attest.local>` |

## Apps

- `ledger/` — trust objects and services: Project lifecycle, AcceptanceItem,
  ChangeOrder, Attestation (hash + sign), CapabilityTag. Source of truth.
- `surface/` — views, templates, urls, forms, magic-link auth, client token
  access, integration call sites (Stripe/AI/S3).

Agent workflow rules live in `.cursor/rules/`; the build plan is `PLAN.md`.
