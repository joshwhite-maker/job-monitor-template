#!/usr/bin/env python3
"""
Grok Website Job Scout — for companies on web_watchlist.json that
deterministic ATS monitoring can't cover, asks Grok to look at the open
web (careers page, job boards, etc.) for current openings, and appends
findings to grok/web_job_discoveries.json as raw, unverified discoveries.

*** STATUS: UNTESTED. *** See grok_company_scout.py's module docstring —
the same caveat applies here: the xAI request shape is best-effort and
has not been exercised against a live API key.

Critically, this script works ONLY from web_watchlist.json entries where
status == "active" and the entry is due (last_checked is null, or
next_check has passed). It does NOT crawl the full company universe —
that's the entire point of the watchlist (see grok/README.md and the root
README's "design for scale" section). If the watchlist is empty or
nothing is due, this is a fast no-op.

This script NEVER touches companies.json or web_jobs.json. It only
appends to grok/web_job_discoveries.json with status "new", and updates
the watchlist entries' last_checked date (that update is bookkeeping
about what Grok looked at, not a canonical-data decision, so it's
reasonable for this script to own it — reconciliation still owns
`status`/`reason`/`priority` on those same entries).

Required environment:
  XAI_API_KEY   required — if unset, prints a message and exits 0.

Optional environment:
  GROK_MODEL           defaults to "grok-4"
  WATCHLIST_BATCH_SIZE  max companies to research per run, defaults to 10
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
CONFIG_FILE = ROOT / "config.json"
WATCHLIST_FILE = ROOT / "grok" / "web_watchlist.json"
DISCOVERIES_FILE = ROOT / "grok" / "web_job_discoveries.json"

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
MODEL = os.environ.get("GROK_MODEL", "grok-4")
BATCH_SIZE = int(os.environ.get("WATCHLIST_BATCH_SIZE", "10"))
TODAY = datetime.now(timezone.utc).date()

PROMPT_TEMPLATE = """\
You are a research scout, not a decision-maker. Look at the open web \
(the company's own careers page, and other public job listings if the \
careers page itself isn't machine-readable) for CURRENTLY OPEN roles at \
this company: {company}

Context on why this company needs manual web research rather than an API \
integration: {reason}

Candidate profile this is ultimately for (for relevance only — do not \
score or rank, just use it to filter out obviously irrelevant roles like \
entry-level individual-contributor engineering roles): {profile}

For each open role found, report ONLY what you can directly verify by \
looking at the actual page. Never guess or infer a field you don't have \
evidence for — use null instead. The job_url must be a real, specific URL \
to that individual posting, not a guess.

If, while looking at the careers page, you can identify what applicant \
tracking system (ATS) it's built on — for example the URL pattern reveals \
it's Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Personio, or \
Teamtailor — report that as ats_provider_guess and ats_token_guess (the \
company slug/token visible in that URL). Only report this if you can \
actually see it; null otherwise.

Return ONLY a JSON array (no markdown, no commentary), where each object \
has exactly these keys:
  title (string, required)
  location (string or null)
  employment_type (string or null)
  job_url (string, required, the specific posting URL)
  careers_url (string or null, the general careers page)
  discovery_method (string or null — e.g. "careers page", "LinkedIn jobs")
  evidence (string or null — quote/snippet confirming this is real and current)
  ats_provider_guess (string or null)
  ats_token_guess (string or null)

Return at most 10 roles. If you find none, return an empty array: []
"""


def load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)  # grok/ may not exist yet on a fresh fork
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def is_due(entry: dict) -> bool:
    if entry.get("status") != "active":
        return False
    next_check = entry.get("next_check")
    if not next_check:
        return True
    try:
        return date.fromisoformat(next_check) <= TODAY
    except ValueError:
        return True


def call_grok(prompt: str, api_key: str) -> list[dict]:
    resp = requests.post(
        XAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "search_parameters": {"mode": "auto"},
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError(f"expected a JSON array, got {type(parsed)}")
    return parsed


def to_discovery(raw: dict, company: str) -> dict | None:
    title = (raw.get("title") or "").strip()
    job_url = raw.get("job_url")
    if not title or not job_url:
        return None
    discovery_id = "wj_" + hashlib.sha1(f"{company}|{title}|{job_url}".encode()).hexdigest()[:12]
    return {
        "discovery_id": discovery_id,
        "company": company,
        "title": title,
        "location": raw.get("location"),
        "employment_type": raw.get("employment_type"),
        "job_url": job_url,
        "careers_url": raw.get("careers_url"),
        "source_url": raw.get("careers_url") or job_url,
        "discovery_method": raw.get("discovery_method"),
        "discovery_date": TODAY.isoformat(),
        "evidence": raw.get("evidence"),
        "ats_provider_guess": raw.get("ats_provider_guess"),
        "ats_token_guess": raw.get("ats_token_guess"),
        "status": "new",
        "notes": None,
    }


def main():
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("XAI_API_KEY not set — Grok Website Job Scout is a no-op. "
              "Set this secret to enable it (see grok/README.md).")
        sys.exit(0)

    watchlist = load_json(WATCHLIST_FILE, [])
    due = [e for e in watchlist if is_due(e)][:BATCH_SIZE]

    print(f"Grok Website Job Scout — {len(due)} of {len(watchlist)} watchlist "
          f"entries due (batch size {BATCH_SIZE}).")
    if not due:
        print("  Nothing due. No-op.")
        return

    config = load_json(CONFIG_FILE, {})
    profile = config.get("candidate_profile", "")
    discoveries = load_json(DISCOVERIES_FILE, [])
    existing_ids = {d["discovery_id"] for d in discoveries}

    total_added = 0
    for entry in due:
        company = entry["company"]
        print(f"  Researching {company} ({entry.get('reason')})...")
        prompt = PROMPT_TEMPLATE.format(company=company, reason=entry.get("reason", ""), profile=profile)
        try:
            raw_results = call_grok(prompt, api_key)
        except Exception as e:
            print(f"    [!] Grok API call failed for {company}: {e}")
            continue

        added = 0
        for raw in raw_results:
            discovery = to_discovery(raw, company)
            if not discovery or discovery["discovery_id"] in existing_ids:
                continue
            discoveries.append(discovery)
            existing_ids.add(discovery["discovery_id"])
            added += 1
        total_added += added
        print(f"    {len(raw_results)} roles returned, {added} new.")

        # Bookkeeping only — reconciliation still owns status/reason/priority.
        entry["last_checked"] = TODAY.isoformat()
        entry["next_check"] = (TODAY + timedelta(days=14)).isoformat()

    save_json(DISCOVERIES_FILE, discoveries)
    save_json(WATCHLIST_FILE, watchlist)
    print(f"\n  Total new job discoveries: {total_added}")


if __name__ == "__main__":
    main()
