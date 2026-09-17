# job-monitor

A small, self-hosted job monitor that polls company career pages directly
through their ATS (applicant tracking system) APIs, filters for roles that
match your criteria, optionally scores each one against your candidate
profile using Claude, and emails you a daily digest.

No scraping, no third-party job board, no rate-limit games — just the same
public JSON endpoints that power each company's own careers page.

Two layers, and the second is entirely optional:

- **The ATS monitor** (`monitor.py`, `health_check.py`, `companies.json`) —
  deterministic, cheap, reliable. Works standalone. This is most of what
  this README covers, and it's all you need if you're happy maintaining
  your own company list by hand.
- **The Grok web-research layer** (`grok/`, `scripts/`) — optional,
  finds companies and jobs the deterministic layer structurally can't
  reach (obscure ATS, custom careers systems, or companies you haven't
  added yet), and folds validated results into the same daily digest.
  Covered in its own section below — skip it entirely if you don't want it.

## How the ATS monitor works

1. `companies.json` lists the companies you want to track, their ATS
   provider, and their board token.
2. `monitor.py` polls each one, filters by keyword and location, and
   deduplicates against roles it's already seen (`seen.json`).
3. If you provide an `ANTHROPIC_API_KEY`, each new role is scored 1–10
   against a candidate profile you write yourself, with a one-line
   rationale. High-scoring roles get their full description fetched and
   an excerpt included in the email.
4. A GitHub Actions workflow runs this on a schedule and emails you the
   result. A second, weekly workflow checks all your company tokens are
   still returning live data, so you notice if one goes stale.

Supported ATS providers: **Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Personio, Teamtailor.** The first four cover the large
majority of VC-backed companies in the UK and US; the other three add
strong coverage for European scaleups and larger fintechs.

## Setup

### 1. Fork or clone this repo

### 2. Build your company list

Edit `companies.json`. Each entry needs:

```json
{
  "sector": "whatever label helps you group things",
  "name": "Display name",
  "ats": "greenhouse | lever | ashby | workable | smartrecruiters | personio | teamtailor",
  "token": "the board token",
  "verified": true
}
```

The board token is usually visible in the company's careers page URL, e.g.
`jobs.lever.co/vercel` → token `vercel`. If you're not sure, you can test
any token directly:

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs`
- Lever: `https://api.lever.co/v0/postings/{token}?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{token}`
- Workable: `https://apply.workable.com/api/v1/widget/accounts/{token}?details=true`
- SmartRecruiters: `https://api.smartrecruiters.com/v1/companies/{token}/postings`
  — note this returns HTTP 200 even for a token that doesn't exist
  (`{"totalFound": 0, "content": []}`). A real match includes a
  `content[].company` object; zero results with no company object isn't
  proof the token is wrong, just inconclusive.
- Personio: `https://{token}.jobs.personio.de/xml?language=en`
- Teamtailor: `https://{token}.teamtailor.com/jobs.json`

If it returns job listings (or, for SmartRecruiters, a `company` object
confirming the name), the token's right. `verified` is just a note to
your future self — the monitor doesn't check it.

The example `companies.json` in this repo has a handful of well-known
companies covering all seven providers, so you can see the shape and run
the monitor immediately. Replace it with your own list.

### 3. Set up your config

```bash
cp config.example.json config.json
```

Then edit `config.json`:

- `greeting_name` — used in the email's opening line ("Good morning
  {name} — here's your job-search digest for...")
- `role_keywords` — a job title must contain at least one of these
- `exclude_keywords` — a job title containing any of these is dropped,
  even if it matched a role keyword
- `location_keywords` — if the role has a location set, it must contain
  one of these (roles with no location listed always pass through)
- `candidate_profile` — free text describing you: background, target
  seniority, strengths, what to screen out. This is sent to Claude
  verbatim for scoring, so be as specific as you like.
- `min_score_to_highlight` — roles at or above this score get visually
  highlighted in the email

`config.json` is gitignored, so once you've filled in your own profile
it stays local (or in your GitHub Actions secrets — see below) and never
gets committed.

### 4. Set up email delivery

The monitor sends via Gmail SMTP using an
[app password](https://support.google.com/accounts/answer/185833) (not
your normal Gmail password — set up 2FA first, then generate one).

Any SMTP-reachable provider works if you adapt `send_email()` in
`monitor.py`; Gmail's just the path of least resistance.

### 5. (Optional) Set up Claude scoring

Get an API key from the
[Anthropic Console](https://console.anthropic.com/). Without a key, the
monitor still runs and emails you every new matching role, unscored,
sorted alphabetically by company.

### 6. Run it locally once

```bash
pip install requests
EMAIL_FROM=you@gmail.com \
EMAIL_TO=you@gmail.com \
EMAIL_APP_PASSWORD=xxxx \
ANTHROPIC_API_KEY=sk-ant-... \
python monitor.py
```

First run will have a lot of "new" roles since `seen.json` starts empty.
That's expected — it settles down after day one.

### 7. Deploy on GitHub Actions

In your repo: **Settings → Secrets and variables → Actions**, add:

- `EMAIL_FROM`
- `EMAIL_TO`
- `EMAIL_APP_PASSWORD`
- `ANTHROPIC_API_KEY` (optional)

`.github/workflows/daily-monitor.yml` runs on weekdays and commits the
updated `seen.json` / `web_jobs.json` / `digest_state.json` back to the
repo so state persists between runs. `.github/workflows/weekly-health-check.yml`
runs weekly and emails you a report of which companies in your list are
still returning live data — ATS tokens do occasionally rot (company
changes provider, board gets renamed, etc.) and this catches it before
your monitor silently drops a company.

Both also support manual triggering from the Actions tab.

**This is a complete, working job monitor on its own.** Everything from
here down is the optional Grok layer — skip it if you're happy curating
`companies.json` by hand.

## The Grok web-research layer (optional)

The ATS monitor above is excellent at companies where you can identify a
publicly queryable ATS. Its limitation is companies where:

- the ATS is obscure or unsupported
- the company uses a custom careers system with no public API
- jobs are only visible on the company's own website
- you simply haven't added the company yet

This layer solves that with **deterministic automation first, Grok only
where agentic web research provides a genuine advantage** — it never
asks an LLM to do something a cheap, reliable API call can already do.

```
INTERNET
  |
  +-- Structured ATS/API sources  ->  the deterministic monitor above
  |
  +-- Open web  ->  Grok Company Scout, Grok Website Job Scout
                          |
                          v
                    grok/ landing files
                          |
                          v
              scripts/reconcile_grok.py
                          |
                          v
          companies.json / web_jobs.json (canonical)
                          |
                          v
                  the same daily digest
```

**Grok's job is FIND → RECORD EVIDENCE → WRITE RAW DISCOVERY.**
**Reconciliation's job is READ → VALIDATE → DEDUPLICATE → VERIFY → PROMOTE.**
Grok never writes to `companies.json` or `web_jobs.json` directly, never
deletes a discovery, and never overwrites a verified ATS token just
because it found different information. If something's ambiguous,
reconciliation marks it for review rather than guessing — see
`grok/README.md` for the full contract each file follows, and
`scripts/reconcile_grok.py`'s module docstrings for exactly what gets
validated at each step (including two real false-positive patterns it
guards against: a provider that returns HTTP 200 for a token that
doesn't exist, and a blind slug guess that coincidentally matches an
unrelated company).

### What it adds to the email

Once this is running, the daily digest becomes a broader job-search
summary rather than just a list of matching roles:

- **Relevant roles** — unchanged, now also includes validated
  web-discovered jobs alongside ATS ones, each tagged with an `ATS` /
  `Web-discovered` badge.
- **New companies discovered** — companies Company Scout found and
  reconciliation has acted on (either promoted to full ATS monitoring, or
  added to the watchlist for further research), with whatever real detail
  Grok found: description, funding signal, date, source link.
- **Companies hiring — no matching role yet** — companies with real,
  evidenced open roles that don't happen to match your filters. Never
  shown without a specific role behind it — a company with no discovered
  job is never described as "hiring."
- **System activity** — a compact line count of what happened since the
  last digest: companies monitored, roles fetched, watchlist entries
  checked, new companies/jobs discovered, relevant roles surfaced.

All of this is generated from `companies.json`, `web_jobs.json`, and
`grok/web_watchlist.json` — never from Grok's raw, unreviewed output.

### Setup

1. **Get an xAI API key** from [console.x.ai](https://console.x.ai/) and
   add it as the `XAI_API_KEY` secret (same place as your other secrets).
   Without this, `grok-company-scout.yml` and `grok-web-job-scout.yml`
   still run on schedule but do nothing (they detect the missing key and
   exit cleanly) — so it's safe to leave the rest of this system in place
   even if you never set this up.
2. **Trigger `grok-company-scout.yml` once manually** from the Actions
   tab and inspect `grok/company_discoveries.json` by hand before trusting
   the schedule. The xAI request shape in `scripts/grok_company_scout.py`
   is written against xAI's documented chat-completions + web-search API
   — verify it still matches [xAI's current docs](https://docs.x.ai/)
   if it ever stops working; API surfaces move.
3. Same for `grok-web-job-scout.yml` — note it only researches companies
   already on `grok/web_watchlist.json` (seeded automatically as
   `grok-company-scout.yml` and reconciliation find companies with no
   usable ATS), so it'll have nothing to do until the company scout has
   run at least once.
4. `reconcile-grok.yml` needs no extra setup or credentials — it's pure
   Python, triggers automatically after either scout workflow completes,
   and also runs on its own daily schedule as a safety net. This is the
   only thing that writes to `companies.json`/`web_jobs.json` from Grok's
   output, so it's safe to leave running even before you've configured
   the scouts.

### Files this adds

| File | Purpose |
|---|---|
| `grok/company_discoveries.json` | Raw Company Scout output (gitignored — accumulates as you run it) |
| `grok/web_job_discoveries.json` | Raw Website Job Scout output (gitignored) |
| `grok/web_watchlist.json` | Companies needing web research, maintained by reconciliation (gitignored) |
| `grok/README.md` | Full schema and division-of-labour documentation (committed) |
| `web_jobs.json` | Canonical web-discovered jobs, promoted by reconciliation (gitignored) |
| `digest_state.json` | Tracks when the last digest was sent, for the email's "since last digest" sections (gitignored) |
| `scripts/ats_probe.py` | Deterministic ATS verification, hardened against real false-positive patterns |
| `scripts/reconcile_grok.py` | Validates, dedupes, and promotes Grok's raw discoveries |
| `scripts/grok_company_scout.py` | Calls xAI to find new companies |
| `scripts/grok_web_job_scout.py` | Calls xAI to research jobs at watchlisted companies |
| `scripts/test_reconcile_grok.py` | Regression tests — run with `python scripts/test_reconcile_grok.py` |

### Scaling note

`scripts/grok_web_job_scout.py` deliberately works from the watchlist,
not the full company universe — Grok's workload stays bounded by how
many companies you can't otherwise monitor, not by how many companies
you track in total. If you're tracking hundreds of companies, the
deterministic ATS monitor still does almost all the work; Grok only
picks up the gaps.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | Main script — poll, filter, score, merge in web-discovered jobs, email |
| `health_check.py` | Weekly ATS liveness check |
| `companies.json` | Your target company list (example included, covering all seven providers) |
| `config.example.json` | Template — copy to `config.json` and edit |
| `.github/workflows/daily-monitor.yml` | Scheduled run |
| `.github/workflows/weekly-health-check.yml` | Scheduled health check |
| `.github/workflows/reconcile-grok.yml` | (Optional layer) validates and promotes Grok's discoveries |
| `.github/workflows/grok-company-scout.yml` | (Optional layer) finds new companies |
| `.github/workflows/grok-web-job-scout.yml` | (Optional layer) researches jobs at watchlisted companies |

## Notes

- This polls each company's own public API directly — the same data
  their careers page uses. No scraping, no auth bypass.
- `seen.json` is gitignored too. If you fork this and run it yourself,
  your own accumulated seen-IDs stay in your fork, not this template.
- Adding an eighth ATS provider is mostly a matter of writing one more
  `poll_x()` function following the same shape as the existing seven and
  registering it in the `POLLERS` dict (and the equivalent `check_x()` in
  `health_check.py`, and `probe_x()` in `scripts/ats_probe.py` if you
  want the Grok layer to be able to auto-detect it too).
- Scheduled workflows on GitHub Actions are best-effort, not exact —
  avoid cron times exactly on the hour if you can, since every workflow
  scheduled for that same minute across all of GitHub queues at once and
  can drift under load. `daily-monitor.yml`'s default cron is already
  offset a few minutes for this reason.

## License

MIT — do whatever you want with it.
