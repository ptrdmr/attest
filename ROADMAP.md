# Roadmap — Next Initiatives (planned 2026-07-21)

This document captures the planning conversation for the next five product goals and the
agreed prioritization approach.

## The Request (top-level goals)

- **Searchable users:** once a user has signed up, they need to be able to make their
  profile public, so that their projects can be displayed there for future clients. It
  would act as a sort of supplementary portfolio or resume.
- **Portal page for customers who work with our freelancers:** they should be provided
  with a magic link via email that allows them to access their in-progress project. Then,
  once the user has completed the project, they should be able to review it there in its
  finalized form.
- **Branding:** allow our freelancers to customize the project's landing page with their
  customer's branding to help welcome the customer. This can be something as simple as a
  brand color(s), and a spot for a logo (probably near or on the "Project Brief" card).
- **Individual Acceptance Criteria submission:** we should be able to submit Acceptance
  Criteria individually. Most projects won't be a waterfall complete — they will sometimes
  be iterative, and other times be step-wise.
- **To-do list / notes per Acceptance Criteria:** if the user uses a broad piece of work
  for an acceptance criterion (e.g. "Must have a new page for current and past customer
  projects" on a freelance website build), there might be granular steps or notes the user
  may want to acknowledge there, for the purpose of representing that acknowledgement to
  their client.

## Assessment

All five goals are viable, but they are not five independent features — three of them
(portal, branding, criteria notes) stack on top of each other, and one (iterative
criteria) rewires the domain state machine everything else reads from. Order of
operations matters more than effort estimates.

### What the codebase already gives us

- A public page already exists — the Capability Record at `/u/<handle>/`. "Searchable
  profiles" is an extension (opt-in flag, richer profile fields, a directory/search
  view), not a new concept. Worth noting: **the record is currently public
  unconditionally** — there is no opt-in today, which the first goal implies there should
  be. That is quietly a privacy decision, not just a feature.
- Clients already get magic links, but they are **single-purpose, 14-day signed tokens**
  (review / sign / change-order). A portal is a different animal: a persistent,
  re-visitable entry point. That touches token/auth code, which the charter classifies as
  a **hazard zone → full dispatch, no fast path**.
- Criteria are already CRUD'd individually but **submitted and approved as one package**
  (`draft → criteria_pending → active`). Individual submission means per-item lifecycle
  states and changes to `submit_criteria_for_approval`, the client review flow, and
  potentially what the signed attestation payload snapshots. That is core Ledger
  territory with domain-law implications (signed payloads are immutable — so what does an
  attestation cover when criteria arrive iteratively?).
- There is uncommitted work in the tree (criteria panel merge, two-column detail layout,
  new tests). That should land or be shelved before any new initiative dispatches.
  **Resolved 2026-07-26:** landed as 69c2833; the tree is clean.

## Recommended Sequence

The dependency logic: **domain before venue, venue before decoration.**

### Step 0 — Land the in-flight work — DONE (69c2833)

Commit the criteria-panel changes (after the usual adversary pass + green suite). Small,
but it unblocks everything.

### Initiative 1 — Iterative criteria submission (goal 4)

*Ledger-owned, Surface consult, full dispatch (touches status machine + client review
tokens).* This goes first because it is the deepest cut: per-item submission states
change the model, the services layer, the client review page, and the attestation
payload semantics. Every later initiative that displays criteria (portal, notes) would
need rework if built on the package-submit model first.

### Initiative 2 — Per-criterion to-do/notes (goal 5)

*Ledger model + Surface UI.* Builds directly on Initiative 1's per-item granularity —
notes and their "acknowledged to client" display only make sense once an item has its own
lifecycle. These two could even be planned as one initiative with two milestones.

### Initiative 3 — Client portal (goal 2)

*Surface-owned, hazard zone (magic-link/token code) → full dispatch, hazard-zone builder
seat per the routing table.* The portal is the venue where the client sees in-progress
state, criteria acknowledgements from Initiative 2, and eventually the finalized project.
Doing it after the criteria rework means it renders the final domain shape once, not
twice.

### Initiative 4 — Client branding (goal 3)

*Surface-owned, small.* Brand color + logo slot on the portal / project brief card. It
decorates Initiative 3's page, so it goes after. Logo upload brings file-storage and
MIME/size validation concerns — and it is a natural **Pro-tier feature** if we want a
billing hook.

### Initiative 5 — Searchable public profiles (goal 1)

*Split: Ledger (Profile fields, opt-in flag — schema change) + Surface (directory/search
views).* Deliberately last in the sequence **but actually parallelizable**: its file
footprint (record templates, new directory views, Profile migration) barely overlaps the
others. If run concurrently with Initiatives 1–2, the only shared files to assign at
dispatch are `surface/urls.py` and `base.html` nav — per the charter, those must belong
to exactly one milestone's boundary.

## Decisions — RULED 2026-07-26

All four were put to the human and answered. The full ruling ledger, including three
further rulings the first one exposed, lives in `PLAN.md` under "Next initiatives".

1. **Iterative criteria semantics:** per-item submission *coexists* with package submit;
   the client approves batches of newly-submitted items; one attestation at the end.
   Per-milestone attestations were rejected for now — they are not a services tweak but
   a schema initiative, because `unique_current_attestation_per_project` and three
   services assume a single current attestation, and the capability-tag derivation would
   inflate skill counts if several attestations shared one project.
2. **Portal access model:** client identity keyed to `client_email` with its own
   magic-link flow, reusing the hardened M6a machinery. Blocked on the shared cache.
3. **Public profile default:** strict opt-in. Chosen partly because it is nearly free
   today — all attestation data is dev-only, so the migration runs against an empty
   table — and partly because the current behavior is worse than "opt-out": a profile
   with a guessable handle is auto-created for any email typed into the login form.
4. **Monetization placement:** no new billing gates until Stripe is real. Branding was
   the natural Pro candidate; the portal was rejected as a gate because it serves the
   freelancer's client, and charging for it penalizes the wrong party.

### Rulings the first ruling exposed

Letting criteria arrive during an active project raised three questions the original
five goals did not anticipate. Suspension in particular replaced an all-or-nothing
client verdict that would have reintroduced the very waterfall rigidity Initiative 1
exists to remove.

5. **Item states:** `draft → submitted → approved`, plus `suspended` (either party may
   park an item, reversibly) and `withdrawn` (terminal).
6. **Removal:** approved items are never hard-deleted; they become `withdrawn` and stay
   in the signed payload carrying that state. Hard delete remains legal only for items
   the client never approved.
7. **Submitted items lock** against freelancer edits. This closes a pre-existing hole:
   `criteria_locked` is false during `criteria_pending`, so today a freelancer can edit
   criteria while the client holds a live review link, and `ClientApproveView` checks
   nothing about what the client was actually shown.

## Next Maneuver

Sequence is now M7a (shared cache — DONE) → M7b (public-record opt-in) → I1a/I1b
(per-item criteria) → I2 (notes) → I3 (portal) → I4 (branding), with I5 (directory and
search) parallelizable and owning `surface/urls.py` plus the `base.html` nav at
dispatch. Milestone definitions and file boundaries are in `PLAN.md`.
