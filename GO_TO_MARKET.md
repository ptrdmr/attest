# Go-to-Market — Positioning, Channels, and Success Criteria

Planning document, drafted 2026-07-30. Companion to `PLAN.md` (build order) and
`ROADMAP.md` (feature sequence). This file answers two questions: how we take
Attest to market, and how we will know whether it worked.

Nothing here is a build authorization. Items that would change code are logged
as candidates for a Planner, and open product judgements are listed at the end
for a human ruling.

---

## 1. What we are actually selling

Attest contains **two products with different payback curves**, and conflating
them is the central marketing risk.

**Product A — scope control (immediate, self-interested, felt on project one).**
The client approves acceptance criteria in writing before work starts; change
orders block delivery until resolved; the client signs that each criterion was
met. The freelancer's benefit is realized *during* the project: fewer "just one
more thing" arguments, and a signed sign-off at the end. This value does not
depend on Attest being known or trusted by anyone.

**Product B — portable verified reputation (deferred, compounding, cold-start
dependent).** The Capability Record at `/u/<handle>/`, with SHA-256 record
hashes and derived skill tags. `PLAN.md` line 3 calls this "the moat," and long
term it is. But it is worth approximately nothing to a new user: a fresh record
renders "No verified skills yet," and its value depends on a third party — the
freelancer's *next* client — caring about a credential they have never heard of.

The current landing page leads with Product B:

```8:11:templates/surface/landing.html
      <h1>Signed proof you shipped</h1>
      <p class="landing-lead">
        Turn finished freelance work into a verified Capability Record your next client can trust.
      </p>
```

That headline sells a benefit the visitor cannot receive until they have
completed multiple projects, and which requires strangers to respect a new
credential. This is the standard failure mode of verified-credential products.

**Recommendation: lead with A, accrue B silently, and narrate B on retention.**
Product B is what we *build* toward and what makes the company defensible; it is
not what converts a visitor who has been burned by a client.

---

## 2. Prerequisites — we cannot market this today

Stated bluntly because it dominates sequencing: **there is no deployment, and a
production deploy today would be a dead end for every visitor.** Evidence:

1. **Production blocks project creation.** The billing stub defaults *off* when
   `DEBUG=0`, and there is no real gateway, so the entitlement gate on
   `ProjectCreateView` rejects everyone.

```148:151:config/settings.py
ATTEST_BILLING_STUB_MODE = (
    os.environ.get("ATTEST_BILLING_STUB_MODE", "1" if DEBUG else "0") == "1"
)
```

   Without either Stripe or a deliberate decision to run design partners with
   `ATTEST_BILLING_STUB_MODE=1`, a signup leads to a billing page reading
   "Checkout is not configured yet. Contact support."

2. **No email means no product.** The backend falls back to console output
   unless `EMAIL_HOST` is set. Every client interaction — criteria review,
   change orders, signing — is reachable only by emailed link. Until a real
   transactional sender with SPF/DKIM/DMARC exists, the client half of the
   product does not function.

3. **No serving stack.** No Postgres driver, no application server, no
   `STATIC_ROOT`/`collectstatic` strategy, no container or platform manifest,
   and `SECURE_SSL_REDIRECT=True` without `SECURE_PROXY_SSL_HEADER` will loop
   behind a TLS-terminating proxy. The `attest_cache` table must be created or
   magic links fail.

4. **No instrumentation of any kind.** There is no analytics, no record-view
   counting, no funnel events. Every success criterion in section 7 is currently
   unmeasurable.

5. **No real user has ever used it.** One internal hand-driven walkthrough
   (2026-07-27) is the entire usage history.

What *is* ready is the thing that is usually not: the domain logic. 310 tests
pass, immutability and consent races are adversary-reviewed, and the privacy
posture (private-by-default records, parked-scope disclosure) is unusually
honest for a reputation product. **The gap is entirely distribution surface and
operational plumbing, not correctness.**

---

## 3. Positioning options

Three coherent options. They differ in who we are fighting and how fast the
promise pays off.

**Option 1 — "Scope insurance" (painkiller-led).**
*"Agree the scope. Prove you shipped it. Get paid without the argument."*
Leads with Product A. Best day-one conversion; the pain is acute, recurring, and
already articulated by the audience in their own words. Cost: we land in the
category of freelance business tools (Bonsai, Indy, Moxie, HoneyBook) that
bundle contracts, invoicing, time tracking and CRM for a similar price. We have
none of those, which makes a second $29/mo subscription a hard ask.

**Option 2 — "Portable verified track record" (moat-led).**
The current headline. Strongest long-term and investor-facing narrative; worst
day-one conversion, because the value is deferred and depends on third-party
recognition we have not earned. Sustainable only once records are numerous
enough that prospective clients encounter them organically.

**Option 3 — "Proof of delivery in the AI era" (wedge-led).**
*"Your client signed off on exactly what shipped."* The README already positions
this — "AI-assisted or not." There is a live, unclaimed anxiety on both sides of
the freelance transaction: clients suspect deliverables are unreviewed AI
output, and freelancers fear "you just used ChatGPT" being used to discount an
invoice. A criterion-by-criterion, evidence-linked, client-signed record answers
both. Strongest content and PR hook, and genuinely differentiated. Cost: it is
trend-coupled, and careless execution reads as anti-AI, contradicting our own
"AI-assisted or not" stance.

**Recommendation: sequence them across the funnel rather than choosing one.**
Option 3 as the top-of-funnel hook (content, communities, PR), Option 1 as the
product-page promise and activation message, Option 2 as the retention and
expansion narrative once a freelancer has two or more attestations. They are
compatible in that order; they are incoherent if compressed into one headline.

---

## 4. Claim discipline — what we may and may not say

A trust product dies of over-claiming. What the artifact actually proves today:

- **Integrity:** the signed payload has not changed since signing. The record
  hash is verified at read time before public render.
- **Third-party assent:** a person controlling the client email address opened a
  purpose-bound link and typed a name into a confirmation form.
- **Scope honesty:** criteria parked or withdrawn before signing are disclosed
  publicly as "Scope adjusted," so a clean sweep cannot be faked by deletion.

What it does **not** prove, and what marketing may therefore not imply:

- **Not identity verification.** The signer is whoever held that mailbox. There
  is no KYC, and the client email is typed in by the freelancer.
- **Not an audit-trail-grade e-signature.** The Ledger accepts `user_agent`,
  `ip_hash` and `ip_truncated` in `signature_meta`, but the signing view passes
  an empty dict, so nothing is captured:

```1258:1261:surface/views.py
                project=self.project,
                client_email=self.project.client_email,
                client_name_typed=form.cleaned_data["signature_name"],
                signature_meta={},
```

- **Not legally binding.** We must never say "legally binding," "notarized," or
  "enforceable." Those words invite liability we cannot support and would be the
  fastest way to lose a trust product's trust.

Honest formulation: **"Your client confirmed each criterion was met, and the
record hasn't changed since."** That is defensible, specific, and still strong.

**Candidate for a Planner (marketing-relevant, Surface-owned):** populate
`signature_meta` with hashed or truncated IP and user agent. The Ledger already
validates exactly those keys and forbids raw IP storage, so the evidentiary
strength of every attestation improves without a schema change or a privacy
regression. This is the cheapest available upgrade to the core claim.

---

## 5. The distribution asset we already own — and are wasting

Attest has something almost no pre-launch product has: **every project compels a
real, non-user third party to open our email and use our pages.** Clients are
buyers of freelance services. They hire repeatedly. A client who has a clean
experience approving scope and signing off is the highest-intent lead we will
ever get, because they can *demand* Attest from the next freelancer they hire.

That channel is currently not just unused but actively harmful.

**The client emails are naked URLs.** Body is the link, nothing else:

```123:134:surface/views.py
def _send_review_link(request, project):
    """Email a fresh purpose-bound criteria review URL."""
    review_token = make_client_token(project, "review")
    review_url = request.build_absolute_uri(
        reverse("surface:client-review", kwargs={"token": review_token})
    )
    return _send_email(
        request,
        subject="Review project criteria",
        body=review_url,
        recipient=project.client_email,
    )
```

A subject line of "Review project criteria" and a body containing only a long
tokenized URL, from an unknown sender, with no freelancer name and no
explanation, is a spam-filter magnet that reads as phishing to a human who gets
past the filter. **This is the single highest-leverage marketing defect in the
codebase, and it presents as a deliverability bug rather than a marketing one.**

Three further gaps on the same surface:

- **Client pages never explain what Attest is** or that the process protects the
  client too. No context, no link, nothing a curious client can follow.
- **The signing confirmation wastes peak goodwill.** `signed.html` shows a
  timestamp and a hash at the exact moment a client has just confirmed the work
  was done well. It offers no copy of the record, no explanation, and no "you
  can ask any freelancer for this."
- **The public record is unverifiable by its reader.** It prints a SHA-256 hash
  and a "Verified" badge with no explanation of what was verified, who verified
  it, or how to check. To a prospective client, an unexplained self-published
  "Verified" badge is decoration at best and suspicious at worst.

**Candidates for a Planner, in leverage order:** (1) rewrite the four client
emails as real messages — who it is from, what is being asked, what Attest is,
why the link is long; (2) a public "how this is verified" explainer the record
links to; (3) a client-facing footer and a post-signature moment on the client
templates. Item 1 is a prerequisite for measuring anything about the client
funnel, because a spam-foldered email is indistinguishable from client apathy.

---

## 6. Channels, ordered

Assumes a very small team and no meaningful ad budget.

1. **Ten to twenty hand-recruited design partners, onboarded concierge.** Not a
   channel — the prerequisite for every other one. Note the hard recruiting
   constraint: the flow needs a client to act, so we need freelancers who are
   *about to start* a fixed-price project, not merely curious ones. Recruit
   where people announce new work.
2. **Communities where this pain is already discussed in the audience's own
   words:** r/freelance, r/forhire, r/web_design, Indie Hackers, designer and
   developer Discords, and groups of people who have left the big platforms.
   Contribute dispute and scope-creep experience; do not pitch.
3. **The client loop (section 5).** The only compounding channel we own. Must be
   fixed and instrumented before it can be counted on.
4. **Content and SEO on the dispute cluster:** acceptance criteria templates,
   "client won't pay the final invoice," scope-creep clauses, statement-of-work
   templates. Slow payback but high intent. The AI draft stub is already most of
   a no-signup public "acceptance criteria generator," which is a strong link
   asset and lead magnet.
5. **Partnerships with the all-in-one tools rather than a frontal fight** — only
   after activation is proven.
6. **Explicitly not paid acquisition,** until activation and retention are
   known. We have no LTV, no retention curve, and an activation event that
   depends on a third party. Paid spend before that is buying noise.

**Wedge audience recommendation:** fixed-price, single-deliverable engagements
in the $2k–$25k range where one dispute costs a month's income — website and
Shopify builds, brand identity, audits, technical writing, data and automation
work. Broad "all freelancers" targeting will not survive contact with the
activation requirement.

**Pricing defect found in the code.** The two SKUs are:

```5:6:surface/billing.py
PRO_PLAN_LABEL = "$29/mo Pro"
PROJECT_PACK_LABEL = "$39/project pack"
```

Pro grants unlimited projects for $29/mo; the pack grants **one** project for
$39. The pack is strictly dominated — any rational buyer takes one month of Pro
for $10 less and cancels. As priced, nobody should ever buy the pack. This needs
resolving before Stripe is wired, and the per-project SKU is arguably the one to
lead with, since it maps to the moment of felt risk and does not compete for the
subscription budget already spent on an all-in-one tool.

---

## 7. What success looks like

Success is defined on **countable events**, gated by cohort rather than calendar,
with one metric elevated above the rest.

**The one metric that cannot be faked: a signed attestation from a real client.**
It requires the freelancer to define criteria, a genuine third party to approve
them, work to actually happen, and that third party to return and sign. Signups,
projects created, and criteria written are all one-sided and prove nothing.

The funnel, each stage a discrete event to instrument:

| # | Stage | Event |
|---|---|---|
| 1 | Visit | Landing page view |
| 2 | Signup | Magic link confirmed |
| 3 | Project | Project created with a third-party client email |
| 4 | Submitted | Criteria sent to client |
| 5 | **Activation** | **Client approves criteria** — first third-party action |
| 6 | Delivered | Freelancer marks delivered |
| 7 | **Core value** | **Client signs — attestation created** |
| 8 | Published | Record made public |
| 9 | **Retention** | Second project started with no prompting from us |
| 10 | Thesis | Record viewed by a non-owner; freelancer reports it affected a deal |

Proposed thresholds — these are **judgement calls offered for a human ruling**,
not established benchmarks:

- **Signup → project with a real client email: ≥ 40%.** Filters curiosity from
  intent.
- **Criteria submitted → client approved: ≥ 70%.** The make-or-break ratio. If
  clients will not approve, nothing downstream exists. Below 50% means the client
  experience or the email is broken — see the caveat below.
- **Active project → client signed: ≥ 60%.** A large leak here means the
  delivery and signing step is too heavy, or projects simply do not finish.
- **Second project started unassisted within 60 days of the first attestation:
  ≥ 40%.** The real product-market-fit signal for a per-project tool.
- **Willingness to pay: ≥ 25%** of freelancers who complete one attestation pay
  for the second. **Currently unmeasurable** — the billing stub means the most
  important business question in the document cannot be answered until Stripe is
  real. Design-partner results must be read with that asterisk attached.

**Critical caveat on stage 5.** With emails as they stand (section 5), the first
cohort will almost certainly miss the approval threshold for reasons that have
nothing to do with market demand. **A low approval rate must not be read as
thesis failure until the emails are real messages and deliverability is
verified.** Confusing instrument error for market signal is the most likely way
to kill a working product.

Cohort gates, in order:

- **Gate 1 — the flow works.** Of the first 10 concierge design-partner
  projects, at least 5 reach a client signature. Until this passes, spend
  nothing on acquisition.
- **Gate 2 — it works unassisted.** Across 25 attestations from 15 or more
  freelancers, unassisted second-project rate at or above the threshold.
- **Gate 3 — the moat is real.** Non-owner record views are non-trivial, and
  multiple freelancers independently report sending their record to a prospect.

---

## 8. What failure looks like

Five failure modes, each with the leading indicator that identifies it.

1. **Client-side friction kill.** Clients never open or approve. *Indicator:*
   stage 4 → 5 below 50%. Most likely cause today is the bare-URL email, not
   the market. Fix and re-measure before drawing any conclusion.
2. **One-and-done curiosity.** Freelancers complete a single attestation and
   never return, meaning it was a novelty rather than a workflow. *Indicator:*
   unassisted second-project rate below 20%.
3. **Nobody reads the records — the moat is fiction.** *Indicator:* effectively
   zero non-owner record views, and no freelancer ever shares theirs. This is
   the most likely *quiet* failure: survivable, because the scope tool still has
   value, but it invalidates the moat narrative and changes the entire strategy.
   **It is invisible without instrumentation that does not exist.**
4. **Willingness-to-pay failure.** Users like it; nobody pays $29/mo alongside
   the all-in-one they already buy. *Indicator:* high satisfaction, near-zero
   conversion once a real wall exists. *Response:* per-project pricing, or move
   to the agency buyer.
5. **Wrong buyer.** The pain is real but sits with the freelancer while the
   benefit accrues to the client. *Indicator:* clients asking whether they can
   require this of their *other* contractors. This is failure of the B2C thesis
   and simultaneously the strongest available pivot signal — the buyer would be
   the company hiring contractors, not the freelancer.

A sixth risk is reputational rather than metric: **trust-artifact skepticism.**
A self-published "Verified" badge with no third-party verification path can read
as self-certification. Mitigated by the public verification explainer in
section 5 and by the claim discipline in section 4.

---

## 9. Pre-committed kill and pivot criteria

Written down in advance so sunk cost cannot argue later.

- **Fewer than 5 of the first 10 hand-held projects reach a client signature**
  → the flow itself is broken. No acquisition spend until it is fixed.
- **After 25 attestations across 15+ freelancers, unassisted second-project rate
  is below 20% *and* no freelancer has used their record with a prospect** →
  the reputation thesis is unsupported. Re-cut as a pure scope and sign-off tool
  sold per project, or stop.
- **Clients repeatedly ask to standardize Attest across their own contractors**
  → flip to B2B before scaling anything B2C.
- **Approval rate stays below 50% after the emails are rewritten and
  deliverability is confirmed** → the client burden is too high; the portal
  (I3) becomes the top priority rather than a roadmap item.

---

## 10. Instrumentation required before any of this is knowable

None of sections 7 through 9 can be evaluated today. The minimum, subject to the
constitution's prohibition on logging client PII:

- Funnel events for the ten stages, keyed to profile and project identifiers
  rather than to any client PII.
- Non-owner Capability Record views, distinguishing owner previews from genuine
  outside reads, with no visitor PII retained.
- Email send *and* delivery outcome. `_send_email` returns a boolean on SMTP
  exceptions and no test covers the failure path; delivery, bounces and spam
  placement are entirely invisible.
- Time from criteria submitted to client approval, and from delivered to signed
  — the two waits where a client silently abandons.

Any analytics choice is a **new dependency and therefore requires explicit
approval** per the constitution. A first pass can be built from existing model
timestamps (`submitted_at`, `approved_at`, `signed_at`) plus a small event
table, with no third-party service and no new package.

---

## 11. Open questions for a human ruling

1. **Positioning sequence** — accept the Option 3 → 1 → 2 funnel split, or lead
   with one message everywhere?
2. **Design-partner billing posture** — run the first cohort with
   `ATTEST_BILLING_STUB_MODE=1` and defer the pricing question, or wire Stripe
   first and learn willingness to pay from the first cohort?
3. **Pricing structure** — resolve the dominated project pack. Which SKU leads,
   and at what numbers?
4. **Success thresholds** — ratify, adjust, or reject the proposed rates in
   section 7. They are informed guesses and should be owned by a human.
5. **Analytics dependency** — approve a third-party analytics package, or build
   the minimal internal event table?
6. **Deployment target** — where does this run, and under what domain? Every
   client email link and every public record URL depends on the answer.

---

## Appendix — evidence base

- Test suite verified green on 2026-07-30: `Ran 310 tests ... OK`.
- Route, journey and access inventory taken from `surface/urls.py`,
  `surface/views.py`, `surface/forms.py`, `surface/auth.py`, `surface/tokens.py`,
  `ledger/models.py`, `ledger/services.py`, and `templates/`.
- Launch-readiness gaps cross-checked against `PLAN.md`, `ROADMAP.md`,
  `config/settings.py`, `requirements.txt`, and git history (32 commits,
  2026-07-18 → 2026-07-27, no deployment artifacts).
