#!/usr/bin/env python3
"""
Deterministic ATS probing — shared by reconcile_grok.py.

Given a company name or a specific (provider, token) guess, checks whether a
public ATS API actually resolves to a real, live board FOR THAT COMPANY.
"Resolves to a real board" and "is the board we think it is" are different
questions — this module answers both where the API allows it.

Two failure modes matter here:

1. A 200 that isn't real. SmartRecruiters returns {"totalFound": 0,
   "content": []} for ANY slug, real or fake — a bare 200 check is not
   verification. Confirmation requires totalFound > 0 AND a "company"
   object echoing back a matching identifier.

2. A 200 that's real but for the WRONG company. Blind slug-guessing from a
   company name (e.g. "backbase" from "Backbase") can coincidentally hit a
   different, unrelated account on the same provider that happens to use
   the same slug. Greenhouse embeds `company_name` on every job; Workable
   always returns a top-level `name`. Where that identifying field exists,
   it's cross-checked against the expected company name. Ashby and Lever's
   public APIs expose no such field — a structural 200 there confirms the
   SLUG is real and live, but NOT that it belongs to the company we guessed
   it for. auto_detect() treats that distinction as a promotion-safety gate
   (see `promotable` below), not just an implementation detail.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import requests

TIMEOUT = 10
NAME_MATCH_THRESHOLD = 0.82


def _normalise(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"\b(inc|ltd|limited|llc|gmbh|corp|corporation|plc|co)\.?\s*$", "", name)
    name = re.sub(r"[^a-z0-9\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _names_match(a: str, b: str) -> bool:
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= NAME_MATCH_THRESHOLD


def _get(url, **kwargs):
    try:
        return requests.get(url, timeout=TIMEOUT, **kwargs)
    except Exception as e:
        return e


def probe_greenhouse(token: str, expected_name: str | None = None) -> dict:
    resp = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        jobs = resp.json().get("jobs", [])
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable response"}

    identity_confirmed = None
    if expected_name and jobs:
        found_name = jobs[0].get("company_name", "")
        identity_confirmed = _names_match(expected_name, found_name)

    return {
        "confirmed": True,
        "identity_confirmed": identity_confirmed,
        "job_count": len(jobs),
        "reason": "200 + parseable job list" + (
            f"; company_name={jobs[0].get('company_name')!r}" if jobs else "; 0 jobs, no company_name to check"
        ),
    }


def probe_lever(token: str, expected_name: str | None = None) -> dict:
    resp = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable response"}
    if not isinstance(data, list):
        return {"confirmed": False, "identity_confirmed": None, "reason": "unexpected response shape"}
    # Lever's public postings API exposes no company-name field at all.
    return {
        "confirmed": True,
        "identity_confirmed": None,
        "job_count": len(data),
        "reason": "200 + parseable job list (Lever exposes no company field to cross-check identity)",
    }


def probe_ashby(token: str, expected_name: str | None = None) -> dict:
    resp = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
        raw_jobs = data if isinstance(data, list) else (
            data.get("jobPostings") or data.get("jobs") or []
        )
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable response"}
    # Ashby's posting API exposes no company-name field at all (the board's
    # display name only appears in the HTML page, not this JSON API).
    return {
        "confirmed": True,
        "identity_confirmed": None,
        "job_count": len(raw_jobs),
        "reason": "200 + parseable job list (Ashby API exposes no company field to cross-check identity)",
    }


def probe_workable(token: str, expected_name: str | None = None) -> dict:
    resp = _get(
        f"https://apply.workable.com/api/v1/widget/accounts/{token}",
        params={"details": "true"},
    )
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
        jobs = data.get("jobs", [])
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable response"}

    found_name = data.get("name", "")
    description = data.get("description")
    has_content = bool(jobs) or bool(description)

    identity_confirmed = None
    reason_suffix = ""
    if expected_name:
        if not has_content:
            # A name match on a board with zero jobs AND no description is
            # exactly what a coincidental unrelated account looks like —
            # confirmed live: guessing "ramp" from the company "Ramp"
            # resolves to a real but unrelated Workable account also named
            # "RAMP" with description=null, jobs=[]. Don't call that
            # confirmed just because the strings match.
            identity_confirmed = None
            reason_suffix = " (name matches but board has 0 jobs and no description — too thin to trust)"
        else:
            identity_confirmed = _names_match(expected_name, found_name)

    return {
        "confirmed": True,
        "identity_confirmed": identity_confirmed,
        "job_count": len(jobs),
        "reason": f"200 + parseable widget response; name={found_name!r}{reason_suffix}",
    }


def probe_smartrecruiters(token: str, expected_name: str | None = None) -> dict:
    """
    SmartRecruiters returns HTTP 200 with an empty result for ANY slug —
    confirmed and unconfirmed differ only in payload content, not status.
    """
    resp = _get(
        f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
        params={"limit": 1},
    )
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable response"}

    total = data.get("totalFound", 0)
    content = data.get("content", [])
    if total > 0 and content:
        company_obj = content[0].get("company", {})
        found_name = company_obj.get("name") or company_obj.get("identifier", "")
        if found_name:
            identity_confirmed = _names_match(expected_name, found_name) if expected_name else True
            return {
                "confirmed": True,
                "identity_confirmed": identity_confirmed,
                "job_count": total,
                "reason": f"200 + confirmed company={found_name!r}",
            }
    if total == 0:
        return {
            "confirmed": False,
            "identity_confirmed": None,
            "inconclusive": True,
            "reason": "HTTP 200 but totalFound=0 — indistinguishable from a "
                      "nonexistent slug on this provider; needs manual check",
        }
    return {"confirmed": False, "identity_confirmed": None, "reason": "no company object in response"}


def probe_personio(token: str, expected_name: str | None = None) -> dict:
    resp = _get(f"https://{token}.jobs.personio.de/xml?language=en")
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        root = ET.fromstring(resp.content)
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable XML"}
    if root.tag != "workzag-jobs":
        return {"confirmed": False, "identity_confirmed": None, "reason": f"unexpected root element {root.tag!r}"}

    identity_confirmed = None
    positions = root.findall("position")
    if expected_name and positions:
        subcompany = positions[0].findtext("subcompany", "")
        if subcompany:
            identity_confirmed = _names_match(expected_name, subcompany)

    return {
        "confirmed": True,
        "identity_confirmed": identity_confirmed,
        "job_count": len(positions),
        "reason": "200 + valid workzag-jobs XML (nonexistent subdomains redirect, not 200)",
    }


def probe_teamtailor(token: str, expected_name: str | None = None) -> dict:
    resp = _get(f"https://{token}.teamtailor.com/jobs.json")
    if isinstance(resp, Exception):
        return {"confirmed": False, "identity_confirmed": None, "reason": str(resp)[:100]}
    if resp.status_code != 200:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"confirmed": False, "identity_confirmed": None, "reason": "unparseable response"}
    if "items" not in data:
        return {"confirmed": False, "identity_confirmed": None, "reason": "no 'items' key in response"}

    identity_confirmed = None
    found_name = data.get("title", "")
    if expected_name and found_name:
        identity_confirmed = _names_match(expected_name, found_name)

    return {
        "confirmed": True,
        "identity_confirmed": identity_confirmed,
        "job_count": len(data.get("items", [])),
        "reason": f"200 + parseable items list; feed title={found_name!r}",
    }


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "ashby": probe_ashby,
    "workable": probe_workable,
    "smartrecruiters": probe_smartrecruiters,
    "personio": probe_personio,
    "teamtailor": probe_teamtailor,
}

# Providers whose public API can positively confirm identity (given a
# non-empty result). Ashby and Lever cannot — a structural 200 there only
# proves the slug is real, not that it's the company we think it is.
IDENTITY_CAPABLE = {"greenhouse", "workable", "smartrecruiters", "personio", "teamtailor"}


def probe(provider: str, token: str, expected_name: str | None = None) -> dict:
    """Probe a specific (provider, token) guess. Returns a result dict; see PROBES."""
    if not isinstance(provider, str):
        # Reachable from Grok/agent-supplied ats_provider_guess — a
        # wrong-typed value here would otherwise crash on provider.lower().
        return {"confirmed": False, "identity_confirmed": None,
                "reason": f"provider is a {type(provider).__name__}, not a string"}
    fn = PROBES.get(provider.lower())
    if not fn:
        return {"confirmed": False, "identity_confirmed": None, "reason": f"unknown provider {provider!r}"}
    result = fn(token, expected_name)
    result["provider"] = provider.lower()
    result["token"] = token
    return result


def slug_candidates(company_name: str) -> list[str]:
    """
    Generate a small set of plausible board-slug guesses from a company name.
    Deliberately conservative — a handful of the most common real-world
    patterns, not a combinatorial explosion.
    """
    stripped = _normalise(company_name)
    no_space = stripped.replace(" ", "")
    hyphenated = re.sub(r"\s+", "-", stripped)

    seen = set()
    out = []
    for c in (no_space, hyphenated):
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def auto_detect(company_name: str, hint_provider: str | None = None,
                 hint_token: str | None = None) -> dict | None:
    """
    Try to confirm a working ATS for a company, and decide whether it's
    safe to auto-promote without human/Claude review.

    Returns a probe() result with an added `promotable: bool`, or None if
    nothing was found at all. `promotable=False` means "we found something
    real, but can't be sure it's the right company" — reconciliation should
    route that to review, not straight into companies.json.

    Policy:
      - A hint from Grok (it presumably read the real careers page) is
        trusted if confirmed, unless the API's own identity check actively
        contradicts it (identity_confirmed is False, not just unknown).
      - A blind slug guess (no hint — we're the ones inferring the token
        from the company name) is only promotable when the provider's API
        let us positively confirm identity. A structural-only confirmation
        on Ashby/Lever is real evidence the slug exists, but not enough on
        its own to promote automatically.
    """
    if hint_provider and hint_token:
        result = probe(hint_provider, hint_token, expected_name=company_name)
        if result.get("confirmed"):
            result["promotable"] = result.get("identity_confirmed") is not False
            return result

    for slug in slug_candidates(company_name):
        for provider in ("ashby", "greenhouse", "lever", "workable"):
            result = probe(provider, slug, expected_name=company_name)
            if result.get("confirmed"):
                result["promotable"] = result.get("identity_confirmed") is True
                return result
    return None
