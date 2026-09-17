#!/usr/bin/env python3
"""
Grok Company Scout — finds newly-fundable/newly-relevant companies via
Grok's web-search-grounded chat completions API and appends them to
grok/company_discoveries.json as raw, unverified discoveries.

*** STATUS: UNTESTED. ***
This was written without access to a live XAI_API_KEY, so the request
shape below (particularly the `search_parameters` field, which is xAI's
documented mechanism as of their 2025 "Live Search" release for grounding
a chat completion in real-time web results) has not been exercised against
the real API. Before relying on this in production:
  1. Confirm the endpoint/request shape still matches xAI's current docs
     at https://docs.x.ai/ — API surfaces move.
  2. Run it once manually (`python scripts/grok_company_scout.py`) with a
     real key and inspect grok/company_discoveries.json by hand.
  3. Only then trust the scheduled GitHub Action.

This script NEVER touches companies.json, web_jobs.json, or
web_watchlist.json — see grok/README.md for why. It only appends to
grok/company_discoveries.json with status "new"; scripts/reconcile_grok.py
does everything after that.

Required environment:
  XAI_API_KEY   required — from https://console.x.ai/
                 If unset, this script prints a clear message and exits 0
                 (a deliberate no-op, not a failure) so a workflow running
                 it doesn't hard-fail before the secret is configured.

Optional environment:
  GROK_MODEL    defaults to "grok-4"
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
CONFIG_FILE = ROOT / "config.json"
COMPANIES_FILE = ROOT / "companies.json"
DISCOVERIES_FILE = ROOT / "grok" / "company_discoveries.json"

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
MODEL = os.environ.get("GROK_MODEL", "grok-4")
TODAY = datetime.now(timezone.utc).date().isoformat()

REQUIRED_FIELDS = {"company_name", "discovery_date", "status"}

PROMPT_TEMPLATE = """\
You are a research scout, not a decision-maker. Find UK/EU companies in \
these sectors that have had a notable funding round, launch, or hiring \
signal in roughly the last 30 days: {sectors}.

Candidate profile this is ultimately for (for relevance only — do not \
score or rank, just use it to judge topical fit): {profile}

Do not include any company already in this list: {existing_names}

For each company found, report ONLY what you can directly verify from a \
real, citable source. Never guess or infer a field you don't have \
evidence for — use null instead. Do not include companies you are not \
reasonably confident are real, currently operating businesses.

Return ONLY a JSON array (no markdown, no commentary), where each object \
has exactly these keys:
  company_name (string, required)
  description (string or null)
  sector (string or null)
  funding_stage (string or null)
  funding_amount (string or null)
  funding_date (YYYY-MM-DD or null)
  lead_investor (string or null)
  hq_location (string or null)
  source_publication (string or null)
  source_url (string, the actual URL you found this at, or null)
  discovery_reason (string or null)
  evidence (string or null — the specific fact/quote supporting this entry)

Return at most 15 companies. If you find none you're confident about, \
return an empty array: []
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


def build_prompt() -> str:
    config = load_json(CONFIG_FILE, {})
    companies = load_json(COMPANIES_FILE, [])
    sectors = sorted({c.get("sector", "") for c in companies if c.get("sector")})
    existing_names = sorted({c["name"] for c in companies})
    return PROMPT_TEMPLATE.format(
        sectors=", ".join(sectors[:20]) or "fintech, legaltech, insurtech, applied AI",
        profile=config.get("candidate_profile", ""),
        existing_names=", ".join(existing_names),
    )


def call_grok(prompt: str, api_key: str) -> list[dict]:
    """
    NOTE: request shape is best-effort against xAI's documented API as of
    early-to-mid 2025 training data. `search_parameters: {"mode": "auto"}`
    is xAI's mechanism for grounding the completion in live web search —
    verify this is still current before trusting the output.
    """
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


def to_discovery(raw: dict) -> dict | None:
    name = (raw.get("company_name") or "").strip()
    if not name:
        return None
    discovery_id = "cd_" + hashlib.sha1(
        f"{name}|{raw.get('source_url', '')}".encode()
    ).hexdigest()[:12]
    return {
        "discovery_id": discovery_id,
        "company_name": name,
        "description": raw.get("description"),
        "sector": raw.get("sector"),
        "funding_stage": raw.get("funding_stage"),
        "funding_amount": raw.get("funding_amount"),
        "funding_date": raw.get("funding_date"),
        "lead_investor": raw.get("lead_investor"),
        "hq_location": raw.get("hq_location"),
        "source_publication": raw.get("source_publication"),
        "source_url": raw.get("source_url"),
        "discovery_date": TODAY,
        "discovery_reason": raw.get("discovery_reason"),
        "evidence": raw.get("evidence"),
        "status": "new",
        "notes": None,
    }


def main():
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("XAI_API_KEY not set — Grok Company Scout is a no-op. "
              "Set this secret to enable it (see grok/README.md).")
        sys.exit(0)

    prompt = build_prompt()
    print("Grok Company Scout — calling xAI API...")
    try:
        raw_results = call_grok(prompt, api_key)
    except Exception as e:
        print(f"  [!] Grok API call failed: {e}")
        print("  This may mean the request shape in this script is out of date — "
              "check https://docs.x.ai/ against grok_company_scout.py's call_grok().")
        sys.exit(1)

    existing = load_json(DISCOVERIES_FILE, [])
    existing_ids = {d["discovery_id"] for d in existing}

    added = 0
    for raw in raw_results:
        discovery = to_discovery(raw)
        if not discovery:
            continue
        if discovery["discovery_id"] in existing_ids:
            continue
        existing.append(discovery)
        existing_ids.add(discovery["discovery_id"])
        added += 1

    save_json(DISCOVERIES_FILE, existing)
    print(f"  {len(raw_results)} candidates returned, {added} new discoveries written.")


if __name__ == "__main__":
    main()
