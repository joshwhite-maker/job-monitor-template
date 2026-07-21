# job-monitor

A small, self-hosted job monitor that polls company career pages directly
through their ATS (applicant tracking system) APIs, filters for roles that
match your criteria, optionally scores each one against your candidate
profile using Claude, and emails you a daily digest.

No scraping, no third-party job board, no rate-limit games — just the same
public JSON endpoints that power each company's own careers page.

## How it works

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

Supported ATS providers: **Greenhouse, Lever, Ashby, Workable.** These
four cover the large majority of VC-backed companies in the UK and US.

## Setup

### 1. Fork or clone this repo

### 2. Build your company list

Edit `companies.json`. Each entry needs:

```json
{
  "sector": "whatever label helps you group things",
  "name": "Display name",
  "ats": "greenhouse | lever | ashby | workable",
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
- Workable: `POST https://apply.workable.com/api/v3/accounts/{token}/jobs`
  with body `{"limit": 5, "details": false}`

If it returns JSON with job listings, the token's right. `verified` is
just a note to your future self — the monitor doesn't check it.

The example `companies.json` in this repo has a handful of well-known
companies across a few sectors, so you can see the shape and run the
monitor immediately. Replace it with your own list.

### 3. Set up your config

```bash
cp config.example.json config.json
```

Then edit `config.json`:

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
updated `seen.json` back to the repo so state persists between runs.
`.github/workflows/weekly-health-check.yml` runs weekly and emails you a
report of which companies in your list are still returning live data —
ATS tokens do occasionally rot (company changes provider, board gets
renamed, etc.) and this catches it before your monitor silently drops a
company.

Both also support manual triggering from the Actions tab.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | Main script — poll, filter, score, email |
| `health_check.py` | Weekly ATS liveness check |
| `companies.json` | Your target company list (example included) |
| `config.example.json` | Template — copy to `config.json` and edit |
| `.github/workflows/daily-monitor.yml` | Scheduled run |
| `.github/workflows/weekly-health-check.yml` | Scheduled health check |

## Notes

- This polls each company's own public API directly — the same data
  their careers page uses. No scraping, no auth bypass.
- `seen.json` is gitignored too. If you fork this and run it yourself,
  your own accumulated seen-IDs stay in your fork, not this template.
- Adding a fifth ATS provider is mostly a matter of writing one more
  `poll_x()` function following the same shape as the existing four and
  registering it in the `POLLERS` dict.

## License

MIT — do whatever you want with it.
