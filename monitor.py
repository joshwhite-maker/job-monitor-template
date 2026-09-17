#!/usr/bin/env python3
"""
Job Monitor — daily digest of new roles across a curated company list.

Polls Greenhouse, Lever, Ashby, and Workable ATS public APIs, deduplicates
against seen.json, filters by title/location keywords, and emails a ranked
digest. Optionally scores each role against your profile via the Claude API.

For roles scoring >= enrich_score_threshold, fetches full job description
and includes a brief excerpt in the email alongside the rationale.

Run manually:  python monitor.py
Run on schedule: GitHub Actions (see .github/workflows/daily-monitor.yml)
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "companies.json"
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
WEB_JOBS_FILE = ROOT / "web_jobs.json"
WATCHLIST_FILE = ROOT / "grok" / "web_watchlist.json"
COMPANY_DISCOVERIES_FILE = ROOT / "grok" / "company_discoveries.json"
DIGEST_STATE_FILE = ROOT / "digest_state.json"


def load_json(path: Path, default=None):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}


def save_json(path: Path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_description_snippet(text: str, max_chars: int = 600) -> str:
    """
    Extract a useful snippet from a job description.
    Tries to find responsibility/requirement sections first;
    falls back to the first max_chars characters.
    """
    lower = text.lower()
    for marker in [
        "what you'll do", "responsibilities", "the role", "about the role",
        "you will", "key responsibilities", "what you will do",
        "requirements", "what we're looking for", "must have", "you'll need"
    ]:
        idx = lower.find(marker)
        if idx != -1:
            snippet = text[idx:idx + max_chars].strip()
            last_period = snippet.rfind(".")
            if last_period > max_chars // 2:
                snippet = snippet[:last_period + 1]
            return snippet

    return text[:max_chars].strip()


# ---------------------------------------------------------------------------
# ATS Pollers
# ---------------------------------------------------------------------------

def poll_greenhouse(token: str) -> list[dict]:
    """Public Greenhouse board API — no auth required."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        jobs = []
        for job in resp.json().get("jobs", []):
            jobs.append({
                "id": f"gh-{token}-{job['id']}",
                "_gh_token": token,
                "_gh_job_id": job["id"],
                "title": job["title"],
                "location": job.get("location", {}).get("name", ""),
                "url": job.get("absolute_url", ""),
                "description": None,
            })
        return jobs
    except Exception as e:
        print(f"  [!] Greenhouse/{token}: {e}")
        return []


def fetch_greenhouse_description(token: str, job_id: int) -> str:
    """Fetch full job description for a single Greenhouse role."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        html = resp.json().get("content", "")
        return strip_html(html)
    except Exception:
        return ""


def poll_lever(token: str) -> list[dict]:
    """Public Lever postings API — no auth required."""
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        jobs = []
        for job in resp.json():
            lists = job.get("lists", [])
            desc_parts = []
            for lst in lists:
                desc_parts.append(lst.get("text", "") + ": " +
                                  strip_html(lst.get("content", "")))
            description = " ".join(desc_parts)

            jobs.append({
                "id": f"lv-{token}-{job['id']}",
                "title": job["text"],
                "location": job.get("categories", {}).get("location", ""),
                "url": job.get("hostedUrl", ""),
                "description": description,
            })
        return jobs
    except Exception as e:
        print(f"  [!] Lever/{token}: {e}")
        return []


def poll_ashby(token: str) -> list[dict]:
    """
    Public Ashby posting API — no auth required.

    FIXED 2026-08-24: the API returns {"jobs": [...]} on most boards (some
    older/unmigrated boards may still return {"jobPostings": [...]}), and
    each job's "location" field can be a plain string OR a {"name": "..."}
    dict depending on the board. Verified live against real board data.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data if isinstance(data, list) else (
            data.get("jobPostings") or data.get("jobs") or []
        )
        jobs = []
        for job in raw_jobs:
            description = job.get("descriptionPlain", "") or strip_html(
                job.get("descriptionHtml", "")
            )
            loc = job.get("locationName") or job.get("location", "")
            if isinstance(loc, dict):
                loc = loc.get("name", "")
            jobs.append({
                "id": f"ab-{token}-{job['id']}",
                "title": job["title"],
                "location": loc,
                "url": job.get("jobPostingUrl", job.get("jobUrl", job.get("applyUrl", ""))),
                "description": description,
            })
        return jobs
    except Exception as e:
        print(f"  [!] Ashby/{token}: {e}")
        return []


def poll_workable(token: str) -> list[dict]:
    """
    Public Workable widget API — no auth required.

    FIXED 2026-08-24: Workable retired the old undocumented
    POST /api/v3/accounts/{token}/jobs path (now returns 400 for public
    callers — that endpoint requires an authenticated bearer token).
    The current public, unauthenticated feed is this GET widget endpoint,
    which also returns full descriptions directly when details=true,
    so no separate description-fetch call is needed for Workable roles.
    Token is the company slug from apply.workable.com/SLUG.
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}"
    try:
        resp = requests.get(url, params={"details": "true"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for job in data.get("jobs", []):
            # On public feeds "state" is sometimes a region name rather than
            # listing status — only skip if it's explicitly non-published.
            state = job.get("state")
            if state and state not in ("published",) and "region" not in str(state).lower():
                continue

            loc = (job.get("location") or {}).get("location_str", "")

            description = (
                (job.get("description", "") or "") + " " +
                (job.get("full_description", "") or "")
            ).strip()

            jobs.append({
                "id": f"wk-{token}-{job.get('id', job.get('shortcode', ''))}",
                "title": job["title"],
                "location": loc,
                "url": job.get("url") or job.get("shortlink", ""),
                "description": description or None,
            })
        return jobs
    except Exception as e:
        print(f"  [!] Workable/{token}: {e}")
        return []


def fetch_workable_description(token: str, shortcode: str) -> str:
    """
    Kept for backward compatibility — no longer called in the normal flow
    since poll_workable() now fetches full descriptions up front via
    details=true. Left in place in case a future board omits descriptions
    from the widget feed and per-job enrichment is needed again.
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}"
    try:
        resp = requests.get(url, params={"details": "true"}, timeout=10)
        resp.raise_for_status()
        for job in resp.json().get("jobs", []):
            if job.get("shortcode") == shortcode:
                desc = (
                    (job.get("description", "") or "") + " " +
                    (job.get("full_description", "") or "")
                ).strip()
                return desc
        return ""
    except Exception:
        return ""


def poll_smartrecruiters(token: str) -> list[dict]:
    """Public SmartRecruiters Posting API — no auth required. Paginates via offset."""
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    jobs = []
    offset = 0
    limit = 100

    while True:
        try:
            resp = requests.get(url, params={"limit": limit, "offset": offset}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [!] SmartRecruiters/{token}: {e}")
            break

        content = data.get("content", [])
        for job in content:
            location = job.get("location") or {}
            loc_str = location.get("fullLocation") or ", ".join(
                str(location[field]) for field in ("city", "region", "country")
                if location.get(field)
            )
            jobs.append({
                "id": f"sr-{token}-{job['id']}",
                "_sr_token": token,
                "_sr_job_id": job["id"],
                "title": job.get("name", ""),
                "location": loc_str,
                "url": job.get("postingUrl") or job.get("applyUrl", ""),
                "description": None,
            })

        offset += len(content)
        if not content or offset >= data.get("totalFound", 0):
            break

    return jobs


def fetch_smartrecruiters_description(token: str, job_id: str) -> str:
    """Fetch and flatten the public SmartRecruiters job-ad sections."""
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        sections = resp.json().get("jobAd", {}).get("sections", {})
        return strip_html(" ".join(
            section.get("text", "") for section in sections.values()
            if isinstance(section, dict)
        ))
    except Exception:
        return ""


def poll_personio(token: str) -> list[dict]:
    """Public Personio Career Site XML feed — no auth required."""
    url = f"https://{token}.jobs.personio.de/xml?language=en"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"  [!] Personio/{token}: {e}")
        return []

    jobs = []
    for position in root.findall("position"):
        job_id = position.findtext("id", "")
        description = strip_html(" ".join(
            node.text or ""
            for node in position.findall("./jobDescriptions/jobDescription/value")
        ))
        jobs.append({
            "id": f"pe-{token}-{job_id}",
            "title": position.findtext("name", ""),
            "location": position.findtext("office", ""),
            "url": f"https://{token}.jobs.personio.de/job/{job_id}?language=en",
            "description": description or None,
        })
    return jobs


def poll_teamtailor(token: str) -> list[dict]:
    """Public Teamtailor JSON job feed exposed by a career site — no auth required."""
    url = f"https://{token}.teamtailor.com/jobs.json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:
        print(f"  [!] Teamtailor/{token}: {e}")
        return []

    jobs = []
    for item in items:
        posting = item.get("_jobposting", {})
        addresses = [place.get("address", {}) for place in posting.get("jobLocation", [])]
        location = ", ".join(
            ", ".join(str(address[field]) for field in ("addressLocality", "addressCountry")
                      if address.get(field))
            for address in addresses
        )
        jobs.append({
            "id": f"tt-{token}-{item['id']}",
            "title": item.get("title", ""),
            "location": location,
            "url": item.get("url", ""),
            "description": strip_html(item.get("content_html", "")) or None,
        })
    return jobs


POLLERS = {
    "greenhouse": poll_greenhouse,
    "lever": poll_lever,
    "ashby": poll_ashby,
    "workable": poll_workable,
    "smartrecruiters": poll_smartrecruiters,
    "personio": poll_personio,
    "teamtailor": poll_teamtailor,
}


# ---------------------------------------------------------------------------
# Web-discovered jobs (grok/ -> Claude reconciliation -> web_jobs.json)
#
# This reads ONLY web_jobs.json — the canonical store reconciliation
# promotes into after validating a raw Grok discovery. It never reads
# grok/web_job_discoveries.json directly, so an unvalidated discovery can
# never reach this digest.
#
# web_jobs.json's own `id` is already present in seen.json — reconciliation
# adds it there for its own "don't rediscover the same job" bookkeeping,
# unrelated to whether it's ever been shown to a human. Candidacy for the
# email is therefore tracked separately, via an `emailed` flag on the
# web_jobs.json record itself (set by this script once a job is selected
# for a digest — see main()), the same way seen.json already tracks ATS
# jobs regardless of whether the email actually sent.
# ---------------------------------------------------------------------------

def normalise_job_url(url: str) -> str:
    """
    Loose normalisation for cross-source dedup — the same posting can
    reach us with a trailing slash or tracking query string depending on
    which source (ATS poll vs web research) fetched it.
    """
    if not url:
        return ""
    url = url.strip().lower().split("?")[0].split("#")[0]
    return url.rstrip("/")


def normalise_web_job(raw: dict) -> dict:
    """
    Map a web_jobs.json record onto the same job-dict shape the ATS
    pollers produce (id/title/location/url/description/company/source),
    so passes_filter(), score_with_claude(), and the email template need
    no source-specific branching. `description` is filled from `evidence`
    (the citation Grok/reconciliation recorded) — web_jobs.json has no
    separate description field.
    """
    return {
        "id": raw["id"],
        "title": raw.get("title", ""),
        "location": raw.get("location") or "",
        "url": raw.get("job_url", ""),
        "description": raw.get("evidence") or "",
        "company": raw.get("company", "Unknown"),
        "source": "web",
    }


def load_web_job_candidates() -> tuple[list[dict], list[dict], dict]:
    """
    Returns (normalised_candidates, raw_records, raw_records_by_id).
    A candidate must be active, not yet emailed, and have an actual URL —
    reconciliation already validates job_url before promotion, but this
    is a second, independent line of defence: the email must never link
    to an empty href.
    """
    raw_records = load_json(WEB_JOBS_FILE, [])
    by_id = {w["id"]: w for w in raw_records}
    candidates = [
        normalise_web_job(w) for w in raw_records
        if w.get("status") == "active" and not w.get("emailed") and w.get("job_url")
    ]
    return candidates, raw_records, by_id


# ---------------------------------------------------------------------------
# Executive digest — company-intelligence sections
#
# Everything here reads ONLY canonical, reconciliation-produced data:
# companies.json, web_jobs.json, grok/web_watchlist.json (reconciliation's
# own maintained ledger — see grok/README.md; not a raw discovery queue).
#
# The one exception is grok/company_discoveries.json, used ONLY as a
# read-only narrative lookup (company description / funding signal /
# discovery date — text Grok itself wrote), and ONLY for discoveries
# reconciliation has already acted on (status "promoted" or "watchlisted",
# never "new"/pending, "duplicate", or "rejected"). Canonical data alone
# still decides whether a company is real and being monitored; this
# lookup only supplies richer display text for a company canonical data
# already confirms. Nothing here invents a fact Grok didn't actually
# write — a company with no matching narrative just gets a plainer entry.
# ---------------------------------------------------------------------------

def parse_date_loose(value) -> datetime | None:
    """
    Parse a YYYY-MM-DD or ISO-datetime string as found in reconciliation's
    output. Never raises, never guesses a date that isn't there. Returns a
    naive datetime (tzinfo stripped) since the rest of this codebase
    already compares naive datetime.now() values throughout.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def week_start(now: datetime) -> datetime:
    """Most recent Monday at 00:00 — the week-to-date window for the opening summary."""
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def load_digest_state() -> dict:
    return load_json(DIGEST_STATE_FILE, {})


def since_last_digest_cutoff(state: dict, now: datetime) -> datetime:
    """
    The "what changed since the previous digest" boundary used by the
    company/signal/activity sections. Falls back to 36h ago on the very
    first run under this system (no prior digest_state.json) — a sensible
    bootstrap, not a claim about when anything actually happened.
    """
    last = parse_date_loose(state.get("last_digest_sent"))
    return last if last else now - timedelta(hours=36)


def format_funding(narrative: dict) -> str | None:
    parts = [p for p in (narrative.get("funding_stage"), narrative.get("funding_amount")) if p]
    return " · ".join(parts) if parts else None


def load_reconciled_company_narratives() -> dict:
    """
    Company Scout's own description/funding-signal/date, keyed by
    normalised company name, restricted to discoveries reconciliation has
    already decided on. See module docstring above for why this file is
    read at all despite generally treating grok/ as a raw landing zone.
    """
    discoveries = load_json(COMPANY_DISCOVERIES_FILE, [])
    narratives: dict[str, dict] = {}
    for d in discoveries:
        if not isinstance(d, dict) or d.get("status") not in ("promoted", "watchlisted"):
            continue
        name = d.get("company_name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip().lower()
        existing = narratives.get(key)
        if not existing or (d.get("discovery_date") or "") >= (existing.get("discovery_date") or ""):
            narratives[key] = d
    return narratives


def build_new_companies_section(companies: list[dict], watchlist: list[dict],
                                 narratives: dict, since_cutoff: datetime) -> list[dict]:
    """Section 3: companies Company Scout found during the period — promoted or watchlisted."""
    items = []
    seen_names: set[str] = set()

    for c in companies:
        if c.get("source") not in ("grok_company_discovery", "grok_web"):
            # Either one of the original, manually curated 214 (no source
            # at all), or a watchlist_reprobe resolution — a previously
            # *known* company (already on the watchlist) whose ATS was
            # found by a deterministic retry, not a new Company Scout
            # discovery. Attributing that here would misstate provenance.
            continue
        d = parse_date_loose(c.get("discovered_date")) or parse_date_loose(c.get("ats_confirmed_date"))
        if not d or d < since_cutoff:
            continue
        key = c["name"].strip().lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        narrative = narratives.get(key, {})
        link = narrative.get("source_url") or c.get("source_url")
        items.append({
            "name": c["name"],
            "description": narrative.get("description"),
            "signal": narrative.get("discovery_reason"),
            "funding": format_funding(narrative),
            "date": narrative.get("discovery_date") or c.get("discovered_date") or c.get("ats_confirmed_date"),
            "link": link,
            "link_label": "Source" if link else None,
            "monitoring_status": f"Added to ATS monitoring ({c['ats']})",
        })

    for w in watchlist:
        if w.get("source") not in ("grok_company_discovery", "grok_web_job_discovery"):
            continue  # excludes the 2026-09-16 EXCLUDED.md seed batch — not a new discovery
        d = parse_date_loose(w.get("added_date"))
        if not d or d < since_cutoff:
            continue
        key = w["company"].strip().lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        narrative = narratives.get(key, {})
        items.append({
            "name": w["company"],
            "description": narrative.get("description"),
            "signal": narrative.get("discovery_reason"),
            "funding": format_funding(narrative),
            "date": narrative.get("discovery_date") or w.get("added_date"),
            "link": narrative.get("source_url"),
            "link_label": "Source" if narrative.get("source_url") else None,
            "monitoring_status": "Added to web watchlist (researching further)",
        })

    return items


def build_hiring_signal_section(companies: list[dict], watchlist: list[dict],
                                 web_jobs_raw: list[dict], narratives: dict,
                                 config: dict, grok_company_jobs_today: dict) -> list[dict]:
    """
    Section 4: companies with real, evidenced hiring activity but nothing
    that passes the relevance filter right now. Scoped to companies Grok
    actually surfaced a signal or a job for (not a blanket scan of the
    full 200+ company universe, which would mostly just be noise here).
    """
    items = []
    seen_names: set[str] = set()

    # (a) Grok-discovered companies now on ATS monitoring: has jobs today,
    #     none of them relevant.
    for c in companies:
        if not c.get("source"):
            continue
        key = c["name"].strip().lower()
        jobs_today = grok_company_jobs_today.get(key, [])
        if not jobs_today or any(passes_filter(j, config) for j in jobs_today):
            continue  # no jobs at all, or it has a relevant one — not this section
        seen_names.add(key)
        narrative = narratives.get(key, {})
        items.append({
            "name": c["name"],
            "why": narrative.get("discovery_reason"),
            "evidence": f"{len(jobs_today)} open role{'s' if len(jobs_today) != 1 else ''} currently listed "
                        f"(e.g. “{jobs_today[0]['title']}”) — none matching your target profile.",
            "link": jobs_today[0].get("url"),
            "link_label": "View openings",
            "monitoring_status": f"Monitored via ATS ({c['ats']})",
        })

    # (b) Watchlisted companies with real web-discovered evidence, none matching.
    web_by_company: dict[str, list[dict]] = {}
    for w in web_jobs_raw:
        web_by_company.setdefault((w.get("company") or "").strip().lower(), []).append(w)

    for entry in watchlist:
        if entry.get("status") != "active":
            continue
        key = entry["company"].strip().lower()
        if key in seen_names:
            continue
        candidate_jobs = web_by_company.get(key, [])
        if not candidate_jobs:
            continue  # no discovered job at all — never describe as "hiring" without evidence
        normalised = [normalise_web_job(w) for w in candidate_jobs]
        if any(passes_filter(j, config) for j in normalised):
            continue  # has a relevant role — belongs in section 2, not here
        seen_names.add(key)
        narrative = narratives.get(key, {})
        first = candidate_jobs[0]
        items.append({
            "name": entry["company"],
            "why": narrative.get("discovery_reason") or entry.get("notes"),
            "evidence": f"{len(candidate_jobs)} open role{'s' if len(candidate_jobs) != 1 else ''} found "
                        f"(e.g. “{first.get('title', '')}”) — none matching your target profile.",
            "link": first.get("careers_url") or first.get("job_url"),
            "link_label": "View openings",
            "monitoring_status": "On web watchlist (no ATS integration yet)",
        })

    return items


def build_week_summary(companies: list[dict], watchlist: list[dict], web_jobs_raw: list[dict],
                        seen: dict, week_start_dt: datetime) -> dict:
    """Week-to-date figures for the opening summary. Pure date-filters over
    already-persisted canonical data — no new tracking needed for these."""
    new_company_names: set[str] = set()
    for c in companies:
        # Same provenance distinction as build_new_companies_section: a
        # watchlist_reprobe resolution is a previously-known company, not
        # a new Company Scout discovery.
        if c.get("source") not in ("grok_company_discovery", "grok_web"):
            continue
        d = parse_date_loose(c.get("discovered_date")) or parse_date_loose(c.get("ats_confirmed_date"))
        if d and d >= week_start_dt:
            new_company_names.add(c["name"].strip().lower())
    added_to_monitoring = len(new_company_names)

    added_to_watchlist = 0
    for w in watchlist:
        if w.get("source") not in ("grok_company_discovery", "grok_web_job_discovery"):
            continue
        d = parse_date_loose(w.get("added_date"))
        if d and d >= week_start_dt:
            key = w["company"].strip().lower()
            if key not in new_company_names:
                new_company_names.add(key)
                added_to_watchlist += 1

    new_web_roles = sum(
        1 for w in web_jobs_raw
        if (parse_date_loose(w.get("discovery_date")) or datetime.min) >= week_start_dt
    )
    relevant_surfaced = sum(
        1 for k, ts in seen.items()
        if not k.startswith("url:") and (parse_date_loose(ts) or datetime.min) >= week_start_dt
    )

    return {
        "new_companies": len(new_company_names),
        "added_to_monitoring": added_to_monitoring,
        "added_to_watchlist": added_to_watchlist,
        "new_web_roles": new_web_roles,
        "relevant_surfaced": relevant_surfaced,
    }


def build_system_summary(companies: list[dict], watchlist: list[dict], web_jobs_raw: list[dict],
                          since_cutoff: datetime, ats_roles_fetched: int, relevant_count: int) -> dict:
    """Since-last-digest activity figures for the compact system-summary section."""
    new_companies = sum(
        1 for c in companies
        if c.get("source") in ("grok_company_discovery", "grok_web") and (
            (parse_date_loose(c.get("discovered_date")) or parse_date_loose(c.get("ats_confirmed_date")))
            or datetime.min
        ) >= since_cutoff
    )
    new_watchlist = sum(
        1 for w in watchlist
        if w.get("source") in ("grok_company_discovery", "grok_web_job_discovery")
        and (parse_date_loose(w.get("added_date")) or datetime.min) >= since_cutoff
    )
    checked = sum(
        1 for w in watchlist
        if (parse_date_loose(w.get("last_checked")) or datetime.min) >= since_cutoff
    )
    new_web_jobs = sum(
        1 for w in web_jobs_raw
        if (parse_date_loose(w.get("discovery_date")) or datetime.min) >= since_cutoff
    )
    return {
        "companies_monitored": len(companies),
        "ats_roles_fetched": ats_roles_fetched,
        "watchlist_checked": checked,
        "watchlist_size": sum(1 for w in watchlist if w.get("status") == "active"),
        "new_companies": new_companies + new_watchlist,
        "new_web_jobs": new_web_jobs,
        "relevant_surfaced": relevant_count,
    }


# ---------------------------------------------------------------------------
# Description enrichment for high-scoring roles
# ---------------------------------------------------------------------------

def enrich_descriptions(jobs: list[dict], threshold: int) -> list[dict]:
    """
    For roles at or above threshold that don't already have a description,
    fetch it via a second API call. Greenhouse and SmartRecruiters need this —
    Lever, Ashby, Workable, Personio, and Teamtailor already include
    description in the listing response.
    """
    to_enrich = [j for j in jobs if j.get("score", 0) >= threshold
                 and not j.get("description")]

    if to_enrich:
        print(f"  Fetching full descriptions for {len(to_enrich)} high-scoring roles...")

    for job in to_enrich:
        if "_gh_token" in job:
            job["description"] = fetch_greenhouse_description(
                job["_gh_token"], job["_gh_job_id"]
            )
        elif "_sr_token" in job:
            job["description"] = fetch_smartrecruiters_description(
                job["_sr_token"], job["_sr_job_id"]
            )

    return jobs


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def passes_filter(job: dict, config: dict) -> bool:
    """
    Returns True if the job passes all keyword filters.
    All matching is case-insensitive against the job title.
    Location filter is applied if the job has a location set.
    """
    title = job["title"].lower()
    location = job["location"].lower()

    role_keywords = [k.lower() for k in config.get("role_keywords", [])]
    if role_keywords and not any(k in title for k in role_keywords):
        return False

    exclude_keywords = [k.lower() for k in config.get("exclude_keywords", [])]
    if any(k in title for k in exclude_keywords):
        return False

    loc_keywords = [k.lower() for k in config.get("location_keywords", [])]
    if loc_keywords and location:
        if not any(k in location for k in loc_keywords):
            return False

    return True


# ---------------------------------------------------------------------------
# Claude Scoring (optional — requires ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

def score_with_claude(jobs: list[dict], profile: str, api_key: str,
                      enrich_threshold: int) -> list[dict]:
    """
    Score each job 1–10 against the candidate profile using Claude Haiku.
    Includes description snippet in prompt where available.
    Adds 'score' and 'rationale' keys to each job dict.
    Returns jobs sorted by score descending.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    for job in jobs:
        desc = job.get("description") or ""
        snippet = extract_description_snippet(desc, max_chars=600) if desc else ""

        if snippet:
            job_context = (
                f"Job title: {job['title']}\n"
                f"Company: {job['company']}\n"
                f"Location: {job['location'] or 'unspecified'}\n\n"
                f"Role excerpt:\n{snippet}"
            )
        else:
            job_context = (
                f"Job title: {job['title']}\n"
                f"Company: {job['company']}\n"
                f"Location: {job['location'] or 'unspecified'}"
            )

        prompt = (
            f"Rate this job opportunity for the candidate below on a scale of 1-10 "
            f"(10 = excellent fit). Reply ONLY with valid JSON, no markdown:\n"
            f'{{"score": <int>, "rationale": "<one sentence>"}}\n\n'
            f"Candidate profile: {profile}\n\n"
            f"{job_context}"
        )

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            job["score"] = int(result.get("score", 5))
            job["rationale"] = result.get("rationale", "")
        except Exception as e:
            print(f"  [!] Scoring failed for '{job['title']}': {e}")
            job["score"] = 5
            job["rationale"] = ""

    return sorted(jobs, key=lambda j: j.get("score", 0), reverse=True)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def render_company_card(c: dict, accent: str = "#d1d5db") -> str:
    """Card for the 'New companies discovered' section."""
    link_html = (
        f" &nbsp;·&nbsp; <a href='{c['link']}' style='color:#2563eb;font-size:0.85em'>"
        f"{c.get('link_label') or 'Source'}</a>"
        if c.get("link") else ""
    )
    desc_html = (
        f"<p style='margin:6px 0 0;color:#444;font-size:0.9em'>{c['description']}</p>"
        if c.get("description") else ""
    )
    meta_bits = [m for m in (c.get("signal"), c.get("funding"), c.get("date")) if m]
    meta_html = (
        f"<p style='margin:4px 0 0;color:#6b7280;font-size:0.82em'>{' &nbsp;·&nbsp; '.join(meta_bits)}</p>"
        if meta_bits else ""
    )
    status_html = (
        f"<p style='margin:6px 0 0;color:#16a34a;font-size:0.8em;font-weight:600'>{c['monitoring_status']}</p>"
        if c.get("monitoring_status") else ""
    )
    return (
        f"<div style='margin:10px 0;padding:10px 14px;border-left:3px solid {accent};"
        f"background:#fafafa;border-radius:4px'>"
        f"<strong style='color:#111;font-size:1em'>{c['name']}</strong>{link_html}"
        f"{desc_html}{meta_html}{status_html}"
        f"</div>"
    )


def render_signal_card(c: dict) -> str:
    """Card for the 'Companies hiring — no matching role yet' section."""
    link_html = (
        f" &nbsp;·&nbsp; <a href='{c['link']}' style='color:#2563eb;font-size:0.85em'>"
        f"{c.get('link_label') or 'View'}</a>"
        if c.get("link") else ""
    )
    why_html = (
        f"<p style='margin:6px 0 0;color:#444;font-size:0.88em'>{c['why']}</p>"
        if c.get("why") else ""
    )
    evidence_html = (
        f"<p style='margin:4px 0 0;color:#6b7280;font-size:0.85em'>{c['evidence']}</p>"
        if c.get("evidence") else ""
    )
    status_html = (
        f"<p style='margin:6px 0 0;color:#92400e;font-size:0.8em;font-weight:600'>{c['monitoring_status']}</p>"
        if c.get("monitoring_status") else ""
    )
    return (
        f"<div style='margin:10px 0;padding:10px 14px;border-left:3px solid #fbbf24;"
        f"background:#fffbeb;border-radius:4px'>"
        f"<strong style='color:#111;font-size:1em'>{c['name']}</strong>{link_html}"
        f"{why_html}{evidence_html}{status_html}"
        f"</div>"
    )


def render_system_summary(s: dict) -> str:
    items = [
        f"{s['companies_monitored']} companies monitored",
        f"{s['ats_roles_fetched']} ATS roles fetched",
        f"{s['watchlist_checked']} of {s['watchlist_size']} watchlist companies checked",
        f"{s['new_companies']} new compan{'y' if s['new_companies'] == 1 else 'ies'} discovered",
        f"{s['new_web_jobs']} new web job{'s' if s['new_web_jobs'] != 1 else ''} discovered",
        f"{s['relevant_surfaced']} relevant role{'s' if s['relevant_surfaced'] != 1 else ''} surfaced",
    ]
    rows = "".join(f"<li style='margin:2px 0'>{item}</li>" for item in items)
    return (
        "<h3 style='color:#111;margin:28px 0 4px;font-size:1.05em'>System activity since last digest</h3>"
        f"<ul style='color:#6b7280;font-size:0.85em;margin:4px 0;padding-left:18px'>{rows}</ul>"
    )


def build_email_body(jobs: list[dict], config: dict, *, greeting_name: str | None = None,
                      week_summary: dict | None = None, new_companies: list[dict] | None = None,
                      hiring_signals: list[dict] | None = None,
                      system_summary: dict | None = None) -> str:
    full_date_str = datetime.now().strftime("%d %B %Y")
    min_score = config.get("min_score_to_highlight", 7)
    enrich_threshold = config.get("enrich_score_threshold", 7)
    scoring_enabled = any("score" in j for j in jobs)
    name = greeting_name or "there"

    lines = [
        "<html><body style='max-width:680px;margin:0 auto;font-family:sans-serif'>",
        f"<h2 style='color:#111;margin-bottom:4px;font-size:1.3em'>Good morning {name} — "
        f"here's your job-search digest for {full_date_str}.</h2>",
    ]

    # --- Opening week-to-date summary ---
    if week_summary:
        parts = []
        if week_summary["new_companies"]:
            parts.append(f"<strong>{week_summary['new_companies']}</strong> new "
                         f"compan{'y' if week_summary['new_companies'] == 1 else 'ies'} discovered")
        if week_summary["new_web_roles"]:
            parts.append(f"<strong>{week_summary['new_web_roles']}</strong> new role"
                         f"{'s' if week_summary['new_web_roles'] != 1 else ''} found via web research")
        parts.append(f"<strong>{week_summary['relevant_surfaced']}</strong> relevant role"
                     f"{'s' if week_summary['relevant_surfaced'] != 1 else ''} surfaced to you")
        monitoring_bits = []
        if week_summary["added_to_monitoring"]:
            monitoring_bits.append(f"{week_summary['added_to_monitoring']} added to active ATS monitoring")
        if week_summary["added_to_watchlist"]:
            monitoring_bits.append(f"{week_summary['added_to_watchlist']} added to the web watchlist")
        lines.append(
            f"<p style='color:#555;font-size:0.95em;margin-top:0'>{week_summary['since_label']}: "
            + ", ".join(parts) + "."
            + (f" ({'; '.join(monitoring_bits)}.)" if monitoring_bits else "")
            + "</p>"
        )

    # --- Section: Relevant roles (existing behaviour, unchanged) ---
    lines.append("<h3 style='color:#111;margin:24px 0 4px;font-size:1.05em'>Relevant roles</h3>")
    lines.append(
        f"<p style='color:#555;margin-top:0'>{len(jobs)} new matching role"
        f"{'s' if len(jobs) != 1 else ''} today.</p>"
    )

    if not jobs:
        lines.append("<p>Nothing new today. Check back tomorrow.</p>")
    else:
        for job in jobs:
            score = job.get("score")
            rationale = job.get("rationale", "")
            desc = job.get("description") or ""

            highlight = scoring_enabled and score and score >= min_score
            border = "border-left:4px solid #2563eb" if highlight else "border-left:4px solid #e5e7eb"
            bg = "#f0f5ff" if highlight else "#fafafa"

            score_str = ""
            if score is not None:
                colour = "#2563eb" if score >= min_score else "#6b7280"
                score_str = (
                    f"<span style='color:{colour};font-weight:bold'>{score}/10</span>"
                    + (f" &nbsp;-&nbsp; <em style='color:#6b7280'>{rationale}</em>"
                       if rationale else "")
                )

            desc_html = ""
            if desc and score and score >= enrich_threshold:
                snippet = extract_description_snippet(desc, max_chars=400)
                if snippet:
                    desc_html = (
                        f"<p style='margin:8px 0 0;color:#444;font-size:0.9em;"
                        f"border-top:1px solid #e5e7eb;padding-top:8px'>"
                        f"{snippet}...</p>"
                    )

            # Source badge — small and muted, not a new section (per the
            # digest requirements: visible per-job, but not intrusive).
            if job.get("source") == "web":
                source_label, source_bg, source_fg = "Web-discovered", "#fef3c7", "#92400e"
            else:
                source_label, source_bg, source_fg = "ATS", "#e5e7eb", "#6b7280"
            source_badge = (
                f"<span style='display:inline-block;margin-left:6px;padding:1px 6px;"
                f"border-radius:3px;font-size:0.68em;font-weight:600;vertical-align:middle;"
                f"background:{source_bg};color:{source_fg}'>{source_label}</span>"
            )

            lines.append(
                f"<div style='margin:12px 0;padding:12px 16px;{border};"
                f"background:{bg};border-radius:4px'>"
                f"<strong><a href='{job['url']}' style='color:#111;"
                f"text-decoration:none;font-size:1.05em'>{job['title']}</a></strong>"
                f"{source_badge}<br>"
                f"<span style='color:#555;font-size:0.95em'>{job['company']}"
                + (f" &nbsp;·&nbsp; {job['location']}" if job["location"] else "")
                + (f"<br>{score_str}" if score_str else "")
                + "</span>"
                + desc_html
                + "</div>"
            )

    # --- Section: New companies discovered ---
    if new_companies:
        lines.append("<h3 style='color:#111;margin:28px 0 4px;font-size:1.05em'>New companies discovered</h3>")
        for c in new_companies:
            lines.append(render_company_card(c, accent="#2563eb"))

    # --- Section: Companies hiring, no matching role yet ---
    if hiring_signals:
        lines.append(
            "<h3 style='color:#111;margin:28px 0 4px;font-size:1.05em'>"
            "Companies hiring — no matching role yet</h3>"
        )
        for c in hiring_signals:
            lines.append(render_signal_card(c))

    # --- Section: System activity ---
    if system_summary:
        lines.append(render_system_summary(system_summary))

    lines.append("</body></html>")
    return "\n".join(lines)


def send_email(jobs: list[dict], config: dict, **digest_sections):
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    date_str = datetime.now().strftime("%d %b %Y")
    subject = f"Job Monitor: {len(jobs)} new role{'s' if len(jobs) != 1 else ''} - {date_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(build_email_body(jobs, config, **digest_sections), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(email_from, app_password)
        server.sendmail(email_from, email_to, msg.as_string())

    print(f"  Sent: '{subject}'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\nJob Monitor - {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 50)

    companies = load_json(COMPANIES_FILE, [])
    config = load_json(CONFIG_FILE, {})
    seen: dict = load_json(SEEN_FILE, {})

    if not companies:
        sys.exit("No companies found in companies.json - add some entries and retry.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    profile = config.get("candidate_profile", "")
    scoring_enabled = bool(api_key and profile)
    enrich_threshold = config.get("enrich_score_threshold", 7)

    now = datetime.now()

    # --- Poll each ATS ---
    all_jobs: list[dict] = []
    grok_company_names = {c["name"].strip().lower() for c in companies if c.get("source")}
    grok_company_jobs_today: dict[str, list[dict]] = {}
    for company in companies:
        name = company.get("name", "Unknown")
        ats = company.get("ats", "").lower()
        token = company.get("token", "")

        poller = POLLERS.get(ats)
        if not poller:
            print(f"  [!] Unknown ATS '{ats}' for {name} - skipping.")
            continue

        print(f"  Polling {name} ({ats})...")
        jobs = poller(token)
        for job in jobs:
            job["company"] = name
            job["source"] = "ats"
        all_jobs.extend(jobs)
        key = name.strip().lower()
        if key in grok_company_names:
            grok_company_jobs_today[key] = jobs

    print(f"\n  ATS roles fetched: {len(all_jobs)}")

    # --- Merge in web-discovered jobs (already validated/promoted by
    #     reconciliation into web_jobs.json — see the module docstring
    #     on load_web_job_candidates()) ---
    web_candidates, web_jobs_raw, web_jobs_by_id = load_web_job_candidates()
    print(f"  Web-discovered candidates: {len(web_candidates)}")
    all_jobs.extend(web_candidates)

    # --- Executive digest sections (companies/activity intelligence) ---
    watchlist = load_json(WATCHLIST_FILE, [])
    narratives = load_reconciled_company_narratives()
    digest_state = load_digest_state()
    since_cutoff = since_last_digest_cutoff(digest_state, now)
    week_start_dt = week_start(now)

    new_companies_section = build_new_companies_section(companies, watchlist, narratives, since_cutoff)
    hiring_signals_section = build_hiring_signal_section(
        companies, watchlist, web_jobs_raw, narratives, config, grok_company_jobs_today
    )
    print(f"  New companies discovered (since last digest): {len(new_companies_section)}")
    print(f"  Companies hiring, no matching role (since last digest): {len(hiring_signals_section)}")

    # --- Filter and dedupe ---
    new_jobs: list[dict] = []
    for job in all_jobs:
        if not passes_filter(job, config):
            continue

        is_web = job.get("source") == "web"
        url_key = f"url:{normalise_job_url(job.get('url', ''))}" if job.get("url") else None

        # ATS jobs: unchanged, existing id-based check. Web jobs: their id
        # is already in `seen` for an unrelated reason (see module
        # docstring above) — checking it here would hide every promoted
        # web job forever, so candidacy for those was already decided by
        # web_jobs.json's own `emailed` flag at load time. Both sources
        # still go through the url-based check, which is what actually
        # catches the same posting surfacing from two different sources
        # (e.g. a job promoted via web research before its company had
        # ATS coverage, which the ATS poll now also finds).
        if not is_web and job["id"] in seen:
            continue
        if url_key and url_key in seen:
            continue

        new_jobs.append(job)
        seen[job["id"]] = datetime.now().isoformat()
        if url_key:
            seen[url_key] = datetime.now().isoformat()
        if is_web:
            raw = web_jobs_by_id.get(job["id"])
            if raw is not None:
                raw["emailed"] = True
                raw["emailed_date"] = datetime.now().isoformat()

    print(f"  New matching roles: {len(new_jobs)} "
          f"({sum(1 for j in new_jobs if j.get('source') == 'web')} web-discovered)")

    # --- Score with Claude (optional) ---
    if scoring_enabled and new_jobs:
        print(f"  Scoring {len(new_jobs)} roles with Claude Haiku...")
        new_jobs = score_with_claude(new_jobs, profile, api_key, enrich_threshold)

        # --- Enrich descriptions for high scorers ---
        new_jobs = enrich_descriptions(new_jobs, enrich_threshold)

        # --- Re-score enriched roles with full description context ---
        enriched = [j for j in new_jobs
                    if j.get("score", 0) >= enrich_threshold and j.get("description")]
        if enriched:
            print(f"  Re-scoring {len(enriched)} enriched roles...")
            rescored = score_with_claude(enriched, profile, api_key, enrich_threshold)
            rescored_by_id = {j["id"]: j for j in rescored}
            new_jobs = [rescored_by_id.get(j["id"], j) for j in new_jobs]
            new_jobs.sort(key=lambda j: j.get("score", 0), reverse=True)

    elif new_jobs:
        new_jobs.sort(key=lambda j: j["company"].lower())

    # --- Build the two summary sections (week-to-date + since-last-digest) ---
    week_summary = build_week_summary(companies, watchlist, web_jobs_raw, seen, week_start_dt)
    week_summary["since_label"] = f"Since {week_start_dt.strftime('%A, %-d %B')}"
    system_summary = build_system_summary(
        companies, watchlist, web_jobs_raw, since_cutoff,
        ats_roles_fetched=len(all_jobs) - len(web_candidates),
        relevant_count=len(new_jobs),
    )

    # --- Output ---
    has_email_creds = os.environ.get("EMAIL_FROM") and os.environ.get("EMAIL_APP_PASSWORD")
    digest_sections = dict(
        greeting_name=config.get("greeting_name"),
        week_summary=week_summary,
        new_companies=new_companies_section,
        hiring_signals=hiring_signals_section,
        system_summary=system_summary,
    )

    if has_email_creds:
        send_email(new_jobs, config, **digest_sections)
    else:
        print("\n  No email credentials set - printing results:\n")
        for job in new_jobs:
            score = f"  [{job.get('score')}/10]" if "score" in job else ""
            print(f"  {job['title']} - {job['company']} ({job['location'] or '?'}){score}")
            if job.get("rationale"):
                print(f"  {job['rationale']}")
            print(f"  {job['url']}\n")

    # --- Persist seen IDs, web job emailed-flags, and last-digest marker ---
    save_json(SEEN_FILE, seen)
    save_json(WEB_JOBS_FILE, web_jobs_raw)
    save_json(DIGEST_STATE_FILE, {"last_digest_sent": now.isoformat()})
    print(f"\n  Saved {len(seen)} seen IDs to seen.json.")
    print("=" * 50)


if __name__ == "__main__":
    main()
