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

Both prerequisites are landed: M7a (shared cache) and M7b (public-record opt-in).
**Initiative 1 is complete** as of 2026-07-26: I1a (Ledger state machine),
I1a-2 (guarded compare-and-swap transitions, added mid-flight after an
adversary proved a concurrent pull-back could erase a recorded client
approval), I1b-1 (public disclosure of parked criteria), and I1b-2 (per-item
UI and the client consent seam). Sequence from here is I2 (notes) → I3
(portal) → I4 (branding), with I5 (directory and search) parallelizable and
owning `surface/urls.py` plus the `base.html` nav at dispatch. The two
consent-seam follow-ups the I1b-2 gate raised are closed by I1a-3 and I1b-3.

**I1b-4 (2026-07-27) closed two defects that only a human walkthrough could
find.** The first hand-driven smoke test this project has ever had was run
against a suite of 254 passing tests, and found the client signing page
presenting a parked criterion as "Not passed" at the moment of signature, and
the freelancer locked out of every per-item control for the whole
`criteria_pending` window. The second was also a latent deadlock. Both are
fixed; suite is at 263. The signing-page defect had been logged as cosmetic —
that ranking was wrong, and the correction is recorded in `PLAN.md`.

**Standing lesson from that milestone:** an implementer-adversary reviews a
diff, so the side of a boundary you did *not* move is invisible to it. Any
milestone that moves a status gate needs the Verifier-adversary seat kept
separate, to ask what the gate still refuses and whether that is pinned.

**I1c (2026-07-27) closed that hazard.** Acceptance items could be deleted from
a project delivered but not yet signed, and because the disclosure is derived
from the payload built at signing time, a deleted parked criterion published as
a clean delivery. Deletion is now frozen outside `draft`, `criteria_pending` and
`active`, enforced in Ledger inside the same filtered DELETE. The gate proved
the guarantee holds against all nineteen owner-facing routes, and recorded
`reopen_active` in `PLAN.md` as a latent bypass for whoever builds a reopen
feature. Suite is at 269.

The sequence from here is I2 (notes) → I3 (portal) → I4 (branding), with I5
(directory and search) parallelizable and owning `surface/urls.py` plus the
`base.html` nav at dispatch. Milestone definitions and file boundaries are in
`PLAN.md`.

## Initiative 2 complete — per-criterion steps (goal 5)

Shipped as I2a (Ledger) and I2b (Surface). Each acceptance criterion can now
carry granular steps, and **the human ruled that the client sees them and that
they enter the signed record** — the middle option, client-visible but unsigned,
was put to the human and rejected, because it would have recreated in a new place
the same defect I1b-1, I1b-3 and I1b-4 each existed to close.

The rule that makes it safe is a split: step *wording* is editable only while its
criterion is a draft, so nothing a client approved can be reworded; the *done*
flag is writable only while the criterion is approved and the project active.
Those windows are disjoint from signing, so everything a client reads at the
signature is frozen when they read it. Steps are folded into the client-approval
fingerprint, which now returns a digest rather than raw JSON.

The old stub had this initiative blocked on I3 for client identity. **That was
wrong** — the dependency only existed for attributing notes to a client author,
and steps are freelancer-authored and client-*visible*. I3 was never a
prerequisite.

Notable: adding the child table silently broke I1c's delete guard, because Django
stops fast-deleting a model the moment it has any cascading relation, which moved
the guard out of the final statement. Caught mid-build, fixed with a guarded
`_raw_delete` and a tripwire, and logged in `PLAN.md` along with the clean
long-term escape from that private API. The separate Verifier-adversary seat
rejected both milestones and was right both times — including finding a signing-page
notice that would have shipped rendering unconditionally, and an edit control whose
endpoint could have been dead. Suite is at 310.

Sequence from here is unchanged: I3 (portal) → I4 (branding), with I5
parallelizable. One human ruling is outstanding and logged in `PLAN.md`: whether
to bound step text length and step count before there is production data, and if
so at what numbers.

## Initiative 3 underway — client portal (goal 2)

**Two human rulings reshaped it before planning.** The portal **hosts the actions**
rather than only displaying state, so clients will approve scope and sign from
inside it — a read-only portal was recommended and rejected. And a client email
appearing on projects belonging to different freelancers sees **one combined list**,
with the shared-inbox consequence accepted knowingly. The first ruling is what makes
this initiative large: it gives the consent seam a second doorway, so the plan
forbids duplicating that logic and instead extracts it behind a shared helper under
a no-existing-test-may-change invariant.

Three sequential milestones: **I3a** the sign-in foundation, **I3b** the project
view, **I3c** the portal-hosted actions. They must never be dispatched concurrently
— all three claim the same four files.

The plan needed **two Planner-adversary rounds, both REJECT**, which is the charter's
limit, so it went to the human rather than looping again. Round 1 found the most
dangerous milestone's central decision undecided — "the approval logic will be
shared" without saying how, where the obvious reading was provably impossible — and
found that nothing in the plan would ever tell a client the portal existed. Round 2
proved by reproduction that the fix for that would break a green test, and found the
checklist extraction was sharing the half that never drifted.

### I3a complete — client session foundation

A client signs in with a one-hour single-use magic link and gets a fourteen-day
session, absolute rather than sliding, listing every non-draft project for their
email across all their freelancers. **A client still gets no account**, which
required amending this project's own department rule — the old wording said client
access was tokenized and clients never get accounts, and the human approved the
replacement before the build.

The load-bearing constraint: the freelancer login form creates a `User` **and** a
`Profile` with a public handle for any email typed into it, before the link is ever
clicked. So portal auth lives in a module structurally forbidden from importing that
code, enforced by a test rather than by discipline. The Verifier-adversary seat
rejected this — its **fourth consecutive rejection and fourth real finding** — by
defeating all three identity guards at once with a dynamic import, and by showing the
absolute session cutoff could silently become a sliding window with nothing going
red. Suite is at 338.

Carried into I3c and logged in `PLAN.md`: on portal pages the client identity
currently reads as subordinate to the freelancer nav, which matters once signing
moves inside the portal.
