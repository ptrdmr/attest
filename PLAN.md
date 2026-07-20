# Attest MVP — Canonical Work Order

Product plan: signed proof of shipped work for freelancers; public Capability
Record is the moat. This file is the charter-mandated plan of record: owning
departments, consults, role sequences, boundaries, and test strategy per
milestone. The orchestrator maintains it as work progresses.

Approved dependencies: Django (core). Stripe SDK / AI client / boto3 enter only
when real integrations are wired, each requiring explicit approval.

## Milestones

### M0 — Foundation (orchestrator, fast path) — DONE when committed
- git root in this folder, venv, Django 6 scaffold (`config`, `ledger`, `surface`)
- Env-driven settings (SQLite dev / `DATABASE_URL` Postgres), console email
- Tooling: uv + ruff (dev deps), .gitignore, README, this plan
- Boundaries: repo root config files, `config/`, empty app scaffolds
- Tests: `manage.py check` + empty suite green

### M1 — Ledger core (owning: Ledger; consults: none; hazard: signing/hash)
- Role sequence: Implementer (Sol — hazard zone) → Implementer-adversary
  (Sonnet) → Verifier (Composer) → Verifier-adversary (Grok) → Gate → commit
- Models: Profile, Project (status machine: draft → criteria_pending → active →
  delivered → attested / disputed), AcceptanceItem, ChangeOrder, Attestation
  (payload_hash, client_email, signed_at, signature_meta), CapabilityTag
- Services: status transitions, canonical-JSON + SHA-256 hashing, sign_attestation
  (append-only enforcement), capability-record derivation
- Boundaries: `ledger/**` only. Schema authorized: initial migration for the
  models above
- Tests: status-machine guards, hash round-trip, immutability enforcement,
  dispute freeze flag, derivation correctness

### M2 — Auth + project surface (owning: Surface; consult: Ledger read-only;
hazard: magic-link tokens)
- Role sequence: Implementer (Sol) → Implementer-adversary (Sonnet) →
  Verifier (Composer) → Verifier-adversary (Grok) → Gate → commit
- Magic-link login (email → signed token → session), client access tokens
  (signed, expiring, no account), base template + HTMX, project CRUD, criteria
  builder UI, client review/approve page
- Boundaries: `surface/**`, `templates/**`, `static/**`, `config/urls.py`
- Tests: token expiry/tamper, login flow, client approve flow, criteria editing
  locked after approval

### M3 — Delivery + attestation + public record (owning: Surface; consult:
Ledger read-only; hazard: signing flow, public record)
- Delivery checklist + evidence links, e-sign page (typed signature), public
  Capability Record at `/u/<handle>`, dispute freeze on public display
- Boundaries: `surface/**`, `templates/**`, `config/urls.py`; Ledger waiver
  ONLY if a service signature needs extension (flag to orchestrator first)
- Tests: sign flow end-to-end, hash surfaced on record, disputed attestations
  frozen/annotated, no PII/tokens in rendered output

### M4 — Change orders + billing gates
- **M4a (Ledger waiver, owning: Ledger; hazard: trust transitions):** add
  `propose_change_order`, `approve_change_order`, `decline_change_order`;
  unresolved PROPOSED change orders block `mark_delivered`. Schema: none
  (ChangeOrder model already exists). Role sequence: Sol → Sonnet → Composer
  → Grok → Gate → commit.
- **M4b (Surface, owning: Surface; consult: Ledger; hazard: billing tokens):**
  change-order UI + client token purpose `change_order`; billing gates behind
  stub provider (no Stripe SDK). Role sequence: Sol → Sonnet → Composer →
  Grok → Gate → commit.
- Tests: CO transitions + unresolved block; Surface out-of-scope flow; billing
  stub gate enforcement

### M5 — Landing + AI draft stub (owning: Surface)
- Landing page (hero: "Signed proof you shipped", one CTA, record visual),
  AI provider interface with deterministic stub: paste dump → draft brief +
  criteria → human-confirm before client exposure
- Tests: draft never client-visible pre-confirm; landing renders

### M6 — Post-MVP hardening (audit remediation)
- **M6a (owning: Surface; hazard: magic-link auth; full dispatch):** regularize
  the previously ungoverned auth WIP — query-param confirm URL, POST-confirm
  interstitial (scanner-prefetch safe), single-use tokens (sha256-keyed cache
  consumption), mandatory nonce payloads, per-email rate limit (5/hour),
  DEBUG-only QP token repair + one-click dev login, 60-min expiry + copy fix
- **M6b (owning: Ledger; hazard: attestation rows; full dispatch; Surface
  waiver: PublicRecordView/MarkDeliveredView messaging + record template):**
  Attestation delete guard, read-time payload-hash verifier on public render,
  skills snapshot into the signed payload, reject signing with failed items
- **M6c (owning: Surface, config territory; fast path):** fail-closed prod
  settings — DEBUG default 0 (manage.py keeps dev opt-in), ALLOWED_HOSTS
  guard, CSRF_TRUSTED_ORIGINS env, settings fail-closed matrix tests

## Standing rules
- Gate (fresh-context, Fable) runs on the final combined diff before EVERY commit
- Orchestrator owns git; specialists never commit
- Refit candidates and escalations logged here

## Log
- 2026-07-18: M0 executed. Django 6.0.7 (latest stable at install time; plan
  said 5.x — noted, no action needed)
- 2026-07-18: M1 executed. Builder (Sol) → adversary (Sonnet) rejected once
  (OneToOne blocked amendment chains; queryset-update bypass; admin delete
  hole) → fixed → accepted. Verifier (Composer, 47 tests) → adversary (Grok)
  rejected once (sort_keys and is_disputed filters not load-bearing) → fixed →
  accepted with mutation-testing proof. Gate: PASS
- Refit candidates: (1) make is_disputed/disputed_at admin-readonly and route
  admin dispute handling through services so tag recompute always runs;
  (2) drop redundant db_index=True on Attestation.project FK
- 2026-07-18: M2 executed. Implementer-adversary (Sonnet) rejected once (no
  tests; HTMX 400 validation paths invisible; DEBUG review_url token in HTML)
  → fixed. Verifier (Composer, 34 surface tests) → Verifier-adversary (Grok)
  rejected once (softball locked-mutation asserts) → fixed → accepted. Gate
  (Fable): PASS. Full suite 81 tests green.
- 2026-07-18: M3 executed. Builder (Sol) → Implementer-adversary (Sonnet)
  rejected once (missing Surface tests; HomeView drive-by; already-signed UX)
  → fixed. Verifier (Composer, +19 surface tests → 53) → Verifier-adversary
  (Grok) rejected once (softball purpose-isolation GET) → fixed → accepted.
  Gate (Fable): PASS.
- 2026-07-18: M4a Ledger CO services committed (24ce295). M4b Surface: Sol
  built CO UI + billing stub; Implementer-adversary rejected once (consume
  after save) → fixed; Verifier-adversary rejected once (soft CO/billing
  tests) → tightened. Gate: PASS (prior pass + no new law violations).
- 2026-07-18: M5 landing + AI draft stub. Builder (Composer) → Implementer-
  adversary ACCEPT; Verifier (+8 surface tests) → Verifier-adversary ACCEPT;
  Gate PASS (AI generate preview-only until human confirm).
- 2026-07-19: Full-stack audit (orchestrator + Ledger/Surface dept auditors).
  Verdict: MVP architecture sound, no constitution violations in committed
  code. Gaps → M6 plan: ungoverned auth WIP on disk, reusable bearer tokens,
  failed-criteria attestation allowed, skills outside signed payload, no
  read-time hash verify, fail-open DEBUG defaults. Auditor false positive
  corrected at final acceptance: bulk_update does NOT bypass the Attestation
  immutability guard (routes through QuerySet.update).
- 2026-07-19: M6a executed (524e4b4). Sol iter 1 → Implementer-adversary
  (Sonnet) REJECT: [blocker] LocMemCache is per-process so single-use +
  rate-limit guarantees are per-worker; [major] scanner prefetch consumes
  single-use tokens. Rulings: LocMem documented as accepted MVP risk —
  production REQUIRES a shared CACHES backend (logged as M7 prerequisite);
  scanner fix implemented as POST-confirm interstitial. Sol iter 2 → Sonnet
  REJECT on a suspected nonce-test flake; orchestrator root-caused it to the
  verifier's mutation (c) being live on disk during Sonnet's concurrent test
  runs — code correct, ruling: accept. Verifier-adversary (Grok) rejected
  twice across M6a/M6c (SECRET_KEY guard-masking softball; limiter test
  mocking the gate under test) → fixed by Composer → ACCEPT. Full suite 150
  green. Gate (fresh Fable): PASS 8/8.
- Process lesson (orchestrator): never schedule the mutation-testing
  verifier concurrently with another adversary that runs the suite —
  mutations bleed into their runs as phantom flakes.
- 2026-07-19: M6c executed. Builder (Composer): DJANGO_DEBUG default "0";
  manage.py setdefault keeps dev/test ergonomics while WSGI/ASGI serving
  paths fail closed (deliberate deviation from the plan's "env opt-in"
  letter to preserve the canonical test command); ALLOWED_HOSTS RuntimeError
  guard; CSRF_TRUSTED_ORIGINS env; 4 fail-closed matrix tests in
  config/tests.py. Orchestrator fast-path adversary pass + Grok verifier
  ACCEPT (after softball fix).
- 2026-07-19: M6b executed. Builder (Sol) completed all four items but its
  session died on an API usage limit before reporting; orchestrator
  recovered by verifying the finished diff directly (full suite 160 green,
  +10) instead of re-dispatching. Implementer-adversary (Sonnet) ACCEPT
  iter 1 — empirically verified PROTECT-vs-delete-guard cascade behavior
  and exclude(is_passed=True) NULL semantics via generated SQL; one minor
  (disputed_count not hash-aware). Verifier-adversary (Grok) ACCEPT — all
  7 mutations forced expected failures; two minors (golden-hash fixture
  alone doesn't pin the skills key; capability-tag order assertion relies
  on insertion order). Gate + commit follow.
- Refit/M7 candidates (new): shared CACHES backend (database or Redis)
  required before any multi-worker deploy; per-IP rate limiting on login
  request; CSRF-denial test (enforce_csrf_checks) for the confirm POST;
  unused show_console_hint context key if still present after M6a;
  disputed_count/"withheld" UI notice not hash-tamper-aware; golden-hash
  test could pin an independent literal digest; CapabilityTag assertions
  could add explicit order_by; attestations signed pre-M6b carry no
  "skills" payload key (dev data only — repair path if ever needed is a
  client-re-signed amendment, never a payload edit).
