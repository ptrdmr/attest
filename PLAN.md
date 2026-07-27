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
- **M7b — public-record opt-in (hazard: public Capability Record; full
  dispatch).** Split by department because the charter allows exactly one
  owning department per task; M4a/M4b precedent. Two orchestrator rulings the
  human ruling did not cover: (a) the owner may always view their own record
  even when unpublished — otherwise `/record/` and the project-detail "View
  the public record" link 404 for every user on day one, since the default is
  False; (b) the publish toggle lives on the record page itself, owner-only,
  so no new nav is needed and `base.html` stays free for I5.
  - **M7b-1 (owning: Ledger):** `Profile.is_public = BooleanField(default=
    False)` plus migration `0002`; `set_profile_visibility(profile, is_public)`
    service, because Surface may not write trust-object fields directly.
    Schema authorized: this one field only. Boundaries: `ledger/models.py`,
    `ledger/migrations/`, `ledger/services.py`, `ledger/tests.py`.
    **Explicitly out of scope: do NOT change capability-tag derivation,
    `public_attestations`, or any attestation code — visibility is a display
    concern and must not alter what is attested or derived.** Tests: default
    is False for new and auto-created profiles; the service flips both ways;
    derivation output is unchanged by visibility.
  - **M7b-2 (owning: Surface; consult: Ledger read-only):** `PublicRecordView`
    404s for anonymous and non-owner viewers when unpublished, renders for the
    owner with an unmistakable not-published state; owner-only POST toggle
    calling the Ledger service. `noindex` is emitted as an `X-Robots-Tag`
    response header rather than a `<meta>` tag: `base.html` has no head block,
    so a meta tag would require editing a file reserved for I5, and the header
    is set in the view (M7b-2's own territory), cannot be missed by a partial
    render, and is directly assertable. The toggle POST must carry **explicit
    publish/unpublish intent** rather than inverting current state: a blind
    toggle is not idempotent, so a double-submit, back-button resubmit, or
    replayed POST can silently republish a record the owner just took offline
    — the exact accidental-publish direction this milestone exists to close.
    Boundaries: `surface/views.py`, `surface/urls.py`, `surface/forms.py`
    (boundary extended at iteration 2 to hold the intent form, since
    `dept_surface.mdc` forbids raw `request.POST` reads in views),
    `templates/surface/record/detail.html`, `surface/tests.py` — `base.html`
    stays untouched.
    Tests: anonymous 404 on private and 200 on public; non-owner 404 on
    private; owner 200 on own private with the not-published marker;
    `X-Robots-Tag: noindex` present only when unpublished; toggle flips the
    flag for the owner and 404s for a non-owner; the Surface profile
    auto-creation call site does not override the private default (moved here
    from M7b-1, where it forced a Ledger→Surface import inversion).
  - Role sequence: Sol (M7b-1) → Sonnet, then Sol (M7b-2) → Sonnet, then
    Composer (Verifier) → Grok (Verifier-adversary) → Gate (Opus) → commit.

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
- **Rescope the gates.** The earlier claim here — that both gates "match NULL"
  — was wrong for one of them, and the corrected semantics are verified from
  generated SQL, not reasoned about:
  - `mark_delivered` uses `filter(is_passed=False)` → `WHERE NOT is_passed`.
    Under SQL three-valued logic `NOT NULL` is NULL, not TRUE, so **undecided
    items do not block delivery today.** A project can reach `delivered` with
    items nobody has judged. Pre-existing gap; only the Surface view hides the
    button.
  - `sign_attestation` uses `exclude(is_passed=True)` →
    `WHERE NOT (is_passed AND is_passed IS NOT NULL)`, which **does** match
    NULL, so undecided items correctly block signing.
  - After I1a both gates must consider only `approved` items, block while any
    item is `draft` or `submitted`, and ignore `suspended` and `withdrawn`
    items entirely — a suspended item has no result and would otherwise block
    signing forever.
- **`canonical_payload` gains per-item state and timestamps.** Two hard
  constraints from reconnaissance:
  - The top-level `skills` key must keep its name, position, and shape.
    `_tag_dates_by_name` reads `payload.get("skills", [])` directly, so moving
    or renaming it would silently drop every historical attestation's
    capability tags to zero.
  - Old attestations are **safe**, verified rather than assumed:
    `verify_payload_hash` rehashes the *stored* `attestation.payload`, never a
    payload rebuilt from live project data. A shape change therefore cannot
    retroactively invalidate signed rows or evict them from the public record.
- **The golden-hash test does not currently pin anything.** Reconnaissance
  found `test_golden_hash_matches_sorted_canonical_json` builds a literal dict
  and recomputes the expected digest with the *same* `json.dumps` arguments,
  so it only proves `compute_payload_hash` uses `sort_keys` and compact
  separators. It never calls `canonical_payload` and pins no digest, so the
  payload *shape* is currently unguarded — the project believes it has a
  golden-hash guard and does not. I1a must add a real one: a literal SHA-256
  digest, derived independently of the implementation, pinned against a fixed
  fixture built through `canonical_payload`. Never update it to whatever the
  new code emits.
- **Backward compatibility is required so I1a can ship without I1b.**
  `criteria_locked(project)` has five call sites in `surface/views.py` plus
  one in `templates/surface/partials/criteria.html`, and
  `submit_criteria_for_approval(project)` is called by `SubmitCriteriaView`.
  Both keep their current signature and behaviour; I1a *adds* the per-item
  equivalents alongside them. I1b switches Surface over and retires the
  project-level pair. A signature change in I1a would break Surface at eight
  sites that I1a is not allowed to touch (five `criteria_locked` calls in
  `surface/views.py`, one in the criteria partial, plus the two project-level
  services). An earlier draft of this line said seven; the adversary caught
  the undercount.
- Pre-M7 attestations keep the old payload shape; per the M6b ruling the only
  legitimate repair is a client-re-signed amendment, never a payload edit.
- Boundaries: `ledger/models.py`, `ledger/migrations/`, `ledger/services.py`,
  `ledger/tests.py`. All of `surface/` and `templates/` is read-only consult —
  that footprint is I1b's.
- Test strategy: state-machine coverage for every legal and illegal item
  transition; both gates against each of the five states, including the
  suspended-item-blocks-signing-forever regression and the NULL delivery gap
  above; payload shape pinned by the new golden digest; a signed pre-I1a
  attestation still verifying and still contributing capability tags after the
  shape change.

#### Decisions closing the Planner-adversary review (rejected iter 1)

The adversary confirmed all five factual claims above against generated SQL
and running code, and rejected the plan for leaving the seam between the old
project-level approval and the new per-item state undecided. Those decisions
are now made and are binding on the Implementer.

- **The project-level services become the batch operations over item state.**
  This is the compatibility bridge, and it is why I1a can ship alone.
  `submit_criteria_for_approval(project)` also flips that project's `draft`
  items to `submitted`; `approve_criteria(project)` also flips its `submitted`
  items to `approved`. Both keep their existing signature and their existing
  project-status transition, so the seven untouched Surface call sites keep
  working and reach a gate-passing state without any I1b change.
- **Field default is `draft`** — the truthful state for a newly created item.
  **The migration backfills existing rows from project status** rather than
  blanket-defaulting: items on a `draft` project become `draft`, items on a
  `criteria_pending` project become `submitted`, and items on `active`,
  `delivered`, `attested`, or `disputed` projects become `approved`, because
  those projects demonstrably passed client criteria approval already. A
  blanket default would have been a lie about half the existing rows.
- **`ledger/tests.py` helpers must be updated in the same milestone.**
  `make_project_with_items` builds projects directly at a target status
  without routing through services, and `mark_all_items_passed` sets only
  `is_passed` via a queryset update, so neither would set item state. The
  adversary is right that the existing 68-test Ledger suite would otherwise
  fail on day one. Helpers are in-boundary; fixing them is part of the work,
  not a licence to weaken any assertion.
- **`is_passed` and `state` are orthogonal and BOTH gate signing.** `state`
  records whether the client agreed the criterion is in scope; `is_passed`
  records whether the delivered work satisfies it. `sign_attestation` requires
  `state == approved AND is_passed is True` for every non-suspended,
  non-withdrawn item. The alternative reading — gate on `state` alone — would
  allow signing an attestation covering a criterion explicitly marked failed,
  which is not what an attestation means. Not a close call; recorded because
  the plan previously left it inferable either way.
- **Payload serialization is explicit, because the naive implementation
  crashes.** Adding `submitted_at`/`approved_at` to the existing `.values(...)`
  call would put raw `datetime` objects into `json.dumps`, which raises
  `TypeError` and would break `sign_attestation` and `amend_attestation`
  outright. Timestamps enter the payload as `.isoformat()` strings, or JSON
  `null` when unset. Suspended and withdrawn items **retain** their timestamps
  — they are historical facts about what happened, and clearing them would
  destroy history inside a signed record. The golden-hash fixture uses
  hardcoded explicit datetime values, never `timezone.now()`, or it cannot be
  a pinned digest.
- **State machine edges, exhaustively.** Legal: `draft → submitted`
  (freelancer submits), `submitted → draft` (freelancer pulls back to edit,
  per ruling 5), `submitted → approved` (client approves), `submitted →
  suspended`, `approved → suspended`, `suspended → ` its prior state (resume),
  and `approved → withdrawn`. Everything else is rejected, including
  `draft → suspended` (a draft item is already invisible to the client, so
  parking it is meaningless) and every edge out of `withdrawn`, which is
  terminal.
- **Resume derives the prior state from the timestamps** rather than adding a
  field to remember it: an item with `approved_at` set resumes to `approved`,
  otherwise to `submitted`. This keeps the schema authorization to the three
  fields already granted.
- **Editing is legal only in `draft`**, and **hard delete only while
  `approved_at is None`** — which is precisely ruling 4's "items the client
  never approved", expressed as a checkable condition rather than a state
  list, so it stays correct for an item suspended after approval.
- **One more required test**, which the adversary correctly noted the strategy
  omitted: a regression walking the unmodified Surface-shaped path end to end
  — create items with no state set, `submit_criteria_for_approval`,
  `approve_criteria`, `mark_delivered`, `sign_attestation` — asserting the
  project still reaches `attested`. That is the scenario the seam breaks, and
  its absence from the first draft was itself the evidence the interaction had
  not been traced.

### I1b — Per-item criteria: Surface (owning: Surface; consult: Ledger;
hazard: client tokens; full dispatch)
- **BINDING PRECONDITION, found by the I1a adversary.** Today "approved items
  are never hard-deleted" is true only by coincidence, not by enforcement.
  `AcceptanceItemDeleteView` calls `item.delete()` directly, bypassing
  `delete_acceptance_item`, and is gated only by the project-level
  `criteria_locked`. It happens to be safe right now because the only way an
  item reaches `approved` is `approve_criteria`, which in the same transaction
  flips the project to `active` and thereby locks the view. **The moment I1b
  lets an item be approved while the project is still `criteria_pending`, that
  view becomes a live path to hard-deleting a client-approved item, silently
  violating ruling 4.** I1b must close this before its Surface wiring lands —
  either route deletion through `delete_acceptance_item` or gate the view on
  `approved_at`. `acceptance_item_locked` already exists in `ledger/services.py`
  for this purpose and is currently unused by Surface.
- **Decision needed, raised by the I1a Gate.** An approved item with
  `is_passed=False` correctly blocks delivery — but *suspending* it clears
  both gates and the project signs. That is not a false attestation: the
  payload records the item verbatim as `state: "suspended", is_passed: false`,
  and the plan deliberately requires both gates to ignore suspended items or a
  parked item would block signing forever. The problem is visibility.
  `templates/surface/record/detail.html` renders only `payload.title`,
  `payload.skills`, and `payload_hash`, never per-item data, so a public
  reader cannot see that a criterion was parked as failed. Today `suspend` has
  no Surface call site; I1b gives it one, and it will be freelancer-driven.
  **RULED 2026-07-26: the public record discloses parked criteria.** An
  attestation whose payload contains any `suspended` or `withdrawn` item says
  so on the public Capability Record. Client consent to suspend was considered
  and not chosen — it would reintroduce a round trip into the very flow I1
  exists to keep moving, and disclosure solves the actual problem, which is
  that a reader currently cannot tell. **Sequencing constraint: the suspend
  control may not merge without the disclosure.** Since the disclosure is
  harmless before any suspend UI exists, build it first within the milestone.
- Split from I1a rather than granting a Ledger-owned waiver: the Surface
  footprint (four acceptance-item views, `_project_context`, criteria
  partials, client review template) is too wide for a waiver to mean anything.
  Follows the M4a/M4b precedent.
- **I1b is itself split in two**, because reconnaissance showed the Surface
  footprint is larger than I1a's and the I1a review chain found a real defect
  at every single seat. Smaller diffs get better review.

#### I1b-1 — Public disclosure of parked criteria (owning: Surface; hazard: public Capability Record; full dispatch)
- Ships **first** and alone, satisfying the sequencing constraint above: the
  disclosure is harmless while no suspend UI exists, and this ordering removes
  any window where a freelancer can park a failed criterion invisibly.
- `templates/surface/record/detail.html` renders only `payload.title`,
  `payload.skills`, and `payload_hash` today. An attestation whose payload
  contains any `suspended` or `withdrawn` item must say so.
- **Read the payload defensively.** Pre-I1a attestations have no `state` key
  on their items, and per the M6b ruling their payloads are immutable. The
  template and any helper must treat a missing `state` as "not parked" and
  must never assume the new shape. A `KeyError` or a false "parked" badge on a
  legacy record is the failure mode to test for.
- Boundaries: `surface/views.py`, `templates/surface/record/detail.html`,
  `surface/tests.py`. No Ledger change: the data is already in the payload.
- Tests: a legacy-shaped attestation renders cleanly with no parked badge; an
  I1a-shaped attestation with a suspended item discloses it; one with a
  withdrawn item discloses it; one with all items approved shows no notice.

#### I1b-2 — Per-item criteria UI (owning: Surface; consult: Ledger; hazard: client tokens; full dispatch)
- Freelancer and client halves ship **together**, not sequentially. They are
  coupled: `approve_criteria` requires project status `criteria_pending`, so
  if per-item submission shipped alone, an item submitted mid-project would
  sit in `submitted` with no route to `approved` — and `submitted` items block
  both gates. That is a broken intermediate state, not a milestone.
- **`ClientApproveView` is the crux.** It currently calls
  `approve_criteria(project)`, which both approves and transitions the project.
  Mid-project the project is already `active`, so it must instead call
  `approve_acceptance_items(batch)` over the items the client was actually
  shown, and perform the project-level transition only in the initial
  `criteria_pending` case.
- **Ruling 7 is the security requirement here:** a client must never approve
  text it was not shown. The batch approved must be derived from what the
  review page rendered, not from "whatever is submitted right now".
- `ClientReviewView` shows `acceptance_items.all()` today; it must show the
  pending batch distinctly from already-approved scope.
- Mid-project submission needs a fresh `review`-purpose client token and
  email. `SubmitCriteriaView` only works from `draft` today.
- **`templates/surface/partials/criteria.html` branches on *project* status**
  — `not criteria_locked` / `active` / else. That branching must become
  item-state-driven, which is the single largest piece of this milestone.
- **Close the delete hole** (the binding precondition above): route
  `AcceptanceItemDeleteView` through `delete_acceptance_item`, and gate the
  per-item controls on `acceptance_item_locked` rather than the project-level
  `criteria_locked`.
- **Two consequences of per-item state that reconnaissance surfaced and the
  Implementer must not miss:** `_project_context`'s `all_delivery_reviewed`
  tests `is_passed is not None` across *all* items, so a suspended or
  withdrawn item would wrongly hold delivery back; and `DeliveryItemForm` can
  currently be posted against any item, including a parked one.
- Match existing HTMX idiom exactly: `_is_htmx`, `_render_criteria`, and
  validation failures returning the fragment with **200**, not 4xx.
- Boundaries: `surface/views.py`, `surface/urls.py`, `surface/forms.py`,
  `templates/surface/partials/criteria.html`,
  `templates/surface/partials/criterion_form.html`,
  `templates/surface/client/review.html`, `surface/tests.py`.
  `templates/base.html` stays reserved for I5.
- Retire the project-level `criteria_locked` and the batch behaviour of
  `submit_criteria_for_approval`/`approve_criteria` **only** once every call
  site is migrated; leaving both live is acceptable if migration is partial.
- Tests: per-item submit, pull-back, suspend, resume, withdraw through the UI;
  delete refused once approved; client approves only the shown batch; a client
  cannot approve an item pulled back after the page was rendered; mid-project
  submission emails a working review link; delivery gating ignores parked
  items; and the criteria panel renders correctly in every item state.

#### I1a-2 — Guarded item transitions (owning: Ledger; no consults; lands BEFORE I1b-2 commits)

- **Why it exists and why it interrupts I1b.** The I1b-2 Implementer-adversary
  proved, by direct interleaving rather than by argument, that a freelancer's
  delayed pull-back click can land after a client's approval and silently erase
  it, leaving the row in `draft` with `approved_at` still set. The Surface code
  had claimed `select_for_update()` closed this window. It does not: the lock
  only serializes the two writes, and `pull_back_acceptance_item` then performs
  a blind `save(update_fields=...)` with no re-check of current state. The test
  database is also SQLite, which does not support `SELECT ... FOR UPDATE` at
  all — Django silently drops the clause — so no test in this suite could ever
  have exercised the locking that was supposed to be the protection.
- **The bug is I1a's, already committed, but I1b-2 is what makes it
  reachable**: before the per-item UI there was no pull-back button. Human
  ruling: fix it first, so no version of this repository ever contains a
  reachable path that erases a recorded client approval.
- Fix: make every `_transition_acceptance_item` write a **compare-and-swap** —
  a guarded `UPDATE ... WHERE state = <expected>` whose affected-row count is
  checked, raising `InvalidTransition` when it is zero, instead of a blind save
  over a row that may have moved. This is a correctness fix at the only layer
  that can hold it; every caller inherits it. **No schema or migration change
  is needed or authorized** — a guarded update is a query, not a structure.
- Boundaries: `ledger/services.py`, `ledger/tests.py`. Nothing in `surface/**`;
  the uncommitted I1b-2 work stays untouched in the tree and is committed
  separately afterwards, which the disjoint file sets make safe.
- Test strategy: a **deterministic interleaving** test, not a threaded one —
  hold a stale in-memory copy of an item, commit a competing transition
  through a second copy, then drive the stale one and assert it raises rather
  than writing. Threaded concurrency tests would be nondeterministic and, on
  SQLite, would not prove what they appear to. Also assert the invariant the
  adversary caught being violated: no row may end in `draft` with `approved_at`
  set. The golden-digest payload test must stay green, which is the signal
  that no serialization behaviour drifted.

##### I1a-2 execution log

- Shipped: `_transition_acceptance_item` now performs a guarded
  `UPDATE ... WHERE pk = ? AND state = ?` and raises `InvalidTransition` on
  zero affected rows. Six transition services inherit it. Three new
  deterministic interleaving tests; suite 244 → 247. No migration, no model
  change, golden digest unmoved at `302fc585…`.
- Implementer-adversary (Sonnet) REJECT then satisfied. It confirmed the sound
  parts by **executed interleaving** rather than by reading — `suspend` fails
  closed, field sets match the old `update_fields` exactly, the in-memory
  instance stays honest, `timezone.now()` is computed once, and
  `AcceptanceItem` has no `save()`/`clean()` for `.update()` to bypass.
- **The blocker it found is the lesson of this milestone.** The first fix
  guarded `state` and stopped there, but `resume_acceptance_item` chooses its
  target by reading `approved_at` off the in-memory copy. The adversary proved
  by interleaving that a stale copy could resume an item to `submitted` with
  `approved_at` still set — silently downgrading a recorded client approval,
  the exact bug class the milestone existed to close, surviving in the one
  caller whose behaviour branches on a field.
- **Orchestrator ruling on the fix shape:** extend the guard to accept
  additional conditions and have `resume` name `approved_at` among them,
  rather than re-reading the row inside the transaction. It reuses the
  already-tested zero-row failure path instead of adding a second one, and it
  makes the dependency visible at the call site — any future transition that
  branches on a field must declare that field, which turns this bug class into
  something a reviewer can see rather than something they must reason out.
- Also fixed: a concurrently deleted row reported as a state race. Same
  exception type (a new one would ripple into Surface, out of boundary) with
  its own message constant.
- Compliance Gate (Opus 5, fresh context) PASS, eight items. It **proved**
  rather than argued that the two Ledger files commit safely alone, by building
  a throwaway worktree at `cc4cad6`, copying in only those files, and running
  the suite green at 235. It also mutation-verified all three new tests itself
  instead of trusting the builder's report.
- **Routed to I1b-2, found by the gate:** `delete_acceptance_item` still reads
  `approved_at` into Python and then calls an unguarded `item.delete()`, so an
  approval committing between the two destroys the row. Much narrower than the
  fixed bug — the window is two queries in one request, not a human's think
  time — and it sits inside I1b-2's declared delete-hole work, so it is that
  seat's to close with a guarded filtered delete.

##### Decisions closing the I1b Planner-adversary review (rejected iter 1)

All six factual claims were confirmed against the code. The plan was rejected
for leaving the client-consent seam as prose. These decisions are binding.

- **Get the threat model right first.** Ruling 7's adversary is the
  **freelancer, not the client**. The risk is a freelancer altering criterion
  text after the client has been shown it. That reframing is what makes the
  mechanism below sufficient without touching the token schema.
- **The approved batch is derived server-side, never from the request.**
  `ClientApproveView` approves exactly the items currently in `submitted`
  state for that project. The review page additionally posts a fingerprint of
  what it rendered — each item's id paired with its `submitted_at` — and the
  view **rejects the whole POST unless that fingerprint exactly matches the
  server's current submitted set.** So the posted data is a staleness
  assertion, never an authorization input: a client cannot widen the batch
  because they do not choose it, and a freelancer cannot narrow or alter it
  unnoticed because pulling an item back clears `submitted_at` and resubmitting
  mints a new one, breaking the match.
- **No token-schema or database change is required, and none is authorized.**
  `surface/tokens.py` stays out of the boundary. This was the deciding factor
  between candidate mechanisms: binding consent through `submitted_at` keeps
  the work out of auth-flow code entirely.
- **Partial staleness rejects the whole batch.** Re-render the review page with
  a plain notice that the criteria changed and ask the client to re-review.
  Partial approval would require telling a client which subset of what they
  read still counts, which is precisely the confusion ruling 7 forbids.
- **Older review tokens stay valid until they expire; they are not
  invalidated.** Because the batch is derived server-side and the review page
  renders current state, a client arriving on an older link sees exactly what
  they would approve, so there is no scope confusion to prevent. Invalidation
  would also silently break a client who simply opened their email late.
- **The per-item submit control is gated to `project.status == active`.**
  During `draft` the existing package Submit button remains the only path.
  `submit_acceptance_item_for_approval` has no status guard of its own, so
  without this an individual submit could mint a client email before the
  project had ever been reviewed at all.
- **Three sites share the state-blindness bug, not two.** The adversary found
  the third: `MarkDeliveredView.post`'s own pre-check filters
  `is_passed__isnull=True` across all items, so a parked item disables the
  freelancer's Mark delivered button even though `services.mark_delivered`
  would now succeed. Fix all three — `all_delivery_reviewed`,
  `DeliveryItemUpdateView`/`DeliveryItemForm`, and this — and treat the list
  as exhaustive only after grepping for the same pattern again.
- **I1b-1 discloses a generic badge and a count**, following the existing
  `disputed_count` notice precedent, rather than enumerating parked criterion
  text. The public record's job here is to stop a reader believing a clean
  sweep happened; reproducing the criteria themselves is a larger product
  decision that belongs with I5's richer record, not here.
- **Correction to the ordering rationale above:** shipping I1b-1 first closes
  the *Surface-driven* window, not every window. `AcceptanceItem.state` is not
  admin-readonly and the suspend service is directly callable, so a parked
  item could already be signed today. The ordering decision stands; the "no
  window" phrasing was too strong. Admin readonly for item state is logged as
  a Refit candidate rather than fixed here.

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
- Gate (fresh-context, Opus) runs on the final combined diff before EVERY commit
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
- 2026-07-26: M7b executed, split M7b-1 (Ledger) / M7b-2 (Surface) because the
  charter allows one owning department per task.
  M7b-1: Sol → Implementer-adversary (Sonnet) REJECT iter 1 — [major]
  `ledger/tests.py` imported `surface.auth`, inverting the department
  dependency; the adversary proved by mutation that the test re-proved the
  model default through a Surface call site rather than establishing a Ledger
  property, so the coverage moved to M7b-2 where it belongs. Defect was in the
  orchestrator's brief, not builder execution. [minor] guard idiom: dropped an
  unprecedented `isinstance` on the primary entity and switched `TypeError` →
  `ValueError` to match `propose_change_order`. Orchestrator ruling: iteration
  2 accepted directly rather than re-running the adversary, since both fixes
  were mechanical and verifiable by reading, and three downstream seats still
  had the code.
  M7b-2: Sol → Sonnet REJECT iter 1 — [blocker] the visibility POST inverted
  current state instead of carrying intent, so a double-submit, back-button
  resubmit, or replayed POST could silently republish a record the owner had
  just taken offline. Fixed with `ProfileVisibilityForm` carrying explicit
  publish/unpublish intent; boundary extended to `surface/forms.py` at
  iteration 2 because `dept_surface.mdc` forbids raw `request.POST` reads.
  Verifier (Composer, +6 tests → 190) closed `/record/`, the project-detail
  record link, authenticated-non-owner-on-published, dispute-notice
  interaction, preview fidelity, and migration reversibility.
  Verifier-adversary (Grok) REJECT iter 1, two blockers, both real: (1) the
  non-owner/anonymous POST test sent no `intent`, so form validation returned
  404 before the ownership check ran — deleting the authorization gate
  entirely left the test green while a non-owner posting a valid intent got a
  302 and published someone else's record; (2) `make_profile` set
  `username=handle`, making a wrong-identifier ownership comparison
  (`handle == user.username`) indistinguishable from the correct
  `user_id == user.pk` — in production `username` is the email, so that bug
  would 404 real owners out of their own records. Fixed; `make_profile` is now
  production-shaped and caused no unrelated failures. Full suite 190 green.
  Gate (fresh Opus): PASS 8/8 — independently confirmed the migration adds
  only the authorized field with no drift, that dispute-freeze display is
  untouched and renders identically in the owner's preview, and that a private
  record is indistinguishable from a nonexistent handle in production. Its
  exposure sweep also ruled out a sitemap, robots view, JSON endpoint, other
  handle-resolving route, and any caching middleware that could serve a
  private preview to an anonymous visitor. Two non-blocking notes recorded:
  under `DEBUG=1` the technical 404 page distinguishes "no such handle" from
  "private record" via the exception message (production renders both
  generically), and an owner GETting the visibility URL gets a 405 rather than
  a 404, which discloses nothing to anyone but the owner.
  Docs: README's claim that every attestation builds a *public* record was
  made false by this milestone and was corrected at final acceptance.
- Orchestrator rulings on M7b minors, accepted as-is with reasoning:
  `ProfileVisibilityView` omits `LoginRequiredMixin` deliberately — a blanket
  404 for anonymous and non-owner alike never signals resource existence,
  which matters more for a privacy control than matching `OwnedProjectMixin`.
  `test_anonymous_published_record_is_indexable` and
  `test_authenticated_non_owner_sees_published_record_without_owner_controls`
  both survive a full revert: each pins an actor whose experience this feature
  intentionally leaves unchanged, so neither can be a revert-detector by
  definition, and both were mutation-proven to catch real leakage regressions.
- 2026-07-26: I1a executed (Ledger half of per-item criteria). Planner
  (orchestrator) → Planner-adversary (Sonnet) REJECT iter 1: all five factual
  reconnaissance claims confirmed, but the plan had not decided what happens at
  the seam between old project-level approval and new per-item state, which
  would have broken the existing 68-test Ledger suite on day one. Seven
  decisions recorded above closed it.
  Builder (Sol) → Implementer-adversary (Sonnet) ACCEPT iter 1, 6/6 checklist
  PASS. It independently reproduced the pinned golden digest via .NET SHA-256
  rather than trusting the builder — exact match, so the guard is load-bearing
  rather than self-referential. Two findings logged: the I1b delete
  precondition above, and the deliberately preserved approved-plus-NULL
  delivery gap.
  Verifier (Composer) found a genuine defect: **vacuous signing.** With both
  gates rescoped onto item state, a project whose items were all suspended or
  all withdrawn — or which had no items at all — passed both gates and could
  be signed, producing an attestation attesting to zero approved criteria.
  Correctly reported with failing tests rather than papered over. Ruled in
  scope for I1a rather than logged: before this milestone there was no way to
  park an item, so the all-parked case is a state the rescoping newly created,
  and you clean what you made. Builder added an "at least one approved item"
  guard to both gates, ordered after the draft/submitted check so the more
  useful error still surfaces first.
  Verifier-adversary (Grok) REJECT iter 1 with the sharpest finding of the
  initiative: **the new guard masked the tests above it.** Every gate test set
  all items to draft or submitted, so after the guard landed they raised for
  "zero approved" instead — and deleting the draft/submitted checks from both
  gates left the entire 218-test suite green. Claims that delivery and signing
  block draft and submitted items were undefended. Also caught an untested
  transaction rollback on the compatibility bridge and three softballs. Fixed
  with mixed approved-plus-blocker fixtures asserting the discriminating
  message. Verified by the orchestrator directly: the same mutation that
  previously produced zero failures now produces four.
  Full suite 225 green (190 → 225, +35).
- **Orchestrator incident, recorded because process lessons are the point of
  this log.** While verifying the Grok fix, the orchestrator mutated
  `ledger/services.py` and reverted with `git checkout -- ledger/services.py`.
  That restores from HEAD, so it discarded roughly 200 lines of uncommitted
  I1a service work, not just the mutation. Recovered by reconstructing the
  file from the verbatim diffs captured earlier in the session; the pinned
  golden digest then verified the payload reconstruction byte-for-byte, and
  all 225 tests passed. **Lesson: `git checkout --` is only safe as a mutation
  revert when the file is otherwise clean at HEAD. With uncommitted work in
  the file, use an explicit inverse edit, or stage the work first so
  `checkout` has something to restore to.** The earlier M7a/M7b uses were safe
  precisely because those files were clean; the habit did not survive contact
  with a large uncommitted diff.
- Refit candidate (new, from the I1b planning review): `AcceptanceItemAdmin`
  has no `readonly_fields`, so item `state` is directly editable in admin,
  bypassing every service guard the state machine provides. Same shape as the
  M1 refit candidate about admin-editable dispute fields. Not fixed during
  I1b — logged.
- Refit candidates (new): three pre-existing ruff `I001` import-order errors
  in `surface/auth.py`, `surface/tests.py`, `surface/views.py` (`django.core`
  sorted after `django.core.cache`) — untouched per the cleanup doctrine,
  `ruff check --fix` clears all three. Latent flaky test (Ledger territory,
  found by the M7a adversary, outside that milestone's boundary):
  `AttestationImmutabilityTests.test_save_raises_when_signed_at_changed`
  mutates `signed_at` with a second `timezone.now()` call, so the "mutation"
  is only real if the clock advanced between `setUp` and the test body — on
  Windows the system clock granularity makes an identical value reachable.
  Now observed by two independent agents across the M7a and M7b runs, and one
  of them normalised "rerun and it passed" — which is the corrosive outcome
  when a green suite is the only evidence this project has. The immutability
  guard itself is sound; the defect is a test that can silently apply no
  mutation at all and still report success.
- 2026-07-26: flaky test FIXED (orchestrator, fast path, test-only). The
  mutation is now derived from the stored value (`signed_at -= timedelta(
  days=1)`) rather than read from the clock, so it is unconditional by
  arithmetic. Mutation-proven: dropping `signed_at` from `IMMUTABLE_FIELDS`
  fails the test with `ImmutableAttestation not raised`. Verifier-adversary
  (Grok) ACCEPT, six checklist items PASS, and independently confirmed a
  second mutation (a two-day soft-tolerance guard) is also caught.
  One [minor] declined with reasons: the adversary suggested
  `timedelta(microseconds=1)` as stricter against a hypothetical
  tolerance-based guard. Rejected — a one-microsecond delta depends on
  datetime precision surviving the round trip on both SQLite and Postgres,
  and this fix exists to remove a precision-dependent flake. Reintroducing a
  precision dependency to catch a guard nobody would write is a bad trade.
#### I1b-1 execution log (public disclosure of parked criteria)

- Shipped: `_parked_acceptance_count` in `surface/views.py` counts `suspended`
  and `withdrawn` items **per attestation** (state belongs to each immutable
  payload, not to the profile), and `templates/surface/record/detail.html`
  renders a "Scope adjusted" badge and count following the existing
  `disputed_count` notice precedent. Six new tests; suite 225 → 231.
- Implementer-adversary (Sonnet) ACCEPT, two [minor], both fixed: a dead
  `attestations` context key the diff itself orphaned, and three defensive
  `isinstance` guards.
- **Orchestrator ruling — the defensive guards were deleted, not kept.** The
  adversary proved they guarded shapes no code path can produce. The decisive
  argument was separate: had they ever fired they would have returned 0 and
  **silently suppressed the very disclosure this milestone exists to produce**,
  rendering a corrupt payload as a clean record. Fail-open is the wrong default
  for a trustworthiness disclosure — a visible 500 beats an invisible lie on
  this page. `.get()` for the missing-`state` case stays, because that is the
  permanent legacy contract rather than defensive coding.
- Verifier-adversary (Grok) ACCEPT, three [minor], one fixed. It closed the
  question the two coarse mutations left open by proving both **negative**
  tests fail under false-positive mutations, not merely under feature deletion.
  Fixed: the legacy fixture now asserts its own precondition (no `state` key on
  any item), because deleting the guards made that single test the sole
  guarantee that immutable pre-I1a payloads still render at all. Declined with
  reasons: assertion-style drift between the suspended and withdrawn tests
  (both proven to catch off-by-one and single-state counting, so churn without
  coverage), and fixtures omitting `submitted_at`/`approved_at` (real drift
  from `canonical_payload`, but inert — neither helper nor template reads them).
- Compliance Gate (Opus 5, fresh context) PASS, eight checklist items.
- **The gate earned its seat.** It found what two adversary passes and the
  orchestrator missed: every test built payloads by hand, so the suite proved
  the *renderer* but never the *pipeline*. It wrote a throwaway probe driving
  real services — failed criterion suspended, then delivered and signed — and
  confirmed the law holds end to end, then deleted the probe. Final acceptance
  required that scenario become permanent as
  `test_failed_criterion_suspended_before_signing_is_disclosed_on_public_record`,
  double-mutation-proven against both the renderer and `canonical_payload`
  dropping `state`. Without it, a future change that stopped emitting `state`
  would make the disclosure unreachable with the whole suite still green.
- Process lesson: hand-built fixtures for an immutable legacy shape are correct
  and unavoidable, but they quietly decouple the tests from the writer. When a
  milestone's fixtures bypass the production serializer, at least one test must
  drive the real path or the serializer is unguarded.
- Refit/M7 candidates (earlier): per-IP rate limiting on login
  request; CSRF-denial test (enforce_csrf_checks) for the confirm POST;
  unused show_console_hint context key (confirmed still set in views.py and
  referenced by no template);
  disputed_count/"withheld" UI notice not hash-tamper-aware; golden-hash
  test could pin an independent literal digest; CapabilityTag assertions
  could add explicit order_by; attestations signed pre-M6b carry no
  "skills" payload key (dev data only — repair path if ever needed is a
  client-re-signed amendment, never a payload edit).
