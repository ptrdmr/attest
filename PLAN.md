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

### M7 — Prerequisites for the next initiatives
- **M7a (owning: Surface, config territory; fast path):** database-backed
  `CACHES` (table `attest_cache`) in every environment so single-use magic
  links and per-email rate limits hold across workers. `MAX_ENTRIES` ceiling
  set far above the auth working set: culling deletes expired rows first, then
  drops the unexpired remainder in `cache_key` order (a hash, unrelated to
  recency), so an undersized ceiling could evict a live consumed-link marker
  inside its window.
  Boundaries: `config/settings.py`, `config/tests.py`, `surface/tests.py`,
  `README.md`. Tests: settings assert the DB backend and ceiling under both
  DEBUG and production env; surface tests assert consumed-link markers and
  rate-limit counters are visible to a second cache client that shares only
  the database.
- **M7b (owning: Ledger + Surface; hazard: public Capability Record; full
  dispatch):** `Profile.is_public` defaulting to False, public record 404s
  when hidden, `noindex` whenever not published. Schema authorized: one
  Profile field. Role sequence: Sol → Sonnet → Composer → Grok → Gate.

## Next initiatives — rulings of 2026-07-26

Human rulings on the four open ROADMAP decisions, plus three that the first
ruling exposed. These are the canonical answers; plans below implement them.

1. **Criteria semantics:** per-item submission *coexists* with package submit;
   the client approves batches of newly-submitted items; exactly one
   attestation at the end of the project (per-milestone attestations are a
   deliberate later initiative, not part of this work).
2. **Status on mid-project additions:** the project stays `active`; the item
   carries its own state. No bounce to `criteria_pending` — that would trip
   the delivery and change-order gates, both keyed to `active`.
3. **Item states:** `draft → submitted → approved`, plus `suspended` (parkable
   by either party, reversible) and `withdrawn` (terminal, for approved items
   removed from scope).
4. **Removal:** approved items are never hard-deleted. They become
   `withdrawn` and remain in the signed payload carrying that state. Hard
   delete stays legal only for items the client never approved.
5. **Submitted items lock** against freelancer edits; changing one requires
   pulling it back to `draft` first, so a client cannot approve text it was
   never shown.
6. **Portal access:** client identity keyed to `client_email` with its own
   magic-link flow reusing the M6a machinery. Requires M7a.
7. **Public profiles:** strict opt-in (see M7b).
8. **Monetization:** no new billing gates until Stripe is real. `billing.py`
   is unchanged by any initiative below.

### I1a — Per-item criteria: Ledger (owning: Ledger; consult: Surface;
hazard: signing payload; full dispatch)
- `AcceptanceItem` gains state + `submitted_at`/`approved_at`. Schema
  authorized for this model only.
- `criteria_locked` becomes per-item rather than per-project;
  `submit_criteria_for_approval` accepts a subset; new suspend / resume /
  withdraw services.
- **Rescope the gates:** `mark_delivered` and `sign_attestation` currently use
  `is_passed=False` / `exclude(is_passed=True)`, which match NULL. A suspended
  item has no result and would block signing forever. Both must consider only
  approved items, and block while any item is still `draft` or `submitted`.
- `canonical_payload` carries item state and timestamps. The golden-hash test
  must be re-pinned to an independently derived digest, never updated to
  whatever the new code emits.
- Pre-M7 attestations keep the old payload shape; per the M6b ruling the only
  legitimate repair is a client-re-signed amendment, never a payload edit.

### I1b — Per-item criteria: Surface (owning: Surface; consult: Ledger;
hazard: client tokens; full dispatch)
- Criteria builder per-item submit/suspend/withdraw controls; client review
  page batches newly-submitted items; `ClientApproveView` approves a batch.
- Split from I1a rather than granting a Ledger-owned waiver: the Surface
  footprint (four acceptance-item views, `_project_context`, criteria
  partials, client review template) is too wide for a waiver to mean anything.
  Follows the M4a/M4b precedent.

### I2 — Per-criterion to-do/notes (owning: Ledger + Surface)
- Notes and client-visible acknowledgements per acceptance item. Depends on
  I1a's per-item granularity and on I3's client identity for attribution.

### I3 — Client portal (owning: Surface; hazard: auth; full dispatch)
- Client identity keyed to `client_email`, own magic-link flow, persistent
  re-visitable project view. **Blocked on M7a.**

### I4 — Client branding (owning: Surface; fast path)
- Brand colour + logo on the portal / project brief card. Ungated per ruling 8.
- Open question for its Planner: logo upload needs a storage decision
  (`MEDIA_ROOT` is configured with no backend) plus MIME/size validation. A
  logo URL avoids both and keeps this on the fast path.

### I5 — Profile directory + search (owning: Ledger + Surface; full dispatch)
- Richer profile fields, directory and search views. Parallelizable with
  I1–I2; the opt-in flag itself ships earlier in M7b.
- **Shared-file ownership at dispatch:** `surface/urls.py` and the `base.html`
  nav belong to I5's boundary, not to any concurrent milestone.

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
- 2026-07-26: Orchestrator assessment of ROADMAP + PLAN against the code.
  Findings: Step 0 was already landed (69c2833), so the roadmap's "in-flight
  work" note was stale; the public record is not merely opt-out but
  auto-creates a guessable `/u/<email-local-part>/` for any address typed
  into the login form; `unique_current_attestation_per_project` plus
  `_current_attestation`, `public_attestations` and `_tag_dates_by_name` all
  assume one current attestation, so per-milestone attestations are a schema
  initiative rather than a services tweak (and would inflate capability tag
  counts if the derivation were not fixed with it); `criteria_locked` is false
  during `criteria_pending`, so a freelancer can edit criteria under a client
  holding a live review link and `ClientApproveView` verifies nothing about
  what was displayed. Four ROADMAP decisions ruled, plus three more the first
  ruling exposed — recorded above.
- 2026-07-26: M7a executed (orchestrator, fast path). Database cache backend.
  Verified from Django source before relying on it: `create_test_db` calls
  `createcachetable`, so the test DB gets the table; `createcachetable` makes
  `cache_key` a PRIMARY KEY, so `DatabaseCache.add` loses the race on a
  duplicate insert and returns False — the single-use guarantee genuinely
  holds cross-process. `incr` is *not* overridden by the DB backend, so the
  rate limiter is read-modify-write and approximate under concurrency;
  documented in README as bounding abuse globally rather than exactly. Full
  suite 169 green (+4). Mutations forced expected failures: locmem backend
  (all 4 fail), marker never written (clean `None is not true`), MAX_ENTRIES
  dropped to the 300 default (both settings tests fail).
  Implementer-adversary (Sonnet) ACCEPT iter 1, six checklist items PASS, four
  minors. Fixed: culling described as key-order-only when `_cull` actually
  purges expired rows first and only then drops the unexpired remainder by
  `cache_key` (corrected in settings, config tests, and this file); README
  `createcachetable` guidance named only new production databases, not
  existing local ones. Ruled accept-as-is: the two cross-worker surface tests
  fail with `no such table` under a locmem mutation rather than a content
  assertion — the assertion path is separately proven by the marker-never-
  written mutation, and contriving a cleaner signature would add test
  machinery for diagnostics alone. Gate (fresh Fable): PASS 8/8 — confirmed
  the cache table holds only SHA-256 digests (no PII, token, or secret
  recoverable from keys or values), that README's single-use claim matches
  what the code delivers, and that `attest_cache` is plan-authorized.
- Refit candidates (new): three pre-existing ruff `I001` import-order errors
  in `surface/auth.py`, `surface/tests.py`, `surface/views.py` (`django.core`
  sorted after `django.core.cache`) — untouched per the cleanup doctrine,
  `ruff check --fix` clears all three. Latent flaky test (Ledger territory,
  found by the M7a adversary, outside that milestone's boundary):
  `AttestationImmutabilityTests.test_save_raises_when_signed_at_changed`
  mutates `signed_at` with a second `timezone.now()` call, so the "mutation"
  is only real if the clock advanced between `setUp` and the test body — on
  Windows the system clock granularity makes an identical value reachable.
  Observed failing once in three suite runs by the adversary; not reproduced
  in the orchestrator's two runs. The immutability guard itself is sound —
  this is a test that can silently apply no mutation. Fix is to set an
  explicitly different timestamp rather than "now".
- Refit/M7 candidates (earlier): per-IP rate limiting on login
  request; CSRF-denial test (enforce_csrf_checks) for the confirm POST;
  unused show_console_hint context key (confirmed still set in views.py and
  referenced by no template);
  disputed_count/"withheld" UI notice not hash-tamper-aware; golden-hash
  test could pin an independent literal digest; CapabilityTag assertions
  could add explicit order_by; attestations signed pre-M6b carry no
  "skills" payload key (dev data only — repair path if ever needed is a
  client-re-signed amendment, never a payload edit).
