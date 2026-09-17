# Grok landing zone

This directory is a **raw research inbox**, not the canonical database.
Nothing in here is trusted until `scripts/reconcile_grok.py` has processed
it. See the root `README.md` for how this fits into the wider system and
how to set it up; this file covers what lives in `grok/` and the exact
contract each file follows.

Everything in this directory except this file is gitignored — it
accumulates as your own Grok Scouts and reconciliation run, the same way
`seen.json` already does for the base ATS monitor. A fresh fork starts
with none of it.

## Division of labour

```
FIND            ->  RECORD EVIDENCE   ->  VALIDATE, DEDUPE, VERIFY, PROMOTE
(Grok scouts)       (files in grok/)      (scripts/reconcile_grok.py)
```

Grok's only job is to look at the open web and write down what it found,
with evidence. It never touches `companies.json`, `web_jobs.json`,
`seen.json`, or its own file's `status` field beyond writing new entries
as `"status": "new"`. Reconciliation is the only thing that reads a
discovery, decides what to do with it, and updates its status.

**Do not have Grok invent a field it doesn't have evidence for.** Every
schema below allows `null`. A guess dressed up as data is worse than a gap
— reconciliation can act sensibly on `null`, it can't act sensibly on a
plausible-sounding fabrication.

## Files

### `company_discoveries.json`

Written by a Grok Company Scout — finds companies worth tracking (e.g.
from funding news, sector coverage) that aren't yet in `companies.json`.

```jsonc
{
  "discovery_id": "cd_<stable id, e.g. a short hash>",
  "company_name": "string, required",
  "description": "string or null",
  "sector": "string or null",
  "funding_stage": "string or null",
  "funding_amount": "string or null",
  "funding_date": "YYYY-MM-DD or null",
  "lead_investor": "string or null",
  "hq_location": "string or null",
  "source_publication": "string or null",
  "source_url": "string (URL) or null",
  "discovery_date": "YYYY-MM-DD, required",
  "discovery_reason": "string or null — why this came up",
  "evidence": "string or null — the specific fact/quote that justifies this entry",
  "status": "\"new\" when Grok writes it; reconciliation changes it to promoted | duplicate | watchlisted | rejected",
  "notes": "reconciliation writes its reasoning here — Grok shouldn't need to fill this in"
}
```

Reconciliation's handling, in order: skip if not `"new"` -> reject if
`company_name` missing or the wrong type -> mark `duplicate` if it
matches an existing `companies.json` entry or watchlist entry (fuzzy name
match) -> try a **deterministic** ATS auto-detect (Ashby/Greenhouse/
Lever/Workable, slug guessed from the name) before ever asking a human or
Grok to look further -> promote straight to `companies.json` if that
auto-detect positively confirms identity -> otherwise add to
`web_watchlist.json` (see below).

### `web_job_discoveries.json`

Written by a Grok Website Job Scout — finds specific open roles by
actually looking at a company's careers page or other public listing,
for companies on `web_watchlist.json` (see "How Grok should work" below).

```jsonc
{
  "discovery_id": "wj_<stable id>",
  "company": "string, required",
  "title": "string, required",
  "location": "string or null",
  "employment_type": "string or null",
  "job_url": "string (URL), required — the specific job posting",
  "careers_url": "string (URL) or null — the general careers page",
  "source_url": "string (URL) or null — where this was actually found, if different from job_url",
  "discovery_method": "string or null — e.g. 'careers page', 'LinkedIn jobs', 'company blog'",
  "discovery_date": "YYYY-MM-DD, required",
  "evidence": "string or null — quote/snippet supporting this being a real, current opening",
  "ats_provider_guess": "one of greenhouse|lever|ashby|workable|smartrecruiters|personio|teamtailor, or null",
  "ats_token_guess": "string or null — only if genuinely identifiable from the page (e.g. visible in a widget URL)",
  "status": "\"new\" when Grok writes it; reconciliation changes it",
  "notes": "reconciliation writes its reasoning here"
}
```

Reconciliation's handling: skip if not `"new"` -> reject if `company`,
`title`, or a well-formed `job_url` is missing or the wrong type ->
mark `superseded_by_ats` and drop it if the company already has a
working ATS integration in `companies.json` (the deterministic monitor
already covers it — a web discovery there is pure duplication, never
promoted) -> mark `duplicate` if the `job_url` is already in
`web_jobs.json` -> mark `needs_review` if the `job_url` doesn't actually
resolve (never silently promoted on an unconfirmed URL) -> if
`ats_provider_guess`/`ats_token_guess` are present and independently
confirmed, promote the **company** into `companies.json` too (this is
the main way a company graduates off web research and onto the cheap,
reliable deterministic monitor) -> promote the job into `web_jobs.json`
and record its id in `seen.json` (the same dedup ledger `monitor.py`
uses, so a rediscovery next run is recognised).

### `web_watchlist.json`

Companies that need Grok's web research because the deterministic monitor
can't cover them. Reconciliation both reads this (it's what the Web Job
Scout should work from) and writes to it (auto-adding companies it
couldn't place, auto-resolving ones a later reprobe manages to confirm).

```jsonc
{
  "company": "string, required",
  "reason": "no_usable_ats_found | ats_dead | unclassified_new | unsupported_ats | no_machine_readable_feed | unconfirmed_ats_candidate",
  "priority": "high | medium | low",
  "source": "grok_company_discovery | grok_web_job_discovery | watchlist_reprobe | health_check | manual",
  "added_date": "YYYY-MM-DD",
  "last_checked": "YYYY-MM-DD or null",
  "next_check": "YYYY-MM-DD or null",
  "status": "active | resolved | paused",
  "notes": "free text — reconciliation appends here rather than overwriting"
}
```

`reason: unconfirmed_ats_candidate` is a distinct, useful state: it means
reconciliation found something structurally real (an Ashby or Lever board
that responds) but the API gave no way to confirm it's the right company.
That's a strong lead for the Web Job Scout to check by eye, not something
to promote blind.

If you keep your own running log of companies you've manually looked at
and ruled out (an `EXCLUDED.md` or similar), it's reasonable to seed
`web_watchlist.json` with those entries by hand (`source: "manual"`) so
this system re-checks them automatically going forward instead of them
staying a dead end in a markdown file only you ever re-read.

## How Grok should work — the point of this whole design

**The deterministic monitor is always preferred.** If a company already
has `"verified": true` in `companies.json`, Grok should never be asked to
research its jobs — that's strictly worse (slower, costs API calls, less
reliable) than the existing ATS poller. Grok's value is entirely in the
gap the deterministic system can't cover.

Concretely:

1. **Grok Company Scout** finds new companies (funding news, sector
   coverage, etc.) and writes them to `company_discoveries.json`. It does
   NOT need to determine their ATS — reconciliation tries that
   deterministically first, and only escalates to the watchlist if that
   fails.
2. **Grok Website Job Scout** should pull its worklist from
   `web_watchlist.json` where `status == "active"` and
   (`last_checked` is null or `next_check` has passed) — **not** from the
   full company universe, and **not** by re-deriving which companies need
   it. Reconciliation maintains that list; Grok just works through it.
3. Reconciliation runs after both scouts (or on its own schedule) and is
   the only thing that writes to `companies.json` or `web_jobs.json`.

This keeps Grok's workload bounded by the size of the watchlist, not the
size of the company universe — see the root README for why that matters
at scale.

## Provenance

Anything reconciliation promotes carries a `source` field so you can
always answer "how did this get in here":

| `source` value | Meaning |
|---|---|
| *(absent)* | Was already in `companies.json` before you set this system up — your own manually curated list |
| `grok_company_discovery` | Found by the Grok Company Scout, ATS confirmed deterministically |
| `grok_web` | Found by the Grok Website Job Scout, ATS confirmed from its guess |
| `watchlist_reprobe` | Was stuck on the watchlist; a later deterministic reprobe found a working ATS |
| `manual` | Added by hand, e.g. seeded from your own prior research |

`companies.json` entries promoted by this system also carry
`source_url`, `discovered_date`, and `ats_confirmed_date` where available.
Your own pre-existing entries are never touched — this field is
additive, not a retrofit.
