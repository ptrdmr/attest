# Attest

Portable, signed proof that work shipped — AI-assisted or not — against clear
acceptance criteria.

Freelancers close fixed-price projects through a criteria → delivery → e-sign
flow; every signed attestation builds a portable **Capability Record**.

The Capability Record is private by default and published only when the
freelancer explicitly opts in. Until then it 404s for everyone but its owner,
who can preview it from `/record/`, and it carries `X-Robots-Tag: noindex`.

A published record never shows a signed attestation as a clean sweep when it
was not one. Acceptance criteria that were parked or withdrawn before signing
are disclosed on the record as a "Scope adjusted" badge and a count, so a
reader can tell that the delivered scope differs from the scope agreed.

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
.\.venv\Scripts\python manage.py createcachetable
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

Development works out of the box via `manage.py` (it defaults `DJANGO_DEBUG=1`).
Production serving (WSGI/ASGI) is fail-closed: set `DJANGO_SECRET_KEY` and
`DJANGO_ALLOWED_HOSTS`; leave `DJANGO_DEBUG` unset or set `0`. Optionally set
`DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated HTTPS origins) when using a
reverse proxy or non-default host.

The cache is database-backed in every environment, because single-use magic
links and per-email rate limits must hold across worker processes. Run
`manage.py createcachetable` once per database — including a newly provisioned
production database, and any existing local database created before the cache
moved to this backend — or those code paths fail on a missing table.

Single-use link consumption is safe under concurrency: the cache table keys on
a primary key, so a racing second use loses the insert and is rejected. The
rate-limit counter uses read-modify-write and is therefore approximate under
heavy concurrency — it bounds abuse globally rather than enforcing an exact
ceiling.

| Variable | Purpose | Dev default (`manage.py`) |
|---|---|---|
| `DJANGO_SECRET_KEY` | Session/signing key | insecure dev key when `DEBUG=1` |
| `DJANGO_DEBUG` | `1`/`0` | `1` via `manage.py`; unset/`0` under WSGI |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts | empty (required when `DEBUG=0`) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated HTTPS origins | empty |
| `DATABASE_URL` | `postgres://user:pass@host:port/name` | SQLite |
| `EMAIL_HOST` (+ PORT/USER/PASSWORD/USE_TLS) | SMTP | console backend |
| `DEFAULT_FROM_EMAIL` | From address | `Attest <noreply@attest.local>` |

## Apps

- `ledger/` — trust objects and services: Project lifecycle, AcceptanceItem,
  ChangeOrder, Attestation (hash + sign), CapabilityTag. Source of truth.
- `surface/` — views, templates, urls, forms, magic-link auth, client token
  access, integration call sites (Stripe/AI/S3).

Agent workflow rules live in `.cursor/rules/`; the build plan is `PLAN.md`, the
feature sequence is `ROADMAP.md`, and the go-to-market plan with its success and
failure criteria is `GO_TO_MARKET.md`.
