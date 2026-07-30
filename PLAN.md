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

### I2 — Per-criterion steps (owning: Ledger, then Surface; full dispatch)

Planned 2026-07-27. An acceptance criterion is often broad ("build a page for
current and past customer projects"), and the freelancer wants to record the
granular steps underneath it so the client can see what the work actually
involves.

**Human ruling: the client sees the steps, and they enter the signed record.**
The alternative — client-visible but unsigned — was put to the human and
rejected on the orchestrator's recommendation, because it recreates in a new
place the exact defect I1b-1, I1b-3 and I1b-4 each existed to close: a client
reads something, approves on the strength of it, and the permanent record does
not contain it. If the client sees it, it is signed.

The old stub said this depends on I3 for client identity. **It does not.** That
dependency only existed for attributing notes to a client author. Steps are
freelancer-authored and client-*visible*; nobody needs an identity to read them.
I3 is no longer a prerequisite.

#### Decisions

- **Name and shape.** `AcceptanceStep`, FK to `AcceptanceItem`
  (`related_name="steps"`), with `text`, `order`, `is_done`, `created_at`.
  Mirrors `AcceptanceItem`'s own shape, including a unique-order-per-parent
  constraint and `ordering = ("order", "pk")`. Called "steps" rather than
  "notes" in the UI because the done flag makes them tasks, and because a client
  reading a signed record should see a list of work, not marginalia.
- **Structure locks with the parent; progress does not.** Adding, editing,
  reordering and deleting steps is permitted only while the parent item is
  `draft`. Ticking `is_done` is permitted only when the parent item is
  `approved` **and** the project is `active`. This is the crux of the design: a
  step's wording can never change after a client approved it, so the approval
  cannot go stale in the dangerous direction, while the freelancer can still
  track progress during the work.
- **The `is_done` predicate is stated exactly, not by analogy.** An earlier draft
  of this plan said "while the project is `active`, exactly as `is_passed`
  already is", and the Planner-adversary caught that those are different things:
  `DeliveryItemUpdateView` gates `is_passed` on project `active` **and**
  `get_object_or_404(..., state=APPROVED)`. The loose phrasing would have let
  `is_done` be toggled on a `submitted` item sitting in a client's live review
  batch. The binding rule is the conjunction above: **project `active` AND parent
  item `approved`.** Consequently steps on `draft`, `submitted`, `suspended` and
  `withdrawn` items cannot be ticked at all — a parked item's progress freezes
  until it is resumed, which is the intended reading of parking.
- **Steps enter `canonical_payload`, nested under their item**, serialising
  `text` and `is_done` in `order, pk` sequence. Order is positional, matching how
  acceptance items already omit `order` and `pk`.
- **Items with no steps serialise `"steps": []`,** so live payload shape is
  uniform and readers never branch on key presence for current data. Only
  pre-I2 stored payloads lack the key entirely.
- **`canonical_payload` fetches steps in one additional query, not per item.**
  It currently builds items from a single flat `.values()`; nesting must not turn
  that into N+1 in a function that runs at every signature. Fetch every step for
  the project ordered by `(item_id, order, pk)` and group in Python: two queries
  total, deterministic ordering.
- **The golden digest changes exactly once, deliberately.**
  `test_golden_hash_matches_sorted_canonical_json` pins
  `302fc585ff14c2b1fda0c67375100b4ec8d802368bbf92306815cd56ef53dc19`. Adding a
  key to every item's payload dict changes it. The new literal must be recomputed
  and re-pinned in the same commit, and the fixture extended to include an item
  that actually has steps — a digest re-pinned over a fixture with no steps would
  pin nothing.
- **A second test asserts the exact per-item dict and will also break.**
  `test_canonical_payload_serializes_item_state_and_timestamps` asserts full
  equality against a six-key dict with no `steps`. The Planner-adversary found
  this by grepping after the first draft claimed the golden digest was the only
  stored value at risk; that claim was wrong. Both tests are inside I2a's
  boundary and both must be updated deliberately rather than discovered.
- **No backfill, and legacy payloads must render.** Existing signed attestations
  keep their payloads untouched; the constitution forbids otherwise. Anything
  reading a payload must tolerate items with no `steps` key, the way I1b-1
  already tolerates items with no `state`.
- **Steps do not gate delivery or signing.** They are granular acknowledgement,
  not acceptance conditions, and gating on them would invite exactly the deadlock
  family I1b-4 and I1c spent a day closing. A criterion may be marked passed with
  steps outstanding; the signing page shows both facts and lets the client weigh
  them. Whether the freelancer should get a soft warning in that case is logged
  as a follow-up, not built.
- **The fingerprint is specified exactly.** `_submitted_batch_fingerprint`
  currently emits `json.dumps` of a sorted list of `[item_pk, submitted_at_iso]`,
  with `"__missing_submitted_at__"` as the null sentinel. Each entry gains a
  third element: the item's steps ordered by `(order, pk)`, each as
  `[step_pk, step_text, step_order, step_is_done]`. **Return a SHA-256 hex digest
  of that JSON rather than the JSON itself**, because the value round-trips
  through a hidden form field and step text would otherwise grow it without
  bound. Existing tests read the fingerprint out of the rendered form rather than
  reconstructing it, so they are format-agnostic; confirm that rather than
  assuming it.
- **Step text is in the fingerprint even though the lock model says it cannot
  change.** Under the rules above nothing about a submitted item's steps is
  mutable, so in principle the fingerprint need only cover existence. Include
  text and `is_done` anyway. The whole point of pinning is to stop relying on an
  invariant enforced somewhere else — this project has now been bitten three
  times by exactly that reasoning, most recently when a race test passed against
  a check-then-act implementation because it trusted a guarantee it did not test.
- **The public Capability Record does not change.** It never listed criterion
  text and will not list steps; it continues to show title, skills, hash and the
  parked-scope notice. Steps live in the payload for the signer's benefit and for
  the record's integrity, not for public display.

#### The service surface, named

The first draft named no functions at all, which is the third time a plan in
this project has left the implementer to invent an API. I1a's plan is the
standard to match: it enumerated every transition individually. These are the
functions I2a creates in `ledger/services.py`, and the implementer adds no
others without escalating:

| Function | Guard | Raises |
|---|---|---|
| `create_acceptance_step(item, text, order)` | parent item `draft` | `InvalidTransition` |
| `update_acceptance_step(step, text, order)` | parent item `draft` | `InvalidTransition` |
| `delete_acceptance_step(step)` | parent item `draft` | `InvalidTransition` |
| `set_acceptance_step_done(step, is_done)` | parent item `approved` and project `active` | `InvalidTransition` |
| `acceptance_step_locked(step)` | — | returns bool, mirrors `acceptance_item_locked` |

- **`create_acceptance_step` is the one exception to the idiom below**, because
  there is no conditional INSERT: it checks the parent lock and then creates,
  matching `AcceptanceItemCreateView`'s existing shape including the unique-order
  savepoint. The other three mutations use the guarded-statement idiom.
- **The unique-order constraint raises `IntegrityError`, not
  `InvalidTransition`,** and it can fire independently of every guard above.
  Surface catches it exactly as it already does for duplicate acceptance-item
  order — `try` / `transaction.atomic()` / `except IntegrityError` returning an
  inline form error — so a duplicate step position never surfaces as a 500.
- **Every other mutation is a database-side guarded statement, not a
  read-then-write.**
  Follow the `delete_acceptance_item` and `_transition_acceptance_item` idiom:
  fold the parent-state condition into the same filtered `update()` or
  `delete()`, check the affected-row count, and raise on zero. Do **not** inherit
  the existing read-then-save shape in `AcceptanceItemUpdateView`, which the
  Planner-adversary correctly identified as a live TOCTOU window of the same
  family that I1a-2, I1a-3 and I1c were each written to close. Carrying that
  pattern into new payload-bound content would be a deliberate regression, and
  silence in the first draft was itself a decision made by omission.
- On refusal, distinguish the causes the way `delete_acceptance_item` does:
  parent locked, parent not approved, project not active, row gone.
- **Step text has no length cap and step count is unbounded**, matching how
  acceptance item text and count already behave. Stated so nobody invents a
  limit. `text` is `TextField`, required, non-blank after strip.

#### Milestones

- **I2a — Ledger.** Model, migration, the five services above,
  `canonical_payload`, and the two test updates. Boundaries:
  `ledger/models.py`, `ledger/migrations/**`, `ledger/services.py`,
  `ledger/tests.py`. Migration explicitly authorized: additive, no data
  migration, because no existing row needs a step.
- **`AcceptanceStep` is deliberately NOT registered in the Django admin**, and
  `ledger/admin.py` is therefore **out** of I2a's boundary. The adversary noted
  that a default registration would be worse than the standing
  `AcceptanceItemAdmin` gap: editing `step.text` through the admin would not
  touch the parent's `submitted_at`, so the review fingerprint would show no
  staleness even though locked, client-visible, payload-bound text had changed.
  Nothing needs admin access to steps. Not registering is the smallest safe
  choice and is trivially reversible.
- **I2b — Surface.** Freelancer step editor under each draft criterion, done
  ticking on approved items of active projects, step display on the client
  review and signing pages, and the fingerprint change. Boundaries:
  `surface/views.py`, `surface/forms.py`, `surface/urls.py`,
  `surface/tests.py`, `templates/surface/partials/**`,
  `templates/surface/client/review.html`,
  `templates/surface/client/sign.html`, and `static/css/app.css` — the last
  granted up front because I1b-4 had to extend its boundary mid-milestone for
  exactly this kind of nested-list styling. Reuse existing `criterion`, `badge`,
  `muted` and `stack` conventions; add a class only where none fits.
- URL and view naming follows the established child-resource convention:
  `projects/<project_pk>/criteria/<item_pk>/steps/...`, names
  `criterion-step-<action>`, `OwnedProjectMixin` for ownership, HTMX partial
  responses via the existing `_is_htmx` and `_render_criteria` helpers.

#### I2b addendum — placement decisions

Added by the orchestrator before dispatch. The accepted plan deferred UI
mechanics to "existing convention", and the Planner-adversary agreed that was
reasonable for reordering specifically. These are the remaining placement calls,
decided here rather than left for the builder to invent, because under-deciding
is what got two earlier plans in this project rejected. Nothing here changes a
reviewed decision; the fingerprint and the lock model stand exactly as accepted.

- **Steps render inside the existing criteria panel**, nested in each
  `article.criterion` between the text line and the delivery form. Every step
  control uses `hx-target="#criteria-panel"` with `hx-swap="outerHTML"`, which is
  what all nine existing controls already do. No new HTMX pattern.
- **Affordances by parent state.** A `draft` item shows its steps with per-step
  edit and delete plus an add-step form. Any non-draft item shows its steps as
  read-only text. An `approved` item on an `active` project additionally shows a
  done control per step. A draft item never shows a done control, because
  `set_acceptance_step_done` would refuse it.
- **The done control posts an explicit boolean, never a bare toggle.** M7b's
  visibility control was rejected in review for inverting current state, which is
  non-idempotent and lets a double submit or a stale page silently flip the value
  back. Steps repeat that mistake at their peril: the form carries the intended
  value, and the view sets it rather than negating what it read.
- **Client review page shows step text only, with no done state.** The client is
  being asked to approve *scope* there, and progress is irrelevant to that
  decision. This is not the sign page and must not grow into it.
- **Client signing page shows every step with its done state**, because that page
  is the legal consent point and the plan requires a criterion passed with an
  outstanding step to disclose both facts. Where a passed criterion has any undone
  step, the notice must be **visually primary, not subordinate to the "Passed"
  badge** — the I1b-4 implementer-adversary rejected exactly that subordination
  for the parked badge, and the same trap is open here.
- **Steps under a parked item still render on the signing page**, inside the
  existing "excluded from this delivery" framing rather than beside it.
- **A criterion with no steps renders no step block at all** on both
  client-facing pages; a bare "no steps" line under every criterion is noise. The
  freelancer's draft criteria always show the add-step affordance.
- **Named surface.** Forms `AcceptanceStepForm` (text, order — mirroring
  `AcceptanceItemForm`, including its duplicate-order `IntegrityError` to inline
  form error handling) and a done form carrying the explicit boolean. URLs under
  `projects/<project_pk>/criteria/<item_pk>/steps/`, named
  `criterion-step-create`, `criterion-step-update`, `criterion-step-delete`,
  `criterion-step-done`.
- **Step lookup is scoped through the owned project**, not by primary key alone:
  `get_object_or_404(AcceptanceStep, pk=step_pk, item__pk=item_pk,
  item__project=self.project)`. A step belonging to someone else's project must
  be a 404, not an authorization check that happens later.
- **View gates mirror the service guards exactly, and both bounds get a matrix
  test.** Create, update and delete require parent `draft`; done requires parent
  `approved` and project `active`. Follow
  `test_controls_and_endpoints_agree_across_all_project_statuses`: assert the
  rendered controls and the endpoints agree across every project status and item
  state, so a control that appears without a working endpoint — or an endpoint
  reachable with no control — fails.

#### Role sequence and test strategy

Planner → Implementer → Verifier for each milestone, with **the
Verifier-adversary seat kept separate throughout** — I2b moves editing
permissions, which is the case where an implementer-adversary is structurally
blind, per the I1b-4 lesson.

Tests must cover:

- The golden digest changing exactly as intended and not otherwise, over a
  fixture that actually contains steps.
- A legacy payload with no `steps` key still rendering everywhere a payload is
  read, following the I1b-1 precedent for payloads with no `state`.
- Structure edits refused once the parent item leaves `draft`, **enumerated
  across every item state**, at both the service and the view layer.
- `is_done` writable exactly when the parent is `approved` and the project is
  `active`, and refused in every other combination — both bounds pinned, not
  just the permitted one. This is the I1b-4 lesson: the side of a gate you did
  not move is the side nobody tests.
- A step change changing the fingerprint — **as a unit test on
  `_submitted_batch_fingerprint` itself**, feeding it two item-plus-step inputs
  identical except for step content and asserting the digests differ. It cannot
  be an end-to-end test: the lock model means a submitted item's steps cannot
  change while `submitted_at` holds, so any end-to-end version would be driven
  by the timestamp and would still pass if steps were silently dropped from the
  hash. That is precisely the softball this project's Verifier-adversary
  checklist exists to catch, and the adversary caught it in the plan instead.
- The signing page showing a criterion passed with an outstanding step honestly,
  disclosing both facts rather than hiding either.
- `AiDraftConfirmView`'s bulk delete cascading to steps. The adversary verified
  this path is safe by construction — it is `draft`-only, and `mark_delivered`
  refuses while any item is `draft`, so cascaded steps can never have been
  client-visible — but safe-by-construction and tested are different things.

**A hand-driven walkthrough is part of this initiative's definition of done, not
an optional extra.** I1b-4 exists because 254 passing tests sat over two real
defects that twenty minutes of clicking found immediately. Steps introduce a
nested list on four different pages with different audiences; at minimum, look
at the freelancer editor, the client review page and the signing page in a real
browser before the Compliance Gate runs.

### I3 — Client portal (owning: Surface; hazard: auth; full dispatch)

Planned 2026-07-28. M7a landed, so the block is cleared. Owning department is
Surface throughout; Ledger is a **read-only consult** — no domain change is
required, because listing a client's projects is a query and the actions already
have services.

**Human rulings taken before planning:**
1. **The portal hosts the actions, not just the view.** Clients approve scope and
   sign from inside it. The read-only option was recommended and rejected; the
   human wants one place for everything.
2. **One list across all freelancers.** A client email that appears on projects
   belonging to different freelancers sees them together. This leaks nothing to
   the freelancers — only the client sees the combined list — but it does mean a
   shared inbox such as `info@company.com` reaches every project at that company.
   Accepted knowingly.

#### The department rule must be amended before I3a lands

`dept_surface.mdc` currently states: *"Client access is tokenized: magic links /
signed tokens with expiry — clients never get accounts in MVP; token checks
happen in a shared mixin/decorator, not per-view copies."*

The design below keeps the substance of that rule — **a client still gets no
account: no `User` row, no `Profile`, no password, no handle** — but it does
replace per-request token possession with a session, which the current wording
forbids by implication. Leaving the text as-is would actively mislead the next
agent. **Proposed amendment, requiring human approval before I3a lands:**

> Client access is credentialled by email, never by account: a magic link
> establishes a session carrying a verified client email, and clients still have
> no `User`, `Profile`, password or handle. Project-scoped action links remain
> tokenized. Both the session check and the token check live in shared mixins,
> never in per-view copies.

#### The hazard that shapes everything

`MagicLinkRequestView` calls `get_or_create_freelancer` on **every allowed
request, before the link is ever clicked**, and that creates a `User` **and** a
`Profile` with a public handle for any email typed into the form. It also matches
on email first, so an email already belonging to a freelancer resolves to that
freelancer's account.

Therefore: **the client portal must have its own request view, its own token
namespace, and its own session keys, and must be structurally incapable of
reaching `get_or_create_freelancer` or `ensure_profile`.** Not "must avoid
calling" — incapable, and pinned by a test that fails if either function becomes
reachable from a portal request. A client who signs into the portal must end the
request with the same `User` and `Profile` count as before it.

**All portal auth lives in a new module, `surface/client_auth.py`.** The first
draft put these helpers in `surface/auth.py`, and the Planner-adversary pointed
out that this is the very module defining `get_or_create_freelancer` and
`ensure_profile` — so "structurally incapable" would have rested on nothing but an
implementer's discipline not to reach for the function on the next line. The new
module **must not import `get_or_create_freelancer`, `ensure_profile`, or
`ledger.models.Profile` at all**, and an import-absence assertion pins that
alongside the mock-based unreachability test. That turns the claim into something
the suite can enforce rather than something the prose asserts.

**The identity-free primitives are shared, not duplicated, via a third neutral
module `surface/signed_links.py`.** Round 2 pointed out that narrowing the
boundary traded a discipline problem for a duplication one: single-use cache
consumption and the DEBUG-mode quoted-printable token repair
(`normalize_magic_login_token`) are generic mechanics that both flows need, and
neither has anything to do with identity. So both are extracted into a module that
imports no identity code at all, and `surface/auth.py` and
`surface/client_auth.py` both import from it. **`surface/auth.py` returns to
I3a's boundary for that behaviour-preserving extraction only.** This is safe
because the guarantee is now carried by the import-absence test on
`client_auth.py`, not by excluding a file from a list.

**The portal gets its own DEBUG one-click link, `attest_dev_portal_url`**, mirroring
`attest_dev_login_url`. Without it the mandated hand walkthrough is harder for the
portal than for the freelancer flow, because the console email backend mangles
tokens with quoted-printable encoding — a real effect visible in this project's own
test output, not a theoretical one.

#### Decisions — identity and session

- **A portal token encodes a normalized email, not a project**, and lives in a
  separate namespace from the three project-scoped purposes.
  `make_portal_token(email)` / `read_portal_token(token)` with salt
  `surface.client.portal`. **Do not add `"portal"` to `CLIENT_TOKEN_PURPOSES`** —
  those govern project-scoped tokens and share a payload shape. Keeping the sets
  disjoint means a portal token can never be read as a review or sign token, and
  that isolation gets a test in the shape of the existing
  `ClientTokenPurposeIsolationTests`.
- **Portal login links are single-use and short-lived: one hour**, matching the
  freelancer magic link rather than the 14-day project tokens, because this link
  establishes a session rather than granting one action. Consumption uses the same
  cache-marker mechanism under its own key namespace
  `surface.portal-login.used:{sha256(token)}`.
- **The GET-then-POST confirm step is mandatory**, for the same reason the
  freelancer flow has it: an email scanner prefetching the link would otherwise
  consume it. This is not optional polish; it is the existing hardening and
  skipping it would be a regression against a known attack.
- **The session keys are exactly `attest_client_email` and
  `attest_client_verified_at`**, matching the existing `attest_billing_*` and
  `attest_dev_login_url` naming. Naming them here is deliberate: "clears only the
  client keys" is not an auditable instruction unless the keys are enumerated, and
  the session also carries Django's three `_auth_*` keys plus billing-stub flags
  that must survive untouched.
- **The server-side `attest_client_verified_at` check is the expiry authority, and
  it is not redundant with the cookie.** `SESSION_COOKIE_AGE` is unset, so Django's
  14-day default applies — the same figure by coincidence — but Django's
  `expire_date` is a **sliding** window recomputed on save, so a client browsing
  daily would never expire. The stored timestamp gives an absolute cutoff from
  first verification. **Session life is 14 days from verification**; the one-hour
  figure belongs to the link, not the session. This reasoning is written down
  because a future reviewer could otherwise "harmonize away" the custom check as
  duplicate logic.
- **Sign-in calls `request.session.cycle_key()`** to close session fixation.
  Confirmed against Django's source: `cycle_key()` preserves session data and does
  not disturb `_auth_user_id`, and `login()` itself calls `cycle_key()` rather than
  `flush()` when no user is already authenticated — so a freelancer logging in
  beside an active client session preserves it.
- **Client sign-out clears only the two client keys**, never flushing.
- **Freelancer logout ends the client session too, and that is accepted rather
  than worked around.** Django's `logout()` calls `request.session.flush()`
  unconditionally, and one browser has one session, so there is no way to keep a
  client session alive across a freelancer logout without weakening the freelancer
  logout — which would be the wrong trade. The first draft asserted "freelancer
  logout keeps flushing everything, unchanged" as though that were consequence-free
  and listed a test only for the *opposite* direction. **Both directions are now
  required tests**, and the client can simply request a fresh link.
- **Rate limiting mirrors the freelancer limiter** — five requests per email per
  hour — under its own key namespace, and, as there, a blocked request must take
  the same visible path as an allowed one.
- **Every comparison of a session email against `client_email` is
  case-insensitive (`client_email__iexact`)**, at all three call sites: the
  eligibility check when a link is requested, the project-listing query, and I3c's
  signing-identity gate. Round 2 caught this as an unstated assumption, and it is a
  real one: `client_email` has **no normalization anywhere** — not on the model, not
  in `ProjectForm` — because until now it was only ever a mail-to address, and the
  portal is the first place this codebase treats it as an identity key. A freelancer
  who types `Client@Example.COM` would otherwise be matched by some call sites and
  not others, depending on which the implementer wrote first. The codebase already
  has the idiom for this in `email__iexact`. Normalizing the stored column is the
  cleaner long-term fix and is logged as a follow-up rather than done here, since it
  is a Ledger change and this initiative is Surface-owned.
- **A signing-time email mismatch returns 404**, matching the established
  convention that access failures do not disclose existence. In practice it should
  be unreachable — the session mixin would already have refused the page, and
  `client_email` cannot change once a project has left `draft` — so this is defence
  in depth. It is specified anyway because "the session's email must equal
  `client_email`" is not testable without saying what happens when it does not.
- **Requesting a link for an email with no projects sends no email and shows the
  same page.** Enumeration parity with the freelancer flow; a client must not be
  able to discover whether an address is a client of anyone.

#### Decisions — discovery and navigation

- **The three existing action emails each gain one line pointing at the portal
  request page — the plain URL of the form, not a token.** Without this the portal
  ships undiscoverable: nothing would ever tell a client it exists, and the
  self-service request page would be a URL nobody has. Pointing at the request
  form rather than embedding a portal token also means no email carries a
  long-lived credential, so the existing "tokens never rendered where they need not
  be" posture is preserved. **No new freelancer-triggered invite action is built**;
  clients already receive these emails, so a second mechanism would be surface
  without purpose.
- **The action link stays on the first line of the body, and any email test that
  parses it must read that first line rather than the whole body.** In the event
  only one test did so; the plan said three, and the builder corrected it. Round 2
  of the plan review proved by reproduction that
  `test_per_item_submit_emails_working_review_link` does
  `urlparse(mail.outbox[0].body.strip()).path` over the entire body, so appending
  any second line corrupts the parsed path and turns a green test red. **This is a
  legitimate test edit and not a violation of the no-existing-test-may-change
  invariant**, which binds I3c's consent refactor specifically: here the email
  format is deliberately changing and the test was written against a
  single-line assumption. The distinction matters, because an implementer who
  applies the invariant too broadly will conclude the discovery decision is
  impossible to implement.
- **`base.html` gains a client strip, rendered when `attest_client_email` is
  present in the session**, showing the verified email, a link to the portal, and a
  client sign-out. It is **separate from and visually distinguishable from the
  freelancer nav**, which stays gated on `request.user.is_authenticated` and will
  be false for a client by the identity guarantee above. In the two-hats case
  **both render simultaneously**, and the strip must make it obvious which identity
  is which — an ambiguous header here is how someone signs the wrong thing. The
  template reads the session through `request`;
  `django.template.context_processors.request` is enabled at `config/settings.py`
  line 62, so this is confirmed rather than assumed.
  Colour and layout are left to the implementer, but the floor is testable: **an
  automated test must assert the two strips render as structurally distinct
  elements with both identity strings present**, backstopped by the mandated
  two-hats walkthrough.
- **The existing emailed action links keep working exactly as they do today.**
  The portal adds a second doorway; it does not deprecate the first. Stated
  explicitly because the token path being retired is a reasonable thing for a
  future agent to assume, and it would break every client mid-project.

#### Decisions — what the portal shows

- **Draft projects never appear.** A draft is the freelancer's private workspace
  and the client has not been told it exists. The portal lists
  `criteria_pending` and later, only.
- **Each project shows which freelancer it belongs to** by display name, which a
  mixed cross-freelancer list is unreadable without. **This ships in I3a, not
  I3b** — the adversary noted that "a bare list" in I3a would contradict this same
  paragraph's reasoning on day one.
- **Cross-tenant isolation depends on `client_email` being editable only while a
  project is `draft`**, which `ProjectUpdateView.project_is_accessible` currently
  enforces. The whole session-identity model rests on that holding, so it is stated
  as an explicit dependency and gets a test that fails if the draft-only gate is
  ever widened — otherwise a future edit could silently move a live project between
  clients' portals.
- **The portal shows progress during `active`, including step done flags.** This
  is the void the reconnaissance found — a client currently sees nothing at all
  between approving scope and being asked to sign — and closing it is the point of
  the initiative.
- **The client review page stays scope-only.** I2b deliberately withheld progress
  there because that page asks the client to approve *scope*, and progress is
  irrelevant to that question. The portal asks a different question and may answer
  it. **These two rules are not in conflict and must not be "harmonized"** by a
  future agent who notices the difference.
- **A disputed project is shown as disputed.** Domain Law forbids presenting a
  disputed attestation as clean, and that applies to this new surface on day one,
  not as a follow-up.
- **A finished project shows the signed record**: the frozen payload's checklist,
  the hash, and the signature time. Today `signed.html` shows only hash and
  timestamp, so the portal is where a client can finally re-read what they signed.

#### Milestones

**These three are strictly sequential and must never be dispatched
concurrently.** I3b needs I3a's `ClientSessionMixin` and I3c needs both, and all
three claim `surface/views.py`, `surface/urls.py`, `surface/tests.py` and
`templates/surface/portal/**`. Two concurrent briefs over those files is the
file-collision defect the charter warns about, and this project has split work
across simultaneous briefs before.

**No milestone in I3 requires a migration.** Session keys and the existing
database cache table carry everything. Stated explicitly because the constitution
forbids unauthorized schema changes, and "no migration needed" should be a
decision on the record rather than something inferred from the absence of a
sentence.

- **I3a — Client session foundation.** Token functions, request/confirm/sign-out
  views, rate limiting, session establishment, `ClientSessionMixin`, and a project
  list **including freelancer attribution**. Boundaries: `surface/tokens.py`,
  **`surface/client_auth.py` (new)**, `surface/views.py`, `surface/forms.py`,
  `surface/urls.py`, `surface/tests.py`, `templates/surface/portal/**`,
  `templates/base.html`, `static/css/app.css`, **`surface/signed_links.py` (new)**,
  and **`surface/auth.py` for the shared-primitive extraction only**. An earlier
  draft excluded `auth.py` outright, and the builder correctly flagged that the two
  statements contradicted each other; the extraction decision above supersedes the
  exclusion, and the identity guarantee now rests on the import-absence test rather
  than on keeping a file off a list. `config/tests.py` is dropped from the boundary;
  the first draft carried it over from M7a's list, and no settings change is needed.
- **I3b — Portal project view (read-only).** Project detail with scope, progress,
  change-order history, delivery state and the signed record. Boundaries:
  **`surface/portal_views.py` (new)**, `surface/client_auth.py`,
  **`surface/delivery.py` (new)**, `surface/views.py`, `surface/urls.py`,
  `surface/tests.py`, `templates/surface/portal/**`,
  `templates/surface/partials/**`, `templates/surface/client/sign.html`,
  `static/css/app.css`.
  - **I3b shares the delivery checklist as a template partial, not just as a
    Python helper.** The first revision extracted `_delivery_rows(project)` from
    `ClientSignView._render` and called the duplication closed. Round 2 showed that
    was the wrong half: the Python there builds only a trivial
    `{item, steps, has_undone_steps}` shape, while **the state-conditional badge
    markup that actually drifted in I1b-4 — the block that told a client a
    suspended criterion was "Not passed" — lives in
    `templates/surface/client/sign.html` lines 19-51.** Extracting the plumbing
    while leaving the portal free to hand-write that conditional again would
    reproduce the original defect on a new surface.
    So: extract both, the `_delivery_rows(project)` helper **and** a shared row
    partial included by `sign.html` and the portal template alike, plus a **parity
    test asserting both surfaces render the same item states identically**.
    `templates/surface/client/sign.html` therefore **is** edited, to include the
    partial — but its existing tests must stay green **unmodified**, which is what
    makes the extraction provably behaviour-preserving. Boundary gains
    `templates/surface/client/sign.html` and `templates/surface/partials/**`.
- **I3c — Portal-hosted actions.** The hazardous one. See below.

#### I3c is a refactor of the consent seam, and the charter governs it

Putting approval and signing in the portal means two doorways into
`approve_acceptance_items` + `approve_criteria` and into `sign_attestation`.
**Duplicating that logic is forbidden.** The consent seam carries a batch
fingerprint, an all-or-nothing savepoint, and stale-batch handling that took four
milestones and several adversary rejections to get right; a second copy would
drift from the first, and the drift would be silent.

**The shared unit is a helper function, never a shared URL-routed view.** This is
the single most important sentence in the milestone, and the first draft left it
ambiguous. `templates/surface/client/review.html` bakes the token into its form
actions via `{% url 'surface:client-approve' token=token %}`, and a
session-resolved route has no token kwarg — so one View class serving both would
force either a fork of that template or a signature change to
`ClientApproveView.post`, and either one breaks the invariant below. Instead:

- `_approve_submitted_batch(project, submitted_fingerprint)` holds the fingerprint
  comparison, the savepoint and the approval calls, and returns an outcome the
  caller maps to its own response.
- `_sign_delivery_record(...)` holds the signing call.
- `ClientApproveView` and `ClientSignView` keep their existing signatures,
  templates and URLs untouched, and become thin callers.
- New `PortalApproveView` and `PortalSignView` are separate thin callers with their
  own templates.

That is a refactor of consent-critical code, and the charter's role sequence for a
refactor is **Verifier first, then Implementer** — characterization tests before
the change, not after.

- **The binding invariant: no existing test may change.** The 310-test suite is
  the characterization harness. Every existing consent test drives its view
  through `reverse()` and the test client rather than calling view methods
  directly, so a behaviour-preserving internal extraction is invisible to them —
  which is what makes the invariant achievable rather than aspirational. If the
  refactor nonetheless requires editing an existing consent test, that is not a
  test problem: it means behaviour moved, and it **stops and escalates to the
  orchestrator** rather than being accommodated.
- **Portal signing is bound to the verified email.** Today possession of a sign
  token is sufficient and the attestation records `project.client_email`
  regardless of who actually clicked. Signing from a session must additionally
  require that the session's verified email equals `project.client_email`. Note
  this makes the portal path *stronger* than the token path; equalizing them is
  out of scope and is logged, not fixed here.
- Boundaries: **superseded by the addendum below.**

#### I3c addendum — the boundary the isolation architecture forces (2026-07-29)

The boundary above was written before I3a and I3b established where portal code
lives, and it now contradicts that architecture. `surface/views.py` imports
`ensure_profile` and `get_or_create_freelancer` at module scope (lines 34-35), so
adding `PortalApproveView` and `PortalSignView` there would place portal routes in
a module that reaches account creation — undoing, for the two most consequential
routes in the initiative, the guarantee I3a built and I3b extended, and which the
Verifier-adversary has already defeated twice with fresh bypasses. The original
boundary is therefore **amended, not interpreted**:

- **Portal action views go in `surface/portal_views.py`**, beside the display view,
  which currently imports only Ledger models, `client_auth` and `delivery`.
- **The shared consent helpers go in a new neutral `surface/consent.py`** that
  imports Ledger services and nothing else. They cannot live in `views.py`:
  `portal_views.py` would then have to import that module and inherit the same path
  to the identity functions. This is the same reasoning that put `_delivery_rows`
  into `surface/delivery.py` during I3b, and the second time the isolation
  guarantee has dictated a module rather than a preference.
- **All three identity guards grow to cover the two new routes** — the row-count
  matrix, the mock-based unreachability matrix, and the AST import-absence check
  over `client_auth.py`, `portal_views.py` and `consent.py`. Both previous
  milestones were rejected for a guard that had a hole, and both holes were in
  newly added surface.
- Amended boundaries: `surface/portal_views.py`, **`surface/consent.py` (new)**,
  `surface/views.py` (thin-caller extraction only), `surface/urls.py`,
  `surface/tests.py`, `templates/surface/portal/**`, `static/css/app.css`.

**The two carried-forward items are resolved in this milestone, not deferred
again.** I3a carried the observation that the client strip reads as subordinate to
the freelancer nav on portal pages, and ruled it acceptable only because I3a
contained no signing; I3b carried the ruling that the undone-steps caution stays
with its criterion on a read-only page, and said to revisit it when the client is
about to sign on that page. Signing now arrives, so both are due: **client identity
becomes primary on portal pages, and the undone-steps caution takes the signing
page's more prominent treatment** rather than the read-only page's.

#### I3c characterization pass — what it proved (2026-07-29)

Suite 352 → **358**. The Verifier ran first, per the charter's role sequence for a
refactor, and **corrected the plan's own claim about the harness.** Line 1117 above
asserts that every existing consent test drives its view through `reverse()` and
the test client. That is not fully true: four tests call
`surface.views._submitted_batch_fingerprint` directly, and three patch
`surface.views.services.*`. The invariant is still achievable, but only under
implementation choices that are now mandatory rather than incidental:

- **`consent.py` must do `from ledger import services` and call
  `services.approve_acceptance_items(...)` and friends by module attribute at call
  time.** `patch("surface.views.services.approve_acceptance_items")` mutates the
  attribute on the shared `ledger.services` module object, so attribute-style calls
  from a different module remain intercepted, while `from ledger.services import
  approve_acceptance_items` binds at import time and defeats the patch.
- **`_submitted_batch_fingerprint` moves to `consent.py` but must stay reachable as
  `surface.views._submitted_batch_fingerprint`.** This is not a compatibility
  shim: `_client_review_context` in `views.py` genuinely still needs it to render
  the hidden fingerprint field, so the import is load-bearing.

Both constraints are **empirically proven, not reasoned**. A simulated extraction
making the opposite choices — bare function imports plus an unconditional
`approve_criteria` — was run against the suite and produced **7 failures** across
`ClientApproveViewTests`, `ClientSignViewTests` and
`PerItemCriteriaWorkflowTests`. The harness fails loudly on a wrong extraction
rather than passing silently, which is the property the whole Verifier-first
sequence exists to establish.

Six characterization tests were added, each mutation-proven: blank fingerprint
staying distinct from a handled empty batch, an active project not replaying
`approve_criteria`, the exact `client_email` recorded by the token path, POST
signing rejecting a non-delivered project before reaching Ledger, POST signing of
an already-attested project reusing the existing record, and a signing
`InvalidTransition` returning the generic 410.

**Verifier-adversary: ACCEPT**, sixth pass from this seat, two minors and no
blockers or majors — the first time it has not found something that had to change.
It re-derived all six mutation proofs independently rather than trusting the
builder's table, and for four of the six it also tried a *weaker* mutation of the
right shape, on the principle that a test which only catches a sledgehammer is a
softball. Both minors were ruled on:

- **M1, a vacuous assertion, fixed.** The blank-fingerprint test asserted
  `assertNotContains("These criteria have already been handled.")`, which cannot
  fail in that fixture: `templates/surface/client/review.html` line 45 renders that
  string only under `{% elif not stale_batch %}`, so the stale path never emits it.
  The fix was **not** to delete the line — that would leave the "not empty" half of
  the test's name unearned — but to assert absence of the empty-state marker from
  line 30, which renders whenever pending scope is empty *regardless* of the stale
  flag and is therefore a real witness that submitted scope survived the rejection.
  Proven by mutation: approving the batch on the invalid-form path makes the new
  assertion fail while the old one stays green, which is direct evidence the swap
  converted a vacuous line into a load-bearing one.
- **M2, a soft count under a mock, accepted unchanged.**
  `attestations.count() == 1` stays true even if `sign_attestation` is invoked under
  the patch, but `assert_not_called` already catches that case. Same ruling as I3a
  made on the row-count guard: defence in depth does not require every layer to
  catch every attack.

**Process note.** The first Verifier-adversary run was interrupted mid-experiment
and left `surface/views.py` rewired to a scratch module. The orchestrator recovered
per the charter — assessed the tree rather than trusting a report, harvested the
interrupted experiment's result (which is the 7-failure evidence above), reverted
production code and deleted the scratch file. Two lessons worth keeping: **an
interrupted adversary leaves live mutations on disk**, so the tree must be
inspected before anything else happens; and **an interrupt can roll back the
orchestrator's own file edits while preserving a subagent's**, which silently
reverted this plan amendment once and required re-applying. The re-dispatched pass
was scoped to a softball hunt with scoped test runs instead of repeated full-suite
runs, since the extraction question was already settled.

**The harness is committed before the refactor begins**, so that I3c's binding
invariant is checkable against a committed baseline rather than being disentangled
from the implementer's work in a single uncommitted diff.

#### I3c implementation round 1 — Implementer-adversary REJECT, and one boundary extension (2026-07-29)

Suite 358 → 369, tests diff additions-only, all four non-negotiable constraints
verified, extraction confirmed behaviour-preserving line by line against
`bbc958e`, and no hole found in the identity guards for the two new routes. The
adversary rejected on five findings, all accepted:

- **Blocker: the portal recorded the wrong email spelling.** `PortalSignView`
  passed the verified session email to the signing helper while the token path
  passes `project.client_email`. Ruled: **both paths record
  `project.client_email`.** `ClientProjectMixin` resolves the project with
  `client_email__iexact`, so the two values are provably equal up to letter case
  by the time `post()` runs — the adversary confirmed `__iexact` case-folds with
  no whitespace normalization, so anything else would already have 404'd. The
  session spelling therefore encodes zero extra truth while making an immutable,
  hashed payload that feeds the public Capability Record depend on which doorway
  the client used. The plan authorized adding a gate, not changing what is
  recorded.
- **Major: a client losing a signing race got a bare 404.** `InvalidTransition`
  inside `PortalSignView.post` was converted to `Http404`, and the path had no
  test. The 404 convention exists so access failures do not disclose existence,
  but here existence is already disclosed — the client holds a verified session
  and the mixin has proven the project is theirs. Ruled: the client must be told
  what happened and given a way forward, following the portal approve path's own
  precedent of re-rendering with a notice rather than erroring, and it must be
  pinned by a test that actually raises `InvalidTransition` inside `post()`.
- **Major: the carried-forward outstanding-steps prominence was missed**, for the
  third milestone running. `.outstanding-steps-notice` was byte-identical before
  and after, and the test named for it would pass with prominence entirely
  absent. Ruled: fix with **page-scoped CSS only — the shared partial must not be
  forked**, which keeps I3b's parity test intact, and the test must fail if the
  prominence is removed.
- **Major: "client identity primary" was visual only.** `base.html` DOM order was
  unchanged; the client strip was hoisted purely by flex `order` under
  `body:has([data-portal-page])`. Tab order and screen-reader reading follow DOM
  order, so on the signing page the very users least able to catch the ambiguity
  visually still met the freelancer identity first — and with no `:has()` support
  the layout silently reverts to freelancer-first. Since the requirement exists
  because "an ambiguous header here is how someone signs the wrong thing", a
  cosmetic satisfaction of it is not satisfaction. **Boundary extended by
  orchestrator authority to `templates/base.html` and
  `templates/surface/partials/**`** so the client strip can genuinely lead the
  document on portal pages, with DOM order asserted for a portal page and
  freelancer-first order asserted to survive on non-portal pages. No concurrent
  milestone claims either path, so this is not a file-collision risk.
- **Major: `approve.html` reproduced a previously-litigated wording defect.** Its
  two empty-state messages render together and unconditionally, so an
  invalid-form resubmission against an already-approved batch shows "the criteria
  changed after this page was shown", "no criteria are currently awaiting
  approval" and "these criteria have already been handled" at once.
  `templates/surface/client/review.html` already solved this with
  `{% elif not stale_batch %}`; the portal template dropped the distinction.
  Same family as I1b-4 and the I3b polarity gap.
- **Minor, accepted without change:** `PortalApproveView` has no project-status
  guard, so a GET for an `active` or `attested` project renders a dead-end shell.
  The token doorway's equivalent views carry no status guard either and the
  wording shown is accurate in every such state, so this matches the existing
  idiom rather than breaking it. Logged, not fixed.

#### I3c execution log (2026-07-29)

Suite 358 → **375**. Hazard-zone builder seat throughout, and the tests diff stayed
**additions-only against `bbc958e` at every round**, so the binding invariant held
without a single existing test being edited.

**Built:** `surface/consent.py` (the shared seam: fingerprint, guarded
compare-and-swap approval with its savepoint, and the signing call),
`PortalApproveView` and `PortalSignView` in `surface/portal_views.py` as thin
callers beside the token path's own thin callers, `templates/surface/portal/`
`approve.html` / `sign.html` / `signed.html`, and three extracted partials —
`client_strip.html`, `site_header.html` and `signed_record_meta.html`.

**Verifier-adversary round 2: REJECT** on the new portal tests, its sixth
consecutive real finding. All four gaps were mutations that left **all 14 new tests
green**, and the headline one was the sharpest yet: **removing `"portal_page": True`
from `PortalSignView._render` alone** reverted the signing page's DOM to
freelancer-first while every test stayed green — reintroducing, on the one page
where a client actually signs, the exact accessibility defect the previous fix round
had just closed. The root cause is worth remembering: **portal-page treatment has
two independent mechanisms**, the `portal_page` context flag driving DOM order and
the `data-portal-page` attribute driving the CSS, and a route can set one without
the other. Closed with a single test that iterates every portal route rather than
five near-duplicates, so a route added later is harder to omit. It also found the
prominence CSS rule orphaned from the class that activates it, the `ATTESTED` branch
of the signing-race handler untested, and — the subtlest — that the identity guards
never established a freelancer session, so an `ensure_profile` call gated on
`request.user.is_authenticated` would have passed both of them. Accepted its minor
about CSS `order` being inert without `display: flex` unchanged, on the standing
ruling that DOM order is the substantive guarantee and CSS is presentation.

**Final acceptance found one real defect and dismissed two reports.**
`templates/surface/portal/signed.html` showed a bare hash with **no signature time
and no explanation** — a repeat of the defect I3b's own final acceptance had already
ruled on for the detail page, reappearing on the one surface where it matters most,
immediately after the client signs. Fixed by extracting `signed_record_meta.html`
and including it from both surfaces, so the explanation has one source and the two
cannot drift.

Dismissed, with evidence rather than judgment:
- **The walkthrough reported the two-hats scenario as a critical blocker, and it is
  not.** This is the *second* time a browser walkthrough has reported this exact
  false positive for the same reason — its browser already held a different
  freelancer's session, so the login hit Django's identity-change path, which
  correctly flushes. Verified against
  `test_switching_freelancers_flushes_existing_client_session`, which proves a
  **first** freelancer login preserves the client session while a **switch** flushes
  it. Re-run with the identities established freelancer-first, the two-hats state
  works: both identities coexist and are labelled unmistakably (`Client:` versus
  `Freelancer:`, `Sign out as client` versus `Log out`), the client strip leads on
  portal pages, the ordering flips back on freelancer pages, and client sign-out
  leaves the freelancer session intact. **Lesson for the next walkthrough: reset
  identity state first, and establish the freelancer session before the client
  one.**
- **A reported em-dash encoding defect does not exist.** The response is served
  `text/html; charset=utf-8`, `base.html` declares the meta charset, the stored
  values are clean, and a raw-byte check of the served page found a correct UTF-8
  em-dash and no mojibake. It was an artifact of the walkthrough tool's text
  extraction.

**One positive design note worth recording.** The addendum's instruction that the
identity guards must "grow to cover the two new routes" could have been read as
requiring edits to the existing matrix tests — which would have violated the
binding invariant. The builder instead added a new test class that independently
re-proves row counts and unreachability for the new routes, and the adversary
verified that as the correct resolution rather than a shortcut. The new module's
AST check is also stricter than the one it was modelled on: it bans any
`__import__` or `importlib.import_module` call outright rather than only calls
naming specific targets, which is the I3a bypass closed at the root.

#### Role sequence and test strategy

Full dispatch for all three, hazard-zone builder seat throughout, and **the
Verifier-adversary seat stays separate** — it has rejected three consecutive
milestones and found a real defect in each.

Tests must cover:
- **The identity guarantee**, as the highest priority: a complete portal
  request/confirm cycle leaves `User` and `Profile` counts unchanged, and
  `get_or_create_freelancer` and `ensure_profile` are unreachable from every
  portal route. Pin it so that wiring a portal view to freelancer auth fails.
- Token namespace isolation in both directions: a portal token rejected by all
  three project purposes, and each project token rejected as a portal token.
- Single-use consumption, reuse after consumption, expiry, and that a GET does not
  consume.
- Enumeration parity: identical response and zero mail for an unknown email, an
  email with only draft projects, and a rate-limited request.
- Session expiry enforced by the server-side timestamp, not only the cookie —
  prove by ageing the stored timestamp while leaving the cookie valid.
- Cross-tenant isolation: a signed-in client sees exactly the projects whose
  `client_email` matches, no drafts, and a 404 rather than a 403 for a project
  that is not theirs, matching the existing convention that failures do not leak
  existence.
- Both hats at once, **in both directions**: a browser holding a freelancer session
  and a client session behaves correctly for both; client sign-out leaves the
  freelancer session intact; and freelancer logout ends the client session, which is
  the accepted behaviour and must be pinned so nobody "fixes" it into a partial
  flush later.
- The `client_email` draft-only dependency, so that widening
  `ProjectUpdateView.project_is_accessible` fails loudly.
- The portal auth module's import-absence guarantee, alongside the mock-based
  unreachability test.
- For I3c specifically: the existing suite passing **unmodified**, plus the
  session path exercising the same fingerprint and savepoint behaviour as the
  token path, plus a signing attempt where the session email does not match
  `project.client_email`.

**A hand-driven walkthrough is part of the definition of done for each
milestone**, as it was for I2b, and for I3 it must include the two-hats case in
one browser — that is the scenario least likely to be caught by tests and most
likely to be encountered by a real freelancer who is also somebody's client.

#### Accepted risks and logged follow-ups

- **A freelancer can put any email in `client_email`, so anyone can inject a
  project into a stranger's portal listing.** This is not new — that address has
  always received the action emails — but the portal changes the exposure from one
  ignorable email into a persistent entry mixed among the client's real freelancer
  relationships, and there is no client-side dismiss, hide or report. Accepted for
  I3 and logged: a dismiss mechanism is the natural follow-up, and email
  verification of `client_email` is the heavier alternative.
- **A project whose status changes while a client is looking at it** needs no new
  machinery: I3b is read-only so there is nothing to go stale, and I3c inherits the
  existing fingerprint and `InvalidTransition` handling through the shared helper.
  Written down because the absence of a mechanism should be a reasoned decision
  rather than an oversight.
- **Portal signing is bound to a verified email while token signing is not**, which
  leaves the two paths at different strengths. Equalizing them means either binding
  sign tokens to an email or retiring them, and both are their own milestone.
- **`client_email` is stored unnormalized.** `client_email__iexact` handles it at
  every portal call site, but normalizing on save is the cleaner fix and is a Ledger
  change, so it is logged for a future milestone rather than smuggled into a
  Surface-owned initiative.

#### I3a execution log (2026-07-28)

Suite 310 → **338**. Hazard-zone builder seat throughout.

**Built:** `surface/client_auth.py` (portal tokens, session helpers, and the portal
views), `surface/signed_links.py` (mechanics shared with freelancer auth),
`surface/context_processors.py`, a portal token namespace in `surface/tokens.py`,
`templates/surface/portal/`, and a client strip in `templates/base.html`.

**Views live in `client_auth.py`, not `views.py`, and that is deliberate.** It
diverges from this codebase's pattern of auth modules holding only helpers, and the
Implementer-adversary flagged it and then recommended accepting it, which I did. The
reason is decisive: `surface/views.py` already imports `get_or_create_freelancer`
and `ensure_profile` at module scope, so portal views living there would make the
import-absence test meaningless. Keeping them in a provably identity-free module is
what makes the central guarantee checkable at all.

**Implementer-adversary: ACCEPT** with one major and three minors, all ruled on.
The major was that the row-count invariant covered only the request and confirm
routes while the other two guards were weaker than they looked, so the net had a
hole; extended to every route. Two minors became fixes: the header rendered the
client strip from the raw session key, so an expired identity displayed as active
(my fault — the plan told the builder to read the session directly), fixed with a
context processor; and `verified_client_email` mutated the session as a side effect
of a read, split into a pure check plus an explicit invalidation. Boundary was
extended for that fix to `surface/context_processors.py` and one line of
`config/settings.py`.

**Verifier-adversary: REJECT, then ACCEPT.** Its fourth consecutive rejection in
this project and its fourth real finding. It defeated **all three identity guards at
once** with `__import__("surface.auth", ...)` plus a call on an email that already
had a `User` — the route matrix omitted login POST, the AST check only saw static
imports, and row counts cannot move for an existing identity. It also showed the
fourteen-day cutoff could silently become a **sliding window** with nothing going
red, which is the precise mutation the stored-timestamp design exists to prevent;
that all three action emails could lose their portal discovery line unnoticed,
because discovery was tested on the helper rather than through the views that send
mail; and that the token salt could be set to `surface.client.review` while the
isolation test stayed green, because that test proved payload shapes differ rather
than namespaces. All six findings closed and each re-proved by re-applying the
mutation that exposed it. On the row-count guard staying green for an
already-existing identity, the adversary ruled it acceptable: defence in depth does
not require every layer to catch every attack, and reachability is pinned by the
mock and the AST ban.

**Walkthrough.** The portal lists all four non-draft projects for
`client@acme.com` across both freelancers with attribution, and does not leak the
same client's draft project. Two process notes worth keeping:

- **A zombie dev server from an earlier session held port 8009 and served
  pre-portal code**, producing a 404 on every portal route and an alarming
  "the feature does not exist" report. The new server had silently failed to bind.
  Check the port's owning process, not just that a server is running.
- **The browser walkthrough reported a critical two-hats defect that does not
  exist.** It already held a different freelancer's session from an earlier
  walkthrough, so its freelancer login hit Django's identity-change path. A direct
  probe showed the truth: a client session survives a **first** freelancer login via
  `cycle_key()`, and is flushed when a **different** freelancer signs in over an
  existing session. The flush is correct, and it was an unspecified third direction
  beyond the plan's two, so it is now pinned by
  `test_switching_freelancers_flushes_existing_client_session`.

**Carried into I3c:** on portal pages the freelancer nav sits in the primary header
while the client strip sits below it, so the client identity reads as subordinate on
a page where the client is the subject. Both are explicitly labelled, so the plan's
"unmistakable" bar is met for I3a, which contains no signing. I3c introduces signing
into the portal and is where subordinate framing starts to matter — make the client
identity primary on portal pages then.

#### I3b addendum — decisions I3a's isolation forced (2026-07-28)

I3a put the portal views in `surface/client_auth.py` precisely because
`surface/views.py` imports `get_or_create_freelancer` and `ensure_profile` at module
scope, which would have made the import-absence test meaningless. That decision has
two consequences the original plan did not foresee, and both must be settled before
a builder starts.

- **Portal display views go in a new `surface/portal_views.py`, not in
  `surface/views.py` and not piled into `client_auth.py`.** Putting them in
  `views.py` would place a portal route in a module that reaches identity-creating
  code, quietly undoing I3a's guarantee for every route added from here on. Piling
  them into `client_auth.py` would work but turns an auth module into a grab-bag.
  So: `client_auth.py` keeps sign-in, session and mixin; `portal_views.py` holds
  portal display. **The import-absence test must be extended to cover both
  modules**, and the row-count and unreachability matrices must grow to include
  every new portal route — I3a's Implementer-adversary specifically warned that
  I3b would add routes onto this foundation.
- **`_delivery_rows(project)` goes in a new neutral `surface/delivery.py`**, not in
  `views.py`. If it stayed in `views.py`, `portal_views.py` would have to import
  that module and thereby open a path to the identity functions. `delivery.py`
  imports Ledger models and nothing else, and both `ClientSignView` and the portal
  import it. The current inline construction is at `surface/views.py` lines
  1286-1309, building `{item, steps, has_undone_steps}`.

#### I3b — what the client actually reads

- **Status wording is client-facing, not the internal vocabulary.** The freelancer's
  states are `criteria_pending`, `active`, `delivered`, `attested`, `disputed`, and
  the I3a walkthrough flagged that a client reads "Criteria pending" as jargon.
  Map them to what the client is being told: awaiting their approval, work in
  progress, delivered and awaiting their signature, signed, and disputed. **Apply
  the mapping to the I3a project list as well as the new detail page**, so the two
  never disagree — a list saying one thing and a detail page another is worse than
  either wording alone.
- **The page shows scope, live step progress, change-order history, delivery state,
  and, once signed, the frozen payload's checklist with its hash and signature
  time.** The last is the point of the initiative: today a client who signs has no
  way to re-read what they signed.
- **A disputed project is labelled disputed**, per Domain Law, on this surface from
  its first commit rather than as a follow-up.
- **No actions.** Approving and signing arrive in I3c. Nothing on this page may
  mutate anything, and there must be no control that looks actionable and is not.
- **A project that is not this client's returns 404**, matching the convention that
  access failures do not disclose existence — the same 404-not-403 choice the
  freelancer-owned views make.

#### I3b execution log (2026-07-28)

Suite 338 → **352**. Hazard-zone builder seat retained deliberately: I3b is not auth
code, but it edits the isolation guarantee's blast radius and the signing page.

**Built:** `surface/portal_views.py` and `templates/surface/portal/detail.html`
(read-only project page: brief, live scope with step progress, change-order history,
delivery state, dispute notice, frozen signed-record replay);
`surface/delivery.py` and `templates/surface/partials/delivery_row.html` (the shared
extraction); client-facing status wording applied to both the list and the detail
page.

**Implementer-adversary: ACCEPT**, no blockers or majors. It confirmed all three
identity guards were extended to the new module and route, that `sign.html` lost only
the extracted markup with zero test lines touched, and — by measurement rather than
inspection — that the page is flat at six queries regardless of row count. Its two
minors (no legacy-payload test, no amendment test) were queued rather than fixed
immediately and folded into the Verifier round.

**Verifier-adversary: REJECT, then ACCEPT.** Fifth consecutive rejection from this
seat, fifth real finding. The headline one was a **new** identity bypass: the AST
check banned `__import__` but not `importlib.import_module`, the mock only watched
two named functions so a direct `create_user` was invisible, and the row-count cycle
used an email that already had a `User` — so a portal route could mint an account for
a virgin client email with all three guards green. It also caught the
**dispute-notice polarity** gap, where always rendering the notice stayed green
across four tests, which is precisely the bug I2b shipped and had to fix; that the
frozen replay pinned text and steps but **not `is_passed`**, so a client could be
shown the wrong Pass/Fail on a checklist we call frozen; that read-only was only
form-shaped, so an anchor styled as a button passed; and that list/detail status
"agreement" was substring containment, so a longer detail label satisfied a shorter
list expectation while the two genuinely disagreed. All closed and re-proved under
the exposing mutations.

**Final acceptance found what both adversaries missed.** The hand walkthrough showed
a signed project rendering **the same checklist twice** — live "Scope and progress"
above the frozen "Signed delivery record". Identical in the dev data, so it read as
duplication, but they can diverge, and if they do the client reads the **unsigned**
one first. Same family as I1b-4 and the disputed-record rule: the authoritative
record must not be subordinate to or confusable with something that is not the
record. **Ruled: when a current attestation exists, the page shows the signed record
and not the live scope list**, `disputed` included, with the dispute notice retained.
Unsigned statuses keep the live section. The builder correctly reported that one
already-reviewed parity test asserted both lists on an attested page and therefore
conflicted with the ruling, rather than quietly rewriting it.

Visual review added one change: the SHA-256 hash rendered as a bare string, which is
the product's trust anchor presented as noise, so it now carries one plain sentence
explaining that it is a fingerprint of the record that changes if any detail is
altered.

**Carried into I3c**, alongside I3a's item about client-identity prominence: the
caution that a criterion is marked passed while steps remain undone currently sits
with its criterion, which is right on a read-only page. A walkthrough suggested
promoting it to a page-level alert and I ruled against it — page level would detach
it from the criterion it describes, so a client would know something was outstanding
without knowing what. **When signing moves into the portal in I3c, revisit it**: the
client will then be about to sign on this page, and the signing page's more prominent
treatment should govern.

#### Plan review history

Two Planner-adversary rounds, both REJECT, both finding real defects — which is the
charter's limit, so the plan came to the human rather than looping a third time.
Round 1 found the I3c extraction shape undecided in a way that would have broken
the plan's own invariant mid-refactor, and link discovery undecided entirely, which
would have shipped the portal undiscoverable. Round 2 found the email-body change
would break a green test it never mentioned, that narrowing the auth boundary had
traded a discipline problem for a duplication one, that the checklist extraction
fixed the half that never drifted, and that `client_email` comparison semantics
were unstated. Every finding was accepted; there is no outstanding disagreement
between planner and adversary, and the escalation is procedural rather than a
deadlock.

### I4 — Client branding (owning: Surface; fast path)
- Brand colour + logo on the portal / project brief card. Ungated per ruling 8.
- Open question for its Planner: logo upload needs a storage decision
  (`MEDIA_ROOT` is configured with no backend) plus MIME/size validation. A
  logo URL avoids both and keeps this on the fast path.

### I5 — Profile directory + search (owning: Ledger + Surface; full dispatch)
- Richer profile fields, directory and search views. The opt-in flag itself
  shipped earlier in M7b. The original "parallelizable with I1–I2" note is spent:
  both are complete, so I5 runs on its own.
- **Shared-file ownership at dispatch:** `surface/urls.py` belongs to I5's
  boundary. The companion note claiming the `base.html` nav is stale — I3c moved
  the nav into `templates/surface/partials/site_header.html` (finding 5).

#### Human rulings taken 2026-07-29, before planning

**Numbering, to prevent a collision:** inside the I5 section, "ruling N" means one of
the four below, and roadmap-level rulings are cited explicitly as "roadmap ruling N".
The two sequences overlap numerically and mean different things.

The human's framing: **a front-end search for visitors who browse freelancers
without creating an account of any kind.** That is in line with what exists rather
than a new direction — `PublicRecordView` already serves a published record to
anonymous visitors and M7b-2's tests pin anonymous 200 on public and 404 on
private, so account-free reading is shipped. I5 supplies the discovery layer that
currently has no entry point.

1. **Attested capability is the primary search and browse axis; self-declared text
   is a secondary filter only.** `CapabilityTag` (`profile`, `name`,
   `attested_count`, `last_attested_at`) is derived from signed attestations and
   recomputed, so a visitor can ask which freelancers hold client-signed deliveries
   in a capability — a claim no ordinary directory can make, and the product's
   premise. `Project.skills_csv` and profile text are freelancer-declared and may
   filter or refine, but **must never carry the headline claim in a listing**.
   Ruled against making the two equal, precisely because equal billing would let
   declared text read as though it were attested.
2. **The directory and published records are search-engine indexable.** Organic
   discovery is accepted as a growth channel. This confirms current behaviour
   rather than changing it: M7b-2 emits `X-Robots-Tag: noindex` only while a record
   is unpublished, so published records are already indexable and no change to that
   view is authorized here. The directory itself must not emit `noindex`.
3. **Ranking: attested volume leads a capability search, recency leads an
   unfiltered browse.** A capability-filtered result orders by that tag's
   `attested_count` descending, ties broken by `last_attested_at` descending, then
   `handle` ascending so the ordering is total and deterministic. An unfiltered
   browse orders by `last_attested_at` descending, then `handle`, which surfaces
   working practitioners instead of entrenching whoever registered first. Volume is
   defensible as the headline signal precisely because it cannot be self-issued —
   every increment costs a client signature — and the recency tie-break stops a
   dormant high-volume profile from owning the top of the page forever.
4. **`handle` stays immutable in MVP; `display_name` becomes editable.** The handle
   is the public record URL (`/u/<slug:handle>/`), so editing it breaks inbound
   links, and once records are indexable a released handle can be re-registered by
   someone who then inherits its accumulated reputation — an impersonation vector.
   Decoupling the name a freelancer is called from the address their record lives at
   delivers the whole benefit without that risk, and needs no retired-handle
   reservation table because nothing is ever retired.

#### Constraints its Planner must carry

- **Dispute state must be correct in listings, not only on detail pages.** Domain
  Law forbids presenting a disputed attestation as clean, and a directory is a
  **new** surface presenting derived aggregates. A listing ranked or filtered on
  `attested_count` could show a freelancer with a disputed project as cleanly
  credentialed while their detail page carries the notice. This is the reason I5 is
  full dispatch. Note the already-logged Refit candidate that the
  `disputed_count`/"withheld" notice is not hash-tamper-aware.
- **Richer profile fields mean a migration**, which requires explicit
  authorization. `Profile` today carries only `user`, `handle`, `display_name`,
  `headline`, `is_public`, `created_at` — no skills, bio, location or rate. The
  authorized field list must be enumerated in the plan, as M7b-1's single-field
  authorization was.
- **Split by department at dispatch**, per the charter's one-owning-department
  rule: Ledger for fields, derivation and query services; Surface for the directory,
  search and nav. Precedent is M4a/M4b and M7b-1/M7b-2.
- **No new dependencies**, so search stays ORM-level — capability-tag joins plus
  `icontains` on SQLite, not a full-text engine. Adequate at MVP scale and it keeps
  the constitution intact.
- **Strict opt-in means the directory ships empty** until freelancers publish
  (`is_public` defaults False per roadmap ruling 7). Correct, but it makes the empty state a
  first-class design problem rather than an afterthought.

#### Reconnaissance findings that shape this plan (2026-07-29)

Six facts about the current code, each with a consequence the plan must carry. All
were re-verified after the Planner-adversary review; findings 3 and 6 changed as a
result.

1. **Freelancers cannot edit their own public identity, and it is derived from
   their email address.** `ensure_profile` (`surface/auth.py:48-56`) sets
   `display_name` to the email local-part (`seed.partition("@")[0]`) and `handle`
   to a slug of the same seed; `headline` is left blank and **no profile-editing
   view or form exists anywhere** — `surface/forms.py` has only
   `ProfileVisibilityForm`, and `surface/urls.py` has no profile-edit route. So a
   directory shipped today would list freelancers as "dana" and "stepwalk". Worse,
   combined with the indexability ruling it would publish the **local-part of every
   opted-in freelancer's email address to search engines**. **A directory cannot
   ship before freelancers control their own identity**, so that becomes the first
   milestone rather than a later polish item.
2. **`CapabilityTag` is a cache, and the directory is the first surface where it
   becomes load-bearing.** `recompute_capability_tags`
   (`ledger/services.py:655`) rebuilds from `_tag_dates_by_name`, which already
   filters `status=ATTESTED, is_current=True, is_disputed=False` and skips any
   attestation failing `verify_payload_hash` — so the cache is dispute-clean and
   tamper-clean **at write time**, and it is recomputed by all four services that
   can change the facts (`sign_attestation`, `amend_attestation`, `flag_dispute`,
   `resolve_dispute`). Crucially, the public record *detail* page does not rely on
   it: `public_attestations(profile)` recomputes live and re-verifies hashes on
   every request. A directory that filters and ranks on `CapabilityTag` has **no
   live recomputation to fall back on**, so cache correctness becomes the
   correctness of the whole surface.
3. **The dispute-cache bypass is wider than one admin form — I first recorded this
   finding as narrower than it is, and the Planner-adversary was right to reject it.**
   Verified directly against the code: `Attestation.IMMUTABLE_FIELDS`
   (`ledger/models.py:229-235`) covers `payload`, `payload_hash`, `client_email`,
   `client_name_typed`, `signed_at`, and necessarily **excludes** `is_disputed` and
   `disputed_at` because `flag_dispute` must write them. Consequently
   `AttestationQuerySet.update()` (`:189-196`) blocks only the immutable set and so
   permits `.update(is_disputed=True)`; `Attestation.save()` permits the same; and
   `ledger/tests.py:2825` and `:2852` already do exactly that. Separately
   `AttestationAdmin` (`ledger/admin.py:62-72`) deliberately leaves both fields
   editable — its docstring calls them "dispute controls". That is **three** write
   paths that change dispute state without recomputing the cache, so promoting Refit
   candidate (1) as written closes one of three and I presented it as sufficient. In
   a directory with no live recomputation to fall back on, any of the three
   **presents a disputed freelancer as cleanly credentialed**, which Domain Law
   forbids. Note also that `QuerySet.update()` does not fire `post_save`, so a
   signal alone is likewise insufficient.
   Two further facts, found while verifying the above:
   - **Locking the admin fields removes a real capability.** Dispute flagging from
     the admin is a deliberate feature, so the fix must replace the editable fields
     with admin actions calling `flag_dispute` / `resolve_dispute`, not merely make
     them read-only.
   - **An admin flipping `is_disputed` today creates a two-model inconsistency, not
     just a stale cache.** `flag_dispute` also transitions the project to
     `DISPUTED`; a bare field edit leaves `Project.status = ATTESTED` while the
     attestation reads disputed, and `_tag_dates_by_name` filters on **both**. The
     equivalent `Project.status` vector is already closed for admins —
     `ProjectAdmin.readonly_fields` includes `status` (`ledger/admin.py:29`) — which
     is both the precedent this fix should follow and evidence the project already
     considers service-routed status changes the rule.
4. **The capability cache ignores `is_public`.** `recompute_capability_tags` writes
   tags for every profile, published or not, so **every directory and search query
   must filter `profile__is_public=True`**. Omitting that filter silently publishes
   every private profile's capability data, which is the exact failure the strict
   opt-in ruling exists to prevent. It gets a dedicated test.
5. **Tag names are slugs, and the nav has moved.** `_normalized_skills`
   (`ledger/services.py:434-442`) slugifies, so `CapabilityTag.name` holds values
   like `react-native` and a visitor typing "React Native" matches nothing unless
   the query is slugified the same way. Separately, the plan's standing note that
   "the `base.html` nav belongs to I5" is now stale: I3c extracted the nav into
   `templates/surface/partials/site_header.html`, which is where the entry point
   must be added — and that partial renders nav links **only when
   `request.user.is_authenticated`**, so an anonymous visitor currently has no
   discovery entry point at all.
6. **`site_header.html` is load-bearing for I3c's identity-order tests.**
   `surface/tests.py:6080-6081` and `:6098-6099` assert DOM order by string index,
   locating the literal `<header class="site-header">` relative to
   `data-identity="client"`. Any I5 edit to that partial must preserve that exact
   opening tag and the client-strip / site-header ordering, and those tests must
   pass **unchanged** — editing them to accommodate a new nav link would silently
   retire I3c's portal-identity guarantee. The anonymous entry point therefore goes
   outside the `{% if request.user.is_authenticated %}` block without disturbing the
   header element itself.

#### Decisions closing the Planner-adversary review (rejected iter 1)

The adversary's central charge was that the plan stated goals where it owed
mechanisms, so a builder would have to invent them. Each gap is now closed by a
decision, not deferred to dispatch.

1. **The secondary text filter searches profile fields only** — `display_name`,
   `headline`, `bio`, `location`. It does **not** search `Project.skills_csv`.
   Project skills are per-project, include draft text a freelancer never intended to
   publish, and would leak unattested claims onto an anonymous indexable surface. So
   no denormalized skills column is added and no fourth migration field is needed.
   This **narrows** ruling 1 rather than contradicting it: ruling 1 said declared text
   *may* filter, and this decision settles which declared text does.
2. **Anti-conflation is a test, not an intention.** Ruling 1 forbids declared text
   carrying the headline claim, which is unenforceable as prose. The test asserts
   that attested capabilities render inside a distinct labelled element, that
   self-declared text renders outside it, and that no verification language
   ("attested", "verified", "signed") appears in the declared-text region. It must
   fail if a template later merges the two.
3. **Two empty states, not one.** "No freelancer has published a profile yet" (the
   launch-day condition under strict opt-in) and "no result matches this search" are
   different messages with different next actions, and each gets its own test. The
   original plan named only the first.
4. **Pagination is specified up front:** Django's built-in `Paginator`, 20 profiles
   per page, and the flat-query test asserts a bounded result set so the page-size
   promise cannot silently regress into "render everything". Paging uses
   **`get_page()`, not `page()`** — the latter raises `PageNotAnInteger` / `EmptyPage`,
   which on an anonymous public surface means `?page=abc` returns a 500 instead of the
   friendly generic response `dept_surface.mdc` requires. A malformed and an
   out-of-range `?page=` each get a test.
5. **The dispute-cache fix is split into its own milestone (new I5c).** Given
   finding 3, its scope is no longer "make two admin fields read-only" but "hold the
   cache-freshness invariant across three write paths, one of which is the queryset
   that enforces attestation immutability". That is hazard-zone work and cannot ride
   along with read-only query services.
6. **The directory anchors on `Profile`, not on `CapabilityTag`** — added in
   iteration 2, after the adversary found a profile the plan would have silently
   erased. `Project.skills_csv` is `blank=True` (`ledger/models.py:51`) and
   `_normalized_skills("")` returns `[]` (verified), so an attested, opted-in,
   dispute-clean project with no skills entered produces **zero `CapabilityTag`
   rows**. A `CapabilityTag`-anchored query would drop that freelancer from the
   directory entirely while their public record shows genuine signed work — hiding
   real attested work is a presentation failure of the same family as the ones ruling
   1 exists to prevent. So:
   - The browse query selects published profiles and joins tags as an annotation;
     a profile with no tags **still appears**, with its capability slot rendering a
     designed "no attested capabilities yet" state (a third empty state, distinct
     from decision 3's two, and tested).
   - `last_attested_at` is per-`(profile, name)`, not per-profile — there is no
     profile-level recency column. The unfiltered browse sort key is therefore the
     **maximum `last_attested_at` across that profile's tags, nulls last**, then
     `handle` ascending. Zero-tag profiles sort deterministically to the bottom,
     which is the honest outcome: they have given the directory nothing to match on.
   - A **capability-filtered search legitimately excludes zero-tag profiles** —
     nothing to match is not the same as being hidden. That asymmetry is deliberate
     and gets a test so a later "fix" cannot quietly collapse the two queries.
   - Note for the freelancer-facing copy: the remedy is entering skills on projects,
     which is a `Project` field and therefore **outside I5's boundary**. No scope is
     added here; I5b edits profile fields only.
7. **I5e displays the three self-declared fields; nothing else does** — added after the
   I5b Implementer-adversary correctly refused to improvise this. The gap it found: every
   decision so far says which profile fields are **searched** (decision 1), and none ever
   said they are **displayed**, so I5b shipped an edit form whose output was invisible.
   That is a plan defect, not a builder defect, and inventing a public-surface layout at
   the Implementer layer would have repeated iteration 1's exact failure one level down.
   Ruling:
   - `bio`, `location` and `website_url` render in **I5e**, both on the directory card
     and in the record page's heading region — **outside** the `Verified skills` card and
     visually distinct from it, with **no** "attested"/"verified"/"signed" vocabulary in
     that region. This is decision 2's anti-conflation rule applied to a detail page
     rather than a listing.
   - **I5e's boundary gains `templates/surface/record/detail.html`.** I5b also holds that
     file; safe only because the milestones are strictly sequential, same caveat as
     `static/css/app.css`.
   - Worth recording honestly: `headline` is equally self-declared and **already** renders
     unlabelled on the record page today, predating decision 2. So I5e is bringing an
     existing inconsistency into line rather than introducing a new risk.
   - Until I5e lands, I5b's form legitimately writes fields that display nowhere. Accepted
     as a seam, not a defect, because the alternative was an unreviewed public-surface
     design decision made by a builder.

For the record, one alternative was considered and rejected: having the directory
compute dispute-clean counts **live** and skip the cache entirely, which would make
freshness moot. It cannot work — capability tags live inside the attestation
`payload` JSON, so they cannot be grouped in SQL, and `verify_payload_hash` runs in
Python and cannot be expressed as a database filter. The cache is structurally
necessary, which is exactly why the guarantee has to come from the write path.

#### Milestones — five, strictly sequential

The honest reason for serial execution is **not** that each milestone depends on the
last: I5c is independent of I5b, and their boundaries do not even overlap
(`ledger/**` versus `surface/**`, `ledger/tests.py` versus `surface/tests.py`). They
run in series because the charter puts cross-department sequencing on the
orchestrator (M4a/M4b, M7b-1/M7b-2 precedent), because the review loop rather than
the typing is the bottleneck, and because I5e depends on all four. The genuine hard
edges are: **I5b requires I5a's fields**, **I5e requires I5d's services**, and
**I5d is only trustworthy given I5c**. Whichever milestone is in flight claims
`surface/tests.py`, `ledger/tests.py` and `surface/urls.py`.

- **I5a — Identity fields and service (owning: Ledger).** The enumerated migration
  plus a `set_profile_details(profile, **fields)` service, because
  `dept_surface.mdc` forbids Surface writing trust-object fields directly — the same
  reason M7b-1 added `set_profile_visibility`.
  **Schema authorization GRANTED by the human 2026-07-29, this list only** — any
  further field requires a fresh grant: `bio` (`TextField`,
  `blank=True`), `location` (`CharField(max_length=120, blank=True)`), and
  `website_url` (`URLField(blank=True)`). `headline` already exists and needs no
  migration, only an editing surface.
  **Deliberately excluded, with reasons:** an hourly rate or availability flag (both
  go stale silently and an availability promise on an indexable page invites exactly
  the disputes this product exists to adjudicate); and an avatar or logo, because
  image storage is I4's open question — `MEDIA_ROOT` has no backend — and smuggling
  it in here would import that unresolved decision into a Ledger migration.
  Boundaries: `ledger/models.py`, `ledger/migrations/`, `ledger/services.py`,
  `ledger/tests.py`.
- **I5b — Profile editing surface (owning: Surface; consult: Ledger read-only).**
  The freelancer's own edit form and view, calling the I5a service. This exists so
  that nothing is ever published under an email-derived name. Editable per ruling 4:
  `display_name`, `headline`, `bio`, `location`, `website_url`. **`handle` is not
  editable and must not appear as a form field** — a test asserts that posting a
  `handle` value leaves it unchanged, so the immutability survives a future form
  edit rather than resting on it being omitted today.
  Boundaries: `surface/views.py`, `surface/urls.py`, `surface/forms.py`,
  `templates/surface/profile/**`, `surface/tests.py`.
- **I5c — Cache freshness across every dispute write path (owning: Ledger; hazard
  zone).** The finding-3 fix, standing alone because it touches the queryset that
  enforces attestation immutability. Three layers, because no one of them is
  sufficient:
  1. `AttestationQuerySet.update()` recomputes tags for affected profiles when the
     dispute markers are touched — needed because `QuerySet.update()` never fires
     `post_save`. This must **extend** the existing immutability guard, not rewrite
     it; the `ImmutableAttestation` behaviour on signed fields stays byte-identical.
  2. A `post_save` receiver on `Attestation` recomputes when dispute markers change,
     catching direct `.save()` calls including `update_fields` writes.
  3. `AttestationAdmin` makes `is_disputed` / `disputed_at` read-only **and** gains
     admin actions calling `flag_dispute` / `resolve_dispute`, preserving the
     capability while keeping `Project.status` consistent. Precedent:
     `ProjectAdmin.readonly_fields` already contains `status`.
  Definition of done includes a **write-path matrix test**: service call, direct
  `.save(update_fields=...)`, bulk `.update()`, and the admin action each leave the
  cache correct. Every cell must fail if its layer is removed.
  **Plus one assertion the matrix structurally cannot make.** The matrix asserts
  "this write leaves the cache correct", which can only exercise layer 3's admin
  *actions*; it says nothing about layer 3's read-only *fields*, because a blocked
  edit changes nothing and so satisfies "the cache is still correct" whether the
  guard exists or not. If someone later re-exposes `is_disputed`/`disputed_at` as
  editable admin form fields — the exact defect finding 3 found — no matrix cell
  would fail. So I5c also asserts directly that **a staff POST to the attestation
  admin change form attempting to set `is_disputed` / `disputed_at` leaves both
  unchanged **while the POST itself succeeds** (200/302, no form errors), in the same
  spirit as I5b's hostile-POST test on `handle`. The success half matters: without it
  the test could pass because the POST failed validation for an unrelated reason
  rather than because the guard held — the same vacuous pass this assertion exists to
  prevent.
  Boundaries: `ledger/models.py`, `ledger/admin.py`, `ledger/services.py`,
  `ledger/apps.py` (signal registration only), `ledger/tests.py`. **No migration**
  — this milestone changes no fields.
- **I5d — Directory query services (owning: Ledger).** The read-only services the
  directory needs: a published-profile listing and a capability search, both
  filtering `profile__is_public=True` per finding 4, both returning capability tags
  without an N+1, both slug-normalizing the query per finding 5, and both applying
  ruling 3's explicit `order_by`. Query logic lives in services, not views, per
  `dept_ledger.mdc`.
  Boundaries: `ledger/services.py`, `ledger/tests.py`.
- **I5e — Public directory and search UI (owning: Surface).** The
  anonymous-accessible directory and search pages, the entry point in
  `site_header.html` visible to visitors who are not logged in, a link from the
  landing page, pagination per decision 4, and all three empty states — decision 3's
  two plus decision 6's zero-capability slot.
  Carries the finding-6 constraint: I3c's identity-order tests must pass untouched.
  Boundaries: `surface/views.py`, `surface/urls.py`, `surface/forms.py`,
  `templates/surface/directory/**`,
  `templates/surface/partials/site_header.html`, `static/css/app.css`,
  `surface/tests.py`.

#### Test strategy

The load-bearing cases, beyond ordinary coverage:

- **A private profile with capability tags never appears** in the directory or in
  any search result, and its tag data appears nowhere in either response body.
  This is the strict opt-in ruling and finding 4; it must fail loudly if the
  `is_public` filter is dropped.
- **Two separate claims here, deliberately not merged.** First, in I5c: the cache
  stays correct after a dispute arrives by **any** write path — the write-path
  matrix above, which is the regression test for finding 3 and must fail if a layer
  is removed. Second, in I5d/I5e: given a correct cache, a disputed freelancer is
  never presented as clean in a listing. The first is about cache freshness, the
  second about presentation; a single test that conflated them would pass while
  either half was broken.
- **A tamper-failed attestation does not contribute to a listing**, mirroring the
  `verify_payload_hash` guard that `public_attestations` applies live.
- **Slug-normalized matching**: "React Native" finds `react-native`, and the
  chosen partial-match semantics are pinned explicitly rather than left to
  `icontains` by accident.
- **Anonymous access throughout**: directory and search return 200 with no session
  of any kind, and the nav entry is present for an anonymous visitor — the finding-5
  gap.
- **Indexability is asserted, not assumed**: the directory must **not** emit
  `X-Robots-Tag: noindex`, in deliberate contrast to M7b-2's unpublished-record
  header. Ruling 2 above makes this a requirement, so it gets a test that fails if
  someone later adds a blanket header.
- **Query count is flat** regardless of how many profiles and tags exist, measured
  rather than inspected, as I3b's adversary did for the portal detail page.
- **Ordering is deterministic and explicitly asserted** — an earlier milestone was
  caught relying on insertion order, so ruling 3's ranking must be pinned with an
  explicit `order_by`, including the `handle` tie-break that makes the order total.
  Construct a tie deliberately and assert the result is stable.
- **Attested and declared text never conflate** — decision 2's test: attested
  capabilities inside a labelled element, declared text outside it, no verification
  vocabulary in the declared region.
- **Both empty states**, per decision 3: nothing published yet, and no match for
  this query. Distinct copy, one test each.
- **Pagination is bounded** — page size holds at 20 and the flat-query test asserts
  the result set is bounded rather than rendering every profile.
- **`handle` survives a hostile POST** — posting a `handle` to the profile edit view
  leaves it unchanged, per ruling 4. Its I5c counterpart: a staff POST to the
  attestation admin form cannot set `is_disputed` / `disputed_at`.
- **A zero-tag attested profile still appears in the browse listing** and is absent
  from a capability search, per decision 6. This fails if the browse query is ever
  rewritten to anchor on `CapabilityTag`.
- **The third empty state renders its own copy** — a zero-tag profile's capability
  slot shows the "no attested capabilities yet" state, asserted on the rendered
  response, not merely inferred from the profile appearing in results. Kept separate
  from the bullet above for the same reason the two dispute claims are separate: one
  test covering both would pass while either half was broken.
- **A malformed *or out-of-range* `?page=` does not 500**, per decision 4 — both
  cases, since `get_page()` handles them by different routes.

#### Role sequence and seats

Full dispatch for all five, per the plan's existing designation. **I5c is the hazard
zone** — it modifies the queryset that enforces attestation immutability and governs
dispute propagation to a public surface — so it takes the hazard-zone builder seat,
runs Verifier-first (characterization tests pinning the existing
`ImmutableAttestation` behaviour before the guard is extended, as I3c did for the
consent seam), and the Verifier-adversary seat stays separate, having found a real
defect in six consecutive milestones. A hand-driven walkthrough is part of the
definition of done for I5b and I5e, and for I5e it must include **an anonymous
browser with no session at all** — the state the whole initiative exists for, and the
one an authenticated developer never stumbles into by accident.

#### Plan review history

- **Iteration 1 (2026-07-29): Planner-adversary REJECT.** It verified all five
  reconnaissance findings against the code and found no factual errors, so the
  rejection was about what the plan concluded from them. Two blockers: no ranking
  algorithm was specified (a product decision the plan had silently left to the
  builder), and the promoted Refit fix was incomplete while being presented as
  complete. Lesser findings: the two dispute claims in the test strategy were
  conflated, the sequencing justification overstated inter-milestone dependency, the
  parallelizable-with-I1/I2 note was stale, and `site_header.html`'s coupling to
  I3c's tests was unrecorded.
- **Orchestrator verification of the blocker, before revising.** The adversary's
  claims about `IMMUTABLE_FIELDS`, `AttestationQuerySet.update()` and the test
  suite's direct `save(update_fields=...)` writes were each confirmed by reading the
  code rather than accepted on report. That pass also produced two facts the
  adversary had not raised: locking the admin fields would remove a deliberate
  capability, and a bare admin field edit yields a two-model inconsistency because
  `_tag_dates_by_name` filters on `Project.status` as well. Both are now in finding 3.
- **Human rulings taken to close the blockers:** ranking (ruling 3) and handle
  mutability (ruling 4).
- **Iteration 2 (2026-07-29): Planner-adversary REJECT, all findings accepted
  without dispute.** It re-verified all ten cited code locations as exact, confirmed
  both iteration-1 blockers genuinely closed, and explicitly judged the five-milestone
  split not over-engineered. Three new findings, all now fixed: (blocker) the I5c
  write-path matrix structurally could not catch a regression of layer 3's read-only
  admin fields, since a blocked write trivially satisfies "the cache is still
  correct" — closed by adding a direct hostile-POST assertion; (should-fix) a
  zero-`CapabilityTag` profile would be erased by a tag-anchored query — closed by
  decision 6; (nit) `Paginator.page()` would 500 on `?page=abc` — closed in decision
  4. It also deliberately checked for a fourth `Project.status` bypass and reasoned
  it out of scope, since no production code path or staff UI reaches it.
- **The bounded two-iteration loop was spent at iteration 2.** Both positions agreed,
  so there was no deadlock to escalate; the human was asked whether to spend a third
  pass on the three unreviewed fixes, and ruled yes.
- **Iteration 3 (2026-07-29): fresh-context Planner-adversary, scoped to the three
  fixes only — ACCEPT.** Deliberately dispatched fresh rather than resuming the
  iteration-2 adversary, since an adversary asked whether its own findings were
  addressed is prone to agree. It independently re-verified the data-model claims,
  confirmed `nulls_last` is portable across this project's backends and that `handle`
  uniqueness makes the browse ordering total, and found no over-correction. Three
  precision findings, all fixed: the hostile-POST assertion needed to require the POST
  *succeed* or it could pass vacuously; and two test-strategy bullets had drifted from
  the decisions they summarize (dropping the out-of-range `?page=` case, and omitting
  decision 6's third empty state entirely). The pattern worth remembering: the
  decisions were right each time and the canonical test list was what lost fidelity —
  and the test list is what builders work from.

#### I5b dispatch decisions (2026-07-29)

Three things the plan left open that would have forced a builder to guess.

- **The entry point is the owner's own record page**, `templates/surface/record/detail.html`,
  beside the existing visibility control — the freelancer already manages their public
  presence from there, and the nav reaches it via "Record". This deliberately avoids
  `templates/surface/partials/site_header.html`, which **I5e claims**; had I5b added a
  nav link, two milestones would own that file and finding 6's I3c coupling would be in
  play twice.
- **The edit URL carries no handle or pk.** The profile is derived from `request.user`,
  so there is no object to authorize against and object-level authorization bugs are
  structurally impossible rather than merely tested for. Login required.
- **Boundary amendment:** add `templates/surface/record/detail.html` (unclaimed by any
  other milestone) and `static/css/app.css`. The latter is also listed under I5e; that
  is safe **only** because these milestones are strictly sequential, and it would be a
  dispatch defect if they ever ran concurrently.

The form offers `display_name`, `headline`, `bio`, `location`, `website_url`. `handle`
is absent as a field entirely, and the hostile-POST test from the plan's test strategy
proves the omission is enforced rather than incidental. The view calls
`set_profile_details` and maps its `ValidationError` onto form errors; per the I5a
carried note it must not call `profile.save()` on that instance itself.

**Amendment from the Implementer-adversary (accepted).** The view deliberately lets the
service's `ValueError` propagate, on the reasoning that a valid form can never trigger
it. That reasoning was verified correct today, but it rests on an **unenforced coupling**:
the form's field set must keep matching `_PROFILE_DETAIL_FIELDS` exactly, and
`display_name` must remain the only required field. Nothing pinned that at either end, so
I5b's Verifier owes a test asserting the form's declared fields equal the service's
allowlist — turning a comment-level assumption into a failing test if anyone breaks it.

#### I5c dispatch decisions (2026-07-30)

Recon re-verified every code location the milestone touches. One plan correction and
four decisions a builder would otherwise have to invent in a hazard zone.

- **Correction to finding 3's citations.** It cites `ledger/tests.py:2825` and `:2852`
  as the direct dispute writes. Those line numbers have drifted — today they are
  `payload`/`payload_hash` immutability tests. The actual direct dispute writes are
  `ledger/tests.py:3079-3080` (`save(update_fields=("is_disputed",))`) and `:3105-3107`
  (both markers). Finding 3's substance is unaffected and was re-verified: the three
  bypass paths are real, and `AttestationQuerySet.update()` at `ledger/models.py:192-199`
  still blocks only `IMMUTABLE_FIELDS`, which excludes both dispute markers.
- **Decision C1 — the `post_save` receiver recomputes unconditionally, on create and
  update alike, with no change-detection predicate.** Any predicate is somewhere a false
  negative can hide, and a false negative here is precisely the bypass this milestone
  exists to close. Keying off `update_fields` was rejected outright: it misses a plain
  `.save()` that changed dispute state. `recompute_capability_tags` is idempotent by
  construction (delete then rebuild), and **no query-count test covers a signing or
  dispute path** — verified, the only `assertNumQueries` is a portal GET
  (`surface/tests.py:5934`) and the `CaptureQueriesContext` blocks cover acceptance
  steps and `canonical_payload`. The honest cost, stated so the adversary can weigh it
  rather than discover it: `amend_attestation` will now recompute three times
  (`is_current` flip, amendment create, explicit call), all inside one transaction with
  the last authoritative. Wasteful, correct, and cheap at MVP scale.
- **Decision C2 — the same unconditional rule applies to `AttestationQuerySet.update()`**,
  for symmetry and for the same no-predicate reasoning. Affected profile ids are
  captured **before** `super().update()` runs, and update-plus-recompute is wrapped in
  `transaction.atomic`.
- **Decision C3 — the receiver lives in `ledger/services.py`, connected from a new
  `ready()` in `ledger/apps.py`.** No `signals.py`: the plan's boundary does not include
  one, and derivation belongs in services per `dept_ledger.mdc`. `INSTALLED_APPS` holds
  the bare string `"ledger"` (`config/settings.py:39`), so Django auto-selects
  `LedgerConfig` and `ready()` will run. **A test must assert the receiver is actually
  connected, not merely defined** — a disconnected receiver is the silent failure this
  layer is most likely to ship with, and every behavioural test would still pass via
  layer 1 or the services.
- **Decision C4 — `models.py` imports `recompute_capability_tags` at function scope.**
  `ledger/services.py` imports from `ledger/models.py` at module level, so the reverse
  import at module scope is circular. This is a known trap, not a style preference.

#### I5b execution log (2026-07-29)

Owning department Surface, full dispatch. Suite **387 → 408 green**, ruff clean on every
file this milestone touched.

- **Implementer-builder (Composer 2.5) → Implementer-adversary (Sonnet 5): ACCEPT**, two
  nits and one forward-looking should-fix. Its most valuable act was a **refusal**: asked
  whether the three new fields displaying nowhere was a defect it should close, it argued
  both sides and concluded the display decision belonged to the plan, not to a builder
  improvising a public-surface layout. That was correct, and it produced decision 7.
- **Orchestrator reverted unauthorized cleanup, twice — and got it wrong the first time.**
  The Implementer had run what amounts to `ruff --fix` on `surface/views.py`'s import
  block, pre-existing mess that is one of the three logged `I001` Refit candidates.
  Verified it was already failing at HEAD and reverted it. **The Compliance Gate then
  caught that the Verifier had done exactly the same thing to `surface/tests.py`**, which
  the orchestrator had missed while describing the diff as "purely additive" — true of
  `views.py`, not of `tests.py`. Reverted that too, so both `I001` candidates stay open
  rather than being closed by stealth in an unrelated commit. Lesson: when reverting a
  class of drive-by change, check **every** file in the diff for it, not the one where it
  was first noticed.
- **Verifier-builder (Composer 2.5): 19 tests → Verifier-adversary (Grok 4.5): REJECT.**
  Ten mutations, one at a time, against hashes the orchestrator verified byte-exact
  afterwards. Most of the harness held. The blocker it found is the best catch of the
  initiative so far: it mutated the view to resolve the profile from `request.POST["handle"]`
  **with a fallback to `request.user`**, and *both* the cross-user isolation test and the
  hostile-handle test stayed green — because the isolation test never posted a foreign
  handle and the hostile test posted a handle that does not exist, so the fallback was
  indistinguishable from correct behaviour. The exact authorization bug the no-pk-in-URL
  design exists to prevent was invisible to the suite. Closed by a test where one
  freelancer posts another's **real** handle and both halves are asserted: the victim
  unchanged **and** the actor's own update still succeeding, so it cannot pass merely
  because the request failed.
- **Second finding, also real:** removing the view's `except ValidationError` broke no
  test, because the form's constraints mirror the model's and reject bad input before the
  service is reached. The service-to-form error bridge was therefore untested. Now pinned
  by patching the service (a collaborator, not the subject) to raise. A vacuous
  "response contains no `Traceback`" test was replaced by these, which cover the same
  intent on the path that can actually raise.
- **Walkthrough: a false positive, and the second of its kind in this project.** The
  browser agent reported "critical: form validation gives no user feedback". Investigated
  rather than accepted: a Django `URLField` renders `<input type="url">` and a required
  field renders `required`, so the **browser's own HTML5 validation blocked the submit
  client-side and the server was never reached**. Proved it by driving the same two POSTs
  through the Django test client against the dev database, which returned 200 with a
  rendered `errorlist` containing "Enter a valid URL" and "This field is required" and no
  traceback. Real users do get feedback, as a native tooltip. **Lesson for future
  walkthroughs: a browser agent cannot see native validation bubbles, so "nothing
  happened" on submit means client-side validation fired, not that the server is silent.**
- Everything else in the walkthrough passed, including the two that matter most: clearing
  only `location` left the other four fields intact, and an anonymous request to
  `/profile/edit/` redirected to login rather than rendering or erroring.
- Gate (fresh-context, Opus 5): **PASS**, with the `tests.py` reorder above plus two
  advisories. The substantive one: **no test asserted `is_public` was still `False` after
  a successful edit.** The invariant held by construction, but nothing would catch a future
  widening of the service allowlist from this surface — which is the precise failure the
  strict opt-in ruling exists to prevent. Now pinned in the valid-POST test alongside
  `handle`, so both invariants this milestone must not break are asserted where the write
  actually happens. Also noted non-blocking: a freelancer editing an unpublished profile
  is redirected to a page framed as their public record. That is deliberate — it is the
  owner's private preview and where the publish control lives — and is recorded here so it
  stays a choice rather than becoming an accident.

#### I5a execution log (2026-07-29)

Owning department Ledger, full dispatch. Suite **375 → 387 green**, ruff clean on all
four touched files.

- **Human granted the schema authorization** for exactly `bio`, `location`,
  `website_url` before dispatch. Migration `0005` contains three `AddField`
  operations and nothing else.
- **Implementer-builder (Composer 2.5) → Implementer-adversary (Sonnet 5): REJECT,
  one blocker.** `set_profile_details` defaulted absent keys to `""` and included
  them in `update_fields`, so a caller omitting `bio` silently **persisted the
  erasure** of an existing bio. The adversary correctly judged this worse than the
  silent `handle` ignore the builder had been told to reject, since it destroys data
  rather than doing nothing. It also found unknown kwargs were swallowed, where
  `_safe_signature_meta` in the same file already establishes the allowlist idiom.
- **Orchestrator ruling on the fix.** The adversary offered three resolutions and
  asked the builder to choose; that choice was the orchestrator's. Ruled **partial-
  update semantics**: absent key means untouched, empty string means cleared, unknown
  keys rejected, `handle` keeps its own distinct error per ruling 4. Chosen over
  documenting full-replace because a docstring does not stop the next caller — a form
  that fails to render a field now leaves it alone instead of erasing it. Django posts
  cleared fields as empty strings, so "cleared" stays distinguishable from "absent".
- **Orchestrator caught one thing neither seat did:** `_PROFILE_DETAIL_FIELDS` came
  back as a `frozenset` that was then *iterated* to build `update_fields`, making the
  tuple's order non-deterministic. Changed to a tuple. This project has already been
  bitten once by a milestone relying on incidental ordering.
- **Verifier-builder (Composer 2.5): 12 tests added.** It also repaired a regression
  I5a caused: `ProfileIsPublicMigrationTests` rolled the schema back to `0002` and
  then inserted via the **live** `Profile` model, so the three new columns broke it.
  Fixed with the historical-model pattern `AcceptanceItemStateMigrationTests` already
  used. Note the builder first reported this as "pre-existing" — it was not; the suite
  was green at HEAD. An audit of the other two migration test classes found them
  clean, so no Refit candidate is owed.
- **Verifier-adversary (Grok 4.5): ACCEPT after mutation testing**, nine mutations
  run one at a time, every one failing the test it should. Because the work was
  uncommitted, the seat was explicitly forbidden from `git checkout`/`restore`/`stash`
  and given SHA-256 hashes of the three files to restore against; the orchestrator
  re-verified all three byte-exact afterwards. It found one real hole empirically
  rather than by reading: the unknown-key test passed even when the service persisted
  `is_public=True` **and then** raised, because the test never checked the hostile
  field. Closed by asserting `is_public` is still `False` after rejection — which
  matters more than it looks, since that field is the strict opt-in flag. It also
  showed the `0005` migration test ignored a `max_length` 120→500 model drift, now
  pinned on the historical field.
- **Note carried to I5b, not a defect:** after a `ValidationError` the caller's
  in-memory `Profile` still holds the rejected values. That is what a form needs for
  redisplay, but an I5b view must not then call `profile.save()` for some other reason
  on the same instance.
- **Carried to I5c by the Compliance Gate (advisory, non-blocking, accepted).**
  `ProfileAdmin` declares `list_display`, `search_fields` and `readonly_fields` but no
  explicit `fields`, so Django defaults to every editable field and the three new ones
  now render on the staff change form without anyone choosing that. Related and more
  substantive: **`handle` is not in `ProfileAdmin.readonly_fields`**, so ruling 4's
  immutability currently rests on the service and form layers only — an admin can
  still change a public record URL. That is pre-existing rather than introduced here,
  which is why it did not block, but it becomes materially worse once records are
  indexable, since a changed handle breaks inbound links and frees the old one. I5c
  already owns `ledger/admin.py` and is the admin-write-path milestone, so it absorbs
  both: add `handle` to `ProfileAdmin.readonly_fields` with a test, and set an explicit
  `fields` list. Logged rather than fixed now, per the cleanup doctrine.
- Gate (fresh-context, Opus 5): **PASS**. It verified the authorization field-for-field,
  confirmed the whole diff contains only two deleted lines (one reworded plan line and
  the one repaired test call) so nothing was weakened to make the suite pass, and ran
  the suite itself.

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
  admin dispute handling through services so tag recompute always runs —
  **SUPERSEDED 2026-07-29 by I5c**, which found this fix covers one of three write
  paths; do not action it standalone; (2) drop redundant db_index=True on
  Attestation.project FK
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
#### I1b-2 execution log (per-item criteria UI)

- Shipped: per-item submit, pull-back, suspend, resume and withdraw through the
  UI; `templates/surface/partials/criteria.html` now branches on **item state**
  instead of project status; the client review page separates the pending batch
  from already-approved scope; mid-project submission mints a fresh review
  token and email; the delete hole is closed through `delete_acceptance_item`
  and `acceptance_item_locked`. Suite 247 → 251.
- **The consent mechanism, as built:** `ClientApproveView` approves exactly the
  items currently in `submitted` state, derived server-side from the
  token-derived project. The page posts a fingerprint of `(item id,
  submitted_at)` pairs which must match exactly or the whole batch is refused.
  No token or schema change was needed, which was the point of choosing it.
- Implementer-adversary (Sonnet) REJECT then satisfied. It confirmed by
  executed attack that a crafted POST cannot widen the batch and that criterion
  text cannot drift while an item is submitted. Blocker fixed: a null
  `submitted_at` — reachable because admin can edit item state — crashed both
  client endpoints with `AttributeError`.
- **Orchestrator ruling on that blocker:** coerce the null to a sentinel and
  keep the item **in** the fingerprint, rather than filtering it out. Filtering
  would have converted a visible crash into a silent consent hole, because the
  server-derived batch would still approve the item while the client's check no
  longer covered it. The Verifier-adversary later confirmed the sentinel
  assertion is the only thing that catches that substitution.
- That pass also produced I1a-2: see its log above. The race it found was the
  most serious defect of the session.
- Verifier-adversary (Grok) REJECT then satisfied, with a per-test mutation
  table. Two majors fixed: the server-side derivation rule had no failing test
  (approving fingerprint-parsed ids left the whole consent suite green), and
  the panel test asserted control *presence* without asserting illegal controls
  were absent, so rendering Suspend on a draft item passed.
- **Orchestrator ruling, promoted from a note to a fix:** the stale-fingerprint
  test depended on two UI submits minting different `submitted_at` values. The
  adversary rated it a red-flake risk rather than a false green. Promoted
  because this project has already lost time to exactly that
  `timezone.now()`-resolution flake on Windows, and a flaky *most important
  test in the milestone* trains a reader to rerun and shrug — corrosive when a
  green suite is the only evidence that exists. The pull-back and resubmit
  still go through the real UI; only the timestamp advance is now by
  construction. **That `+= timedelta(seconds=1)` is deliberate, not arbitrary.**
- Compliance Gate (Opus 5, fresh context) PASS, eight items, verified by
  running five throwaway probes rather than by trusting any report.

#### I1a-3 / I1b-3 — Closing the two consent-seam follow-ups (fast path)

Two separate tasks with two owning departments and two commits. Both are small
and precisely understood, so the orchestrator takes the builder seat, with the
floor intact: an adversary pass on the diff, tests that demonstrably can fail,
and a docs-impact check before each commit.

- **I1a-3 (owning: Ledger).** `delete_acceptance_item` reads `approved_at` in
  Python and then calls an unguarded `item.delete()`, so an approval committing
  between the two destroys the row and the approval with it. This is the last
  known instance of the bug class I1a-2 fixed. Fix: a filtered delete on
  `approved_at__isnull=True` whose affected-row count is checked, raising the
  existing `InvalidTransition` when it is zero, so the guard is enforced by the
  database rather than by a stale read. Boundaries: `ledger/services.py`,
  `ledger/tests.py`. No schema change.
- **I1b-3 (owning: Surface).** `ClientApproveView` catches `InvalidTransition`
  from a lost race, flashes "These criteria were already handled", and still
  renders `thanks.html`. Both halves are wrong: the criteria were not handled,
  they were pulled back, and the client is told an approval landed when the
  savepoint rolled back and nothing was recorded. Fix: return the existing
  `_review_response(..., stale_batch=True)`, whose notice already says exactly
  the right thing — the criteria changed, please re-review. Boundaries:
  `surface/views.py`, `surface/tests.py`.
- Why the second one matters more than its size suggests: every other part of
  this seam was built so a client can trust what the page tells them. A false
  confirmation is the one failure that cannot be detected by the person it
  misleads.

##### I1a-3 / I1b-3 execution log

- Both fixes landed as planned and were mutation-proven by reverting each and
  watching the matching test fail. The reverted Surface run printed the bug
  verbatim — "Thank you. The project criteria are recorded." beside a flash
  saying the criteria were already handled, over a rolled-back transaction that
  recorded nothing.
- Implementer-adversary (Sonnet) ACCEPT, six checklist items, having
  re-verified both mutations itself rather than trusting the report.
- **It found a latent hazard worth fixing rather than logging.** The two
  approval writes sat in one `try` but only `approve_acceptance_items` carried
  a savepoint. The adversary proved by probe that a failure in
  `approve_criteria` would commit the item approvals and *still* tell the
  client to re-review — a partial commit presented as nothing having happened,
  worse than the bug being fixed. It is unreachable today only because the
  view's `status == criteria_pending` check and `approve_criteria`'s own check
  read the same in-memory object, making the inner one a tautology. That is a
  landmine for whoever hardens `approve_criteria` with a real database guard,
  exactly as I1a-2 did for item transitions.
- Fixed rather than logged: one savepoint now spans both writes, so a client is
  either told their approval landed and all of it did, or told to re-review and
  none of it did. Mutation-proven — removing the savepoint leaves the items
  approved while the page asks the client to re-review.
- Two [minor] findings accepted as-is with reasons. The zero-row delete reports
  "cannot be deleted" even for a concurrently deleted row, but the only caller
  converts `InvalidTransition` to a bare 404 and no user or test ever sees the
  text, so a disambiguating query would buy a message nobody reads. And the
  mock in the race test stands in for a collaborator, not for the code under
  test; the adversary confirmed the two assertions together pin *review
  re-rendered with the stale notice* rather than merely "did not say thanks".

### I1b-4 — Honest state presentation (owning: Surface; hazard: client signature seam; full dispatch)

Both defects were found by the **first hand-driven smoke test this project has
ever had**, after 254 green tests had signed off on the same code. Neither was
visible to the suite: one is about what a page *says*, the other about a state
the tests never entered.

#### I1b-4a — The signing page misrepresents parked criteria

- `templates/surface/client/sign.html` line 19 renders
  `{% if item.is_passed %}Passed{% else %}Not passed{% endif %}` with no
  awareness of item state. A criterion that was **suspended** so delivery could
  proceed is therefore shown to the client as **"Not passed"**, visually
  identical to work that was attempted and failed — at the moment she applies a
  legally binding signature. Worse, a parked item whose result was never
  recorded has `is_passed = None` and also falls through to "Not passed", which
  is not ambiguous but simply false.
- This is the mirror of the Domain Law the public record obeys. That law forbids
  presenting a compromised attestation as clean; this presents a negotiated
  scope reduction as a delivery failure. **I misclassified it as cosmetic when
  the I1b-2 adversary first logged it**, on the reasoning that suspension had
  been admin-only — missing that I1b-2 itself is what made it routine.
- **Decision — parked status is visually primary, and the recorded result stays
  as secondary detail.** Do not simply hide the pass/fail: a criterion that was
  attempted, failed, and then parked is three facts, and suppressing the middle
  one is its own distortion. The client needs to see that it is not part of what
  she is signing for, and may see what happened to it.
- **Decision — label `suspended` and `withdrawn` distinctly here**, unlike the
  public record which deliberately uses one generic phrase. A stranger sizing up
  a freelancer needs only to know the scope moved; a signer needs to know
  precisely what she is and is not accepting.
- **Decision — `is_passed = None` must never render as "Not passed"** anywhere
  on this page, in any state.

#### I1b-4b — The freelancer is locked out during client review

- `templates/surface/partials/criteria.html` line 56 gates **every** per-item
  control on `project.status == "active"`, but a project sits in
  `criteria_pending` from the moment criteria are sent until the client
  approves. Through that entire window there is no Pull back, no Suspend, and
  no Edit — precisely the period in which a freelancer notices a mistake in
  what they just sent. Only the `submit` control needed that gate, and only to
  keep `draft` out; the implementation applied it to all five.
- **Decision — the gate becomes `criteria_pending` or `active`.** `draft` stays
  excluded, so the package Submit button remains the only path there, and
  delivered/signed stay excluded.
- **This is required rather than cosmetic, because gating submit to `active`
  alone is a deadlock.** `submit_criteria_for_approval` only runs from `draft`
  and `approve_criteria` only from `criteria_pending`, and `ClientApproveView`
  returns early when nothing is submitted. So a freelancer who pulled back every
  item during `criteria_pending` would leave the client unable to approve, the
  project unable to leave `criteria_pending`, and no per-item submit available
  to undo it. Verified against the services, not assumed.
- **Decision — the add-criterion form and create endpoint also include
  `criteria_pending`.** Pulling back and deleting every item would otherwise
  leave no criterion to resubmit and no route out of client review.

- Boundaries: `templates/surface/client/sign.html`,
  `templates/surface/partials/criteria.html`, `surface/views.py`,
  `surface/tests.py`, `static/css/app.css`. `ledger/**` is out of bounds —
  every state needed is already on the model.
- Test strategy: assert what a **client** sees for a suspended item, a
  withdrawn item, and an item with `is_passed = None`, including that "Not
  passed" is absent in those cases. For 4b, drive pull-back and edit through the
  UI while the project is `criteria_pending`, and add a regression test that
  pulls back every submitted item and proves the project can still reach
  `active` — the deadlock, pinned.

#### I1b-4 execution log

- Builder implemented both parts; suite 254 → 260.
- **Implementer-adversary REJECT (round 1)**, three `[major]`, each proven by
  execution rather than argued. It stripped the `is_passed is None` clause from
  only the `withdrawn` copy of a duplicated block and all nine signing tests
  stayed green, proving two copies of consent-critical rendering could drift
  silently. It read `app.css` and showed the parked label was `0.85rem` at
  normal weight against `<strong>` at `1rem` bold — so the label the plan
  required to be visually *primary* was in fact visually *subordinate*. And it
  showed the plan's unconditional `is_passed = None` decision was untested on
  the ordinary branch. Fixed by collapsing to a single result block, adding a
  `.badge.parked` modifier, and adding the missing case. Boundary extended to
  `static/css/app.css` by orchestrator ruling, since finding 3 was unfixable
  without it.
- **Orchestrator visual verification.** Because this milestone exists precisely
  because nobody had looked at a rendered page, the parked label was checked in
  a real browser on a real delivered project before acceptance, not just
  asserted in markup. It reads as the dominant element in the checklist.
- **Compliance Gate PASS**, having derived the reachable state set itself rather
  than accepting one: exactly eight state-and-result pairs can reach the signing
  page, all eight are labelled, and `None` cannot render as "Not passed" on any
  path. It found four independent single-edit mutations all caught by named
  tests, confirming the drift risk was eliminated rather than relocated.
- **The gate's most valuable finding was structural.** Asked whether folding the
  Verifier-adversary seat into its pass was defensible, it said yes for the
  template work and no for the gate change, and then demonstrated why: it
  widened the action gate to `delivered` and all 261 tests stayed green while
  suspend/withdraw/resume became reachable on a record under signature. **An
  Implementer-adversary reviews a diff, so the side of a boundary you did *not*
  move is invisible to it by construction.** Pinning the untouched side is the
  Verifier's characteristic question. Do not fold that seat again on a milestone
  that moves a gate.
- Orchestrator applied the same experiment to `criterion-create`, the one
  endpoint the gate's matrix omitted, and reproduced the gap a third time.
  Closed by extending the matrix to 72 checks over six statuses.
- Orchestrator upgraded the gate's `[minor]` zero-criteria trap to a blocker:
  pull-back made it possible to delete every criterion during `criteria_pending`
  and strand the project, which is the same deadlock 4b exists to close with one
  extra step, and pull-back is ours. Fixed by widening the add-criterion gate in
  template and view together.
- Final: **263 tests**, ruff at the 3 documented pre-existing `I001`.

### I1c — Freeze scope at delivery (owning: Ledger; consult: Surface; full dispatch)

Promoted from the Refit candidate below after the human ruled it ahead of I2.
Cross-department and it touches the Capability Record disclosure guarantee, so
full dispatch, and **the Verifier-adversary seat stays separate** per the
standing lesson from I1b-4 — this milestone adds a status gate, which is exactly
the case where an implementer-adversary is structurally blind.

#### The hole, stated precisely

`delete_acceptance_item` guards only `approved_at__isnull=True`, and
`AcceptanceItemDeleteView` applies no project-status check at all. An item that
went `submitted → suspended` never receives an `approved_at`, so it stays
deletable at every project status.

**The damaging window is `delivered` but not yet signed.** The public record's
disclosure is computed from the attestation payload, and that payload is built
from live items at `sign_attestation` time. So deleting a parked criterion while
the delivery record is out for signature means it never enters the payload, no
"Scope adjusted" badge is ever derived, and a narrowed delivery is published as
a clean one. Deleting *after* signing is harmless by comparison, because the
payload is already a frozen JSON snapshot — worth knowing so the fix is aimed at
the right window rather than at the scarier-sounding one.

It also lets the freelancer change what the client is looking at between opening
the signing page and signing it.

#### Decisions

- **Deletion is legal only while scope is still mutable: `draft`,
  `criteria_pending`, `active`.** Same set the create and action gates now use,
  so all three agree and there is one rule to remember rather than three.
- **Enforce in Ledger, not only in Surface.** This is domain law — the scope of
  a delivery that is out for signature cannot change — so the service must
  refuse regardless of caller. The Surface gate is then for a coherent UI, not
  for safety.
- **Make the guard atomic, in the style I1a-3 established.** Fold the project
  status into the same filtered delete rather than reading status and then
  deleting, so it cannot race a concurrent `mark_delivered`. A separate check
  would reintroduce exactly the TOCTOU shape I1a-2 and I1a-3 removed.
- Ruling 6 in `ROADMAP.md` says hard delete stays legal for items the client
  never approved. That still holds; this narrows *when*, not *which*. The ruling
  predates the I1b-1 disclosure guarantee and did not contemplate it.
- Already covered and not to be re-guarded: items that were ever approved,
  including `withdrawn` ones and suspended-from-approved ones, carry an
  `approved_at` and the existing guard stops them.

- Boundaries: `ledger/services.py`, `ledger/tests.py`, `surface/views.py`,
  `surface/tests.py`. No model or migration change — project status and
  `approved_at` are both already present.
- Test strategy: prove the damaging window directly rather than by proxy — park
  a criterion, deliver, delete it, sign, and assert the public record has lost
  its disclosure. That test must fail before the fix. Then pin the gate at both
  bounds, deletion still working in all three mutable statuses, and add the
  interleaving case where `mark_delivered` commits between a caller reading
  status and issuing the delete.

#### I1c execution log

- Builder wrote the end-to-end harm test first and **confirmed it failed before
  fixing anything** — `'Scope adjusted' not found` — so the hole was proven
  exploitable rather than argued. Suite 263 → 268.
- **Implementer-adversary ACCEPT.** Captured the emitted SQL and confirmed one
  statement carrying both guards, so the TOCTOU shape really is absent. Its most
  useful finding was that the Surface end-to-end test still passes with the
  Ledger predicate alone reverted, because the view gate intercepts first — the
  isolated proof of Ledger enforcement lives in `ledger/tests.py`, and the
  Surface test is a combined-layers pin. Worth remembering before anyone trims
  what looks like duplicate coverage.
- **Verifier-adversary REJECT**, and keeping this seat separate paid for itself
  immediately. `test_delete_rechecks_project_status_atomically_after_delivery_race`
  did not test its own name: a check-then-act implementation doing a *fresh*
  status read passed it, and only a stale-read version failed. It pinned "do not
  trust a stale related object", not "the predicate is in the DELETE".
  Fixed by asserting the successful delete emits **exactly one query** — a
  discriminator that is backend-independent, unlike asserting SQL text, which
  matters because this eventually runs on Postgres. Verified failing `2 != 1`
  against the adversary's own mutation. The stale-read test was kept and its
  name narrowed. Suite 269.
- **Compliance Gate PASS**, and it went well beyond the brief. Asked whether the
  fix protects the guarantee or only the one route we happened to find, it built
  a delivered project holding a parked criterion, snapshotted the canonical
  payload, and POSTed **all nineteen owner-facing routes with valid form data**
  so validation could not be what saved it. Every one refused; the payload hash
  was byte-identical after each; the sweep was repeated against an attested
  project. It also showed the protection is structural rather than lucky: every
  field `canonical_payload` reads is already frozen by a status gate at
  delivery, including the bulk `acceptance_items.all().delete()` inside
  `AiDraftConfirmView`, which is `DRAFT`-only. Single-item deletion was the only
  hole.

#### I2a execution log

- Model, migration `0004_acceptancestep`, the five planned services, nested
  payload, both pinned tests re-pinned deliberately. Golden digest moved
  `302fc585…dc19` → `e4694469…1b8c`, over a fixture extended to actually contain
  steps. Suite 269 → 286.
- **The builder found that adding a child model silently broke I1c**, which had
  been committed only hours earlier. Django's deletion collector cannot
  fast-delete a model that has *any* cascading relation — it is a model-level
  decision, not a row-level one — so `AcceptanceItem.objects.filter(guards).delete()`
  stopped compiling to one guarded `DELETE` and became SELECT-the-ids, then
  DELETE-by-id. The final delete carried no guard, reopening precisely the
  check-then-act race I1c existed to close. Nothing in the suite would have said
  a word, because the I1c test counted statements rather than inspecting them.
- **Orchestrator ruling: accept `_raw_delete`.** Steps are removed first by their
  own guarded statement, then the item is deleted through
  `queryset._raw_delete()`, which compiles the queryset's WHERE straight into the
  DELETE, all inside `transaction.atomic`. It is a private API, which is a real
  cost, but the alternatives were worse: reverting to collector behaviour
  reintroduces the race, and locking would have required changing
  `mark_delivered` outside this milestone's boundary.
- **The I1c test was rewritten rather than weakened, and came out stronger.** It
  no longer counts queries — a count is meaningless once a legitimate child
  delete exists — and instead asserts the *final* DELETE statement still carries
  `approved_at`, `ledger_project` and `status`. That pins the property that
  actually matters, and would have caught this regression where the original
  would not.
- **`_raw_delete` skips the collector, so cascade is now hand-written code.**
  A tripwire test pins `AcceptanceItem._meta.related_objects` to exactly
  `AcceptanceStep`, with a docstring telling whoever trips it to add their new
  child to the manual deletion or lose the cascade silently. Proven by
  temporarily adding a second child model and watching it fire.
- Plan clarification: the "two queries" rule meant two for the item-and-step
  fetch. Three total, including the pre-existing change-order query, is correct
  and is what the test pins.
- **Implementer-adversary ACCEPT.** Recomputed the golden digest in a standalone
  script importing no project code, and matched. Captured the emitted SQL to show
  `_raw_delete` carries the full WHERE, then proved the clause load-bearing
  against a row that fails the guard. Proved rollback two ways, including the
  guard-triggered zero-row path.
- **Verifier-adversary REJECT**, and the separate seat earned itself for the
  second milestone running. It pinned both bounds of the `is_done` gate across
  the whole item-state × project-status matrix and found two real holes:
  `delete_acceptance_step` had no stale-parent recheck — proven by a
  check-then-act implementation surviving all fourteen step-service tests — and
  **the rewritten I1c test pinned SQL substrings rather than predicates.** A
  DELETE carrying `approved_at`, `ledger_project` and `status` as decoy literals
  while filtering only on `id` passed it. The orchestrator had approved that test
  as "stronger than the one it replaced"; it was stronger against a naive revert
  and weaker against a decoy. Now fixed to assert the DELETE's **bound
  parameters**, captured via `connection.execute_wrapper` because
  `CaptureQueriesContext` does not expose them on SQLite.
- **Compliance Gate PASS**, with the best verification method this project has
  seen. It validated its digest recomputation by first reproducing the *old*
  literal from the pre-I2 item shape, then the new one — a self-checking method
  rather than a bare assertion. It then answered the question that actually
  mattered empirically: a **genuine pre-I2 attestation in the dev database**,
  written by that morning's hand-driven walkthrough, still verifies `True` under
  the new code, because `verify_payload_hash` rehashes the stored dict and never
  calls `canonical_payload`. No historical record is disturbed.
- The gate also confirmed the race was real at its source — `can_fast_delete()`
  genuinely flips to `False` once a cascading child exists — and improved the
  risk picture: the FK is `DEFERRABLE INITIALLY DEFERRED` with `PRAGMA
  foreign_keys` on, so a future child omitted from the manual cascade raises
  `IntegrityError` at commit rather than orphaning rows quietly. The failure mode
  is loud.
- Tripwire extended to assert no `pre_delete`/`post_delete` receivers exist for
  `AcceptanceItem`, since `_raw_delete` skips signal dispatch as well as cascade.
  Migration `0004` gained a reversibility test to match `0002` and `0003`.
- Final: **288 tests**, ruff at the 3 documented pre-existing `I001`.

##### Roadmap item — the clean escape from `_raw_delete`

Logged by the I2a gate. Setting `on_delete=models.DO_NOTHING` on `AcceptanceStep`
and declaring a database-level `ON DELETE CASCADE` instead would restore
`can_fast_delete` to `True`, letting a plain guarded `.delete()` compile back to
a single statement with a real cascade — removing both the private API and the
hand-written child cleanup. It needs custom migration SQL and a SQLite table
rebuild, so it was out of scope here. This is the shape to reach for if
`_raw_delete` ever becomes a maintenance problem, rather than inventing something
new under pressure.

#### I2b execution log

- Four step routes (`create`, `update`, `delete`, `done`), `AcceptanceStepForm`
  and an explicit-boolean done form, step display on the freelancer panel and
  both client-facing pages, and the fingerprint folded to include step content.
  Suite 288 → 310.
- The orchestrator's **I2b addendum was written before dispatch** specifically to
  avoid a third round of "the plan left it to the implementer". It held: no
  escalation was raised and no placement decision had to be invented mid-build.
- **My claim about the fingerprint's format-independence was wrong.** The plan
  asserted existing tests read the fingerprint out of the rendered form rather
  than reconstructing it. The builder checked instead of trusting and found
  `test_null_submitted_at_is_stable_on_review_and_approval` searched the raw value
  for the `__missing_submitted_at__` sentinel. Moving to a digest therefore cost
  real coverage.
- **Implementer-adversary REJECT** on exactly that: it mutated the sentinel to a
  different literal and then removed the sentinel design entirely, and the
  replacement test passed both times. A digest is 64 characters whatever the
  null-handling does. Fixed with a self-checking unit test that reconstructs the
  expected JSON using the literal sentinel and hashes it. It also measured an N+1
  on the approve path — the GET review path prefetched steps while
  `ClientApproveView`'s `select_for_update()` queryset did not — and found the
  create endpoint's cross-project scoping untested.
- **Verifier-adversary REJECT**, the third milestone running that keeping this
  seat separate has paid for itself. It confirmed both state matrices are
  genuinely load-bearing by moving every POST gate in both directions one state at
  a time, then found five holes:
  - **The fingerprint unit test pinned shape but not content.** The fixture used
    `is_done=False, order=1`, so hardcoding those literals in place of the live
    values passed. Omitting the fields failed, which is what made it look
    covered.
  - **`sorted()` was unpinned** — swapping it for `list()` stayed green, because
    every fixture was single-item or already in primary-key order.
  - **The signing page's outstanding-work notice had presence-only coverage.**
    `{% if True %}` passed: an unconditional notice would have shipped.
  - **The review page's "no progress" rule was pinned by vocabulary**, so a leak
    worded `Completed` / `Incomplete` passed.
  - **The Edit-step GET endpoint was entirely unpinned** and could return `Http404`
    unconditionally with every step test still green — meaning the control that
    `hx-get`s the inline form could have been dead. That is the I1b-4 defect shape
    exactly, caught this time before shipping rather than by a human clicking.
- All five fixed, plus two minors: the "Mark not done" label is now pinned in both
  directions, and a mobile `flex-direction` rule left dead by the `.criterion`
  flex→grid change was removed as builder-created mess.
- **Hand-driven walkthrough completed** across four seeded projects covering
  draft, criteria-pending, active and delivered. Step add/edit/delete exercised
  in place, done ticking exercised in **both** directions, duplicate step position
  produced a readable inline error rather than a server error, and a criterion
  created with no steps rendered consistently with its neighbours at both desktop
  and 390px widths. The signing-page trap — a criterion marked Passed with two of
  four steps outstanding — showed a bordered notice above and visually dominant
  over "Passed", while the all-done criterion beside it showed none.
- The first walkthrough report skipped the review-page and layout checks and was
  written in cheerleading prose; it was sent back for the missing observations,
  which then included computed colour, font weight, opacity and data attributes.
  **The orchestrator additionally verified the no-leak claim directly** by
  rendering the review page and inspecting its HTML: zero progress vocabulary, and
  every step an identical `<li class="pre-line">` despite two of them genuinely
  being done in the database.

- **Compliance Gate PASS**, verifying the consent seam the strongest way yet: it
  recomputed the fingerprint from the database in a separate process, without
  calling `_submitted_batch_fingerprint`, and got a value byte-identical to the
  hidden field on the live review page. It then mutated the preimage seven ways
  and confirmed every component is load-bearing. It independently confirmed the
  review page leaks nothing despite two of the four seeded steps genuinely being
  done, and fetched the signing-page trap from the running server rather than
  judging the template.
- The gate noted the property that makes inducement impossible and that nobody had
  stated outright: `_client_review_context` passes **the same materialised list
  object** to the fingerprint and to the template, so the steps that are hashed
  and the steps that are displayed are the same Python objects within one request,
  with no window between them.
- Dead context key `acceptance_items` in `ClientSignView._render` removed —
  builder-created residue from the switch to `acceptance_rows`, so builder mess
  under the cleanup doctrine rather than a pre-existing Refit target.

##### Roadmap item — bound step text length and step count

Raised by the I2a gate as "worth a thought at I2b", answered by the I2b gate as
acceptable to ship but worth logging. Both are correct and this is the reasoning.

The sharp edge is already gone: what worried I2a was unbounded content
round-tripping through a client-held form field, and the digest change fixed that
— the fingerprint field is 64 characters regardless of how much step text exists.
What remains is an authenticated freelancer inflating their own record, one row
per POST, with no unauthenticated path and no amplification, already capped per
request by Django's 2.5 MB `DATA_UPLOAD_MAX_MEMORY_SIZE` default.

Decisively: `AcceptanceItem.text` has been an unbounded `TextField` with unbounded
count since M1, already client-visible and already in the signed payload. Steps
are a second instance of an accepted exposure, not a new class of one, and capping
steps while leaving criteria uncapped would be incoherent.

The one argument with force is that a signed payload cannot be corrected
afterwards, so a bound is cheaper before production data exists than after. The
fix, when taken, is a form-layer `max_length` on `AcceptanceStepForm.text` — the
model `TextField` imposes none — plus a per-item count check through the existing
inline-error path. No migration, no service change. **Choosing the numbers is a
product judgement and belongs to a human ruling, which is why this is logged
rather than guessed.**

##### Refit candidate — `AcceptanceItemUpdateView` reads the lock, then saves

Found by the I2 Planner-adversary. `AcceptanceItemUpdateView.post` calls
`services.acceptance_item_locked(item)` and then separately calls `form.save()`,
leaving a window between the check and the write. It is the same read-then-write
family that I1a-2, I1a-3 and I1c each closed elsewhere with database-side
guarded statements.

Deliberately not fixed under I2, per the cleanup doctrine: it is pre-existing,
outside I2's boundary, and I2's own step mutations use the guarded idiom rather
than inheriting this one. The adversary was asked directly whether writing
guarded code beside an unguarded neighbour is incoherent enough to force the
issue, and ruled that it is not. Logged here so the deferral is a decision on
the record rather than an inline aside.

##### Roadmap item — `reopen_active` is a latent bypass of I1c

Found by the I1c gate. `reopen_active` in `ledger/services.py` lowers
`DELIVERED` back to `ACTIVE` with no guard, which by construction re-opens the
deletion window this milestone just closed. The gate proved the whole chain:
reopen, delete the parked criterion, re-deliver, sign — parked count in the
signed payload is zero and a narrowed delivery publishes clean.

**Not exploitable today**: it has no view, no URL, and no caller anywhere except
one Ledger test. Deliberately not fixed, because any real "reopen for more work"
feature needs its own design — not least what it means for an attestation that
may already exist — and guarding an unreachable service now would be
speculative.

The honest statement of where the guarantee rests: the guard itself is durable,
because it is a database-side predicate binding every caller, but **the set of
statuses it trusts is only as good as nothing lowering the status after
delivery.** The day someone wires a Reopen button, I1c is bypassed and no test
fails. Whoever builds that reads this first.

##### Refit candidate — `AcceptanceItemDeleteView` has no project-status gate — CLOSED by I1c

Found while verifying the gate's findings; **pre-existing, so logged rather than
fixed under I1b-4**, but it is the strongest candidate for the next milestone
because it has a Domain Law consequence.

The view applies no project-status check at all. Its only protection is the
`approved_at__isnull=True` guard added in I1a-3, which stops approved criteria
being destroyed. But an item that went `submitted → suspended` never receives an
`approved_at`, so that guard does not cover it. On a **delivered** project
awaiting the client's signature, the freelancer can therefore delete a parked
criterion — and with it the "Scope adjusted" disclosure I1b-1 exists to
guarantee, turning a narrowed delivery into an apparently clean one on the
public record.

I1b-4 widened the reachability of that state by allowing suspension during
`criteria_pending`, but did not create the path: suspending a submitted item in
an `active` project already produced it. Verified by reading the view and the
service guard, not assumed.

##### Follow-ups the I1b-2 gate surfaced (none blocking, ordered by seriousness)

1. **A client whose approval loses a race is still shown the thanks page.** If
   a freelancer pulls an item back while the client's approve POST is in
   flight, the batch correctly rolls back whole and nothing is recorded — but
   the client is told their approval landed. Nothing is corrupted and no
   approval is lost, so the gate did not block. It is still a false statement
   to a client on the consent seam, and it is the top candidate for the next
   pass.
2. **Guarded delete in the Ledger.** `delete_acceptance_item` reads
   `approved_at` in Python and then deletes unguarded, so an approval
   committing in between destroys the row. The window is two queries inside one
   request rather than a human's think time. It is the last known instance of
   the bug class I1a-2 fixed. **Correction to the earlier routing note: this is
   `ledger/services.py`, so it belongs to a Ledger follow-up, not to I1b-2,
   whose boundary excludes it.** A filtered delete on
   `approved_at__isnull=True` is the shape.
3. A draft item added to an `active` project is invisible to
   `MarkDeliveredView`'s pre-check, so the button looks live and the service
   rejects with a generic message rather than naming the unapproved criterion.
   Safe, imprecise.
4. Refit candidate: `templates/surface/client/sign.html` renders parked
   criteria as "Not passed". Harmless when suspension was admin-only; now that
   the suspend UI exists it is routine and misleading.

- Refit/M7 candidates (earlier): per-IP rate limiting on login
  request; CSRF-denial test (enforce_csrf_checks) for the confirm POST;
  unused show_console_hint context key (confirmed still set in views.py and
  referenced by no template);
  disputed_count/"withheld" UI notice not hash-tamper-aware; golden-hash
  test could pin an independent literal digest; CapabilityTag assertions
  could add explicit order_by; attestations signed pre-M6b carry no
  "skills" payload key (dev data only — repair path if ever needed is a
  client-re-signed amendment, never a payload edit).
- 2026-07-29: I3c executed, closing initiative I3. Suite 358 → 375, tests
  additions-only throughout. Implementer-adversary REJECT (blocker: the portal
  recorded the session email spelling into an immutable hashed payload; majors: a
  bare 404 for a client losing a signing race, and both carried-forward UI items
  either missed or satisfied only cosmetically). Verifier-adversary REJECT, its
  sixth consecutive real finding, all four gaps being mutations that left every new
  test green. Final acceptance caught a bare hash with no signature time on the
  post-signing page, repeating a defect I3b had already ruled on. Two walkthrough
  reports investigated and dismissed with evidence: the two-hats "blocker" (a
  repeat false positive from a stale freelancer session) and an em-dash encoding
  defect (a tooling artifact; the served bytes are clean UTF-8).
- Process lesson (orchestrator): **a browser walkthrough must reset identity state
  before testing two-hats, and establish the freelancer session before the client
  one.** This false positive has now cost two milestones' investigation time.
- Refit candidate (logged by the I3c gate, 2026-07-29): two pre-existing tests
  (`surface/tests.py` lines 461 and 5085) override the email backend to the console
  one to exercise the DEBUG-gated dev-link branches, and echo two full magic-link
  tokens to test stdout. Transient process output rather than a persisted log, and
  both branches are `settings.DEBUG`-gated, so it is not a constitution violation —
  but the assertions could stop rendering the token body. Noted, not touched.
- 2026-07-29: I3c characterization pass (Verifier first, per the charter's role
  sequence for a refactor of consent-critical code). Suite 352 → 358. The Verifier
  corrected the plan's own claim that every consent test drives its view through
  `reverse()`: four call `_submitted_batch_fingerprint` via `surface.views` and
  three patch `surface.views.services.*`, which turns two implementation choices
  from incidental into mandatory for the extraction. Both are empirically proven by
  a simulated wrong extraction that produced 7 failures. Verifier-adversary ACCEPT
  with two minors; one vacuous assertion fixed by making it load-bearing rather
  than deleting it, one soft count accepted under the I3a defence-in-depth ruling.
  I3c's boundary amended before dispatch: the plan sent the new portal views into
  `surface/views.py`, which imports the identity functions at module scope and
  would have undone I3a's guarantee for the two most consequential routes in the
  initiative.
