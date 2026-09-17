#!/usr/bin/env python3
"""
Reconcile Grok's raw web-research discoveries into the canonical data.

FIND (Grok)  ->  RECORD EVIDENCE (grok/*.json)  ->  RECONCILE (this script)
                                                          |
                                    validate, dedupe, verify, promote

This script never trusts Grok's output as fact. Every promotion is gated on
either (a) a deterministically re-confirmed ATS token (see ats_probe.py), or
(b) passing basic field/URL validation for a web-sourced job with no
alternative. Nothing here overwrites or deletes existing verified data —
see the module docstrings on why each guard exists.

Run:  python scripts/reconcile_grok.py [--dry-run]

Reads:
  companies.json               canonical company universe (untouched fields
                                on existing entries are never modified)
  seen.json                    job-id dedup ledger shared with monitor.py
  grok/company_discoveries.json   raw, Grok-written
  grok/web_job_discoveries.json   raw, Grok-written
  grok/web_watchlist.json         companies needing web research

Writes (only if not --dry-run):
  companies.json                new entries appended; existing ones untouched
  web_jobs.json                 canonical store for web-discovered jobs
  seen.json                     new web-job ids added (same ledger monitor.py uses)
  grok/company_discoveries.json   status field updated per entry
  grok/web_job_discoveries.json   status field updated per entry
  grok/web_watchlist.json         upserted: new entries, last/next checked dates
  grok/reconciliation_log.jsonl   one JSON line appended per run
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).parent))
import ats_probe  # noqa: E402

ROOT = Path(__file__).parent.parent
COMPANIES_FILE = ROOT / "companies.json"
SEEN_FILE = ROOT / "seen.json"
WEB_JOBS_FILE = ROOT / "web_jobs.json"
GROK_DIR = ROOT / "grok"
COMPANY_DISCOVERIES_FILE = GROK_DIR / "company_discoveries.json"
WEB_JOB_DISCOVERIES_FILE = GROK_DIR / "web_job_discoveries.json"
WATCHLIST_FILE = GROK_DIR / "web_watchlist.json"
LOG_FILE = GROK_DIR / "reconciliation_log.jsonl"

TODAY = datetime.now(timezone.utc).date()
NOW_ISO = datetime.now(timezone.utc).isoformat()

DUPLICATE_NAME_THRESHOLD = 0.87  # SequenceMatcher ratio on normalised names
WATCHLIST_RECHECK_DAYS = 14


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    # grok/*.json are gitignored in a fresh fork (see .gitignore) — only
    # grok/README.md is committed, so nothing guarantees the directory
    # exists yet on a brand new checkout. Reproduced live: a fresh fork
    # with no grok/ directory at all crashes here with FileNotFoundError
    # the first time reconciliation tries to write anything.
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def extract_string_field(d: dict, field: str) -> tuple[str, str | None]:
    """
    Safely pull a string field out of untrusted Grok/agent JSON.

    Returns (value, type_error). type_error is None for a missing/null
    field (that's an existing, already-handled "required field absent"
    case) — it's only set when the field is PRESENT but is the wrong
    type entirely (a number, list, dict, bool, ...), which is a distinct
    and previously unhandled failure mode: reproduced live with
    `"company_name": 123`, which crashed reconcile_grok.py with
    `AttributeError: 'int' object has no attribute 'strip'` because the
    code assumed every value was at worst None, never a wrong-typed
    scalar. Never coerces (e.g. str(123) -> "123") — a hallucinated
    non-string value becoming a plausible-looking name would be worse
    than rejecting it outright.
    """
    raw = d.get(field)
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return "", f"{field} is a {type(raw).__name__}, not a string"
    return raw.strip(), None


def normalise_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"\b(inc|ltd|limited|llc|gmbh|corp|corporation|plc|co)\.?\s*$", "", name)
    name = re.sub(r"[^a-z0-9\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def names_match(a: str, b: str) -> bool:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= DUPLICATE_NAME_THRESHOLD


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]
    return f"{prefix}_{digest}"


def is_plausible_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def url_resolves(url: str, timeout: int = 8) -> bool | None:
    """
    True/False if we could tell, None if we genuinely can't (network failure,
    or a response code that means "blocked", not "doesn't exist").

    401/403/429 are treated as inconclusive rather than broken — confirmed
    against real Grok output: several genuine, live news sites (FinTech
    Futures, EU-Startups, Sifted) return 403 to a scripted HEAD/GET request
    while being completely reachable in a browser. Flagging those as
    "broken" would be a false alarm on real citations.
    """
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 405:  # some sites reject HEAD; retry GET
            resp = requests.get(url, timeout=timeout, stream=True)
        if resp.status_code in (401, 403, 429):
            return None
        return resp.status_code < 400
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Company discovery -> canonical companies.json (or -> watchlist)
# ---------------------------------------------------------------------------

def company_already_known(name: str, companies: list[dict], watchlist: list[dict]) -> str | None:
    for c in companies:
        if names_match(name, c["name"]):
            return f"already in companies.json as {c['name']!r} ({c['ats']})"
    for w in watchlist:
        if names_match(name, w["company"]):
            return f"already on web_watchlist.json as {w['company']!r}"
    return None


def process_company_discoveries(discoveries: list[dict], companies: list[dict],
                                 watchlist: list[dict], dry_run: bool) -> dict:
    summary = {"promoted": 0, "watchlisted": 0, "duplicate": 0, "rejected": 0, "skipped": 0, "malformed": 0}
    watchlist_by_name = {normalise_name(w["company"]): w for w in watchlist}

    for d in discoveries:
        if not isinstance(d, dict):
            # A non-dict entry can't carry a status field to mark as
            # rejected — left exactly as-is in the file (not deleted, not
            # rewritten) so a human can see what Grok actually produced.
            print(f"  [!] company discovery entry is a {type(d).__name__}, not an object — skipped: {d!r:.100}")
            summary["malformed"] += 1
            continue
        if d.get("status") != "new":
            summary["skipped"] += 1
            continue

        name, name_type_error = extract_string_field(d, "company_name")
        if name_type_error:
            d["status"] = "rejected"
            d["notes"] = name_type_error
            summary["rejected"] += 1
            continue
        if not name:
            d["status"] = "rejected"
            d["notes"] = "missing company_name"
            summary["rejected"] += 1
            continue

        dup_reason = company_already_known(name, companies, watchlist)
        if dup_reason:
            d["status"] = "duplicate"
            d["notes"] = dup_reason
            summary["duplicate"] += 1
            continue

        # Citation quality check — informational only. A dead news-article
        # link doesn't mean the company isn't real (and promotion below is
        # gated on an independently, deterministically confirmed live ATS
        # token, not on this citation), but it's worth recording rather
        # than silently presenting an unverifiable source as if it were
        # solid provenance.
        source_url = d.get("source_url")
        url_note = ""
        if source_url and is_plausible_url(source_url):
            if url_resolves(source_url) is False:
                url_note = " [source_url did not resolve — citation could not be verified]"
        elif source_url:
            url_note = " [source_url is not a well-formed URL]"

        # Deterministic-first: see if this company actually has a findable,
        # confirmable ATS before ever asking Grok to browse its careers page.
        hit = ats_probe.auto_detect(name)
        if hit and hit.get("promotable"):
            companies.append({
                "sector": d.get("sector") or "unclassified",
                "name": name,
                "ats": hit["provider"],
                "token": hit["token"],
                "verified": True,
                "source": "grok_company_discovery",
                "source_url": d.get("source_url"),
                "discovered_date": d.get("discovery_date"),
                "ats_confirmed_date": TODAY.isoformat(),
            })
            d["status"] = "promoted"
            d["notes"] = f"auto-detected {hit['provider']}/{hit['token']} ({hit['reason']})" + url_note
            summary["promoted"] += 1
            continue

        # Something matched structurally but identity couldn't be confirmed
        # (typically Ashby/Lever, which expose no company-name field) — real
        # evidence, but not enough to promote unattended. Note it for review
        # rather than silently trusting a slug guess.
        candidate_note = ""
        if hit and not hit.get("promotable"):
            candidate_note = (
                f" Candidate found but not auto-promoted (unconfirmed identity): "
                f"{hit['provider']}/{hit['token']} — {hit['reason']}"
            )

        # No safely-promotable deterministic ATS found — this is exactly the
        # case Grok's web research exists for. Queue it, don't guess.
        key = normalise_name(name)
        if key in watchlist_by_name:
            entry = watchlist_by_name[key]
            entry["last_checked"] = TODAY.isoformat()
            if candidate_note:
                entry["notes"] = (entry.get("notes", "") + " |" + candidate_note).strip(" |")
            d["status"] = "watchlisted"
            d["notes"] = "already on watchlist; refreshed last_checked" + candidate_note + url_note
        else:
            entry = {
                "company": name,
                "reason": "unconfirmed_ats_candidate" if hit else "no_usable_ats_found",
                "priority": "medium",
                "source": "grok_company_discovery",
                "added_date": TODAY.isoformat(),
                "last_checked": TODAY.isoformat(),
                "next_check": (TODAY + timedelta(days=WATCHLIST_RECHECK_DAYS)).isoformat(),
                "status": "active",
                "notes": (
                    f"discovered via {d.get('source_publication') or d.get('source_url') or 'unknown source'}."
                    + candidate_note + url_note
                ),
            }
            watchlist.append(entry)
            watchlist_by_name[key] = entry
            d["status"] = "watchlisted"
            d["notes"] = "no auto-promotable ATS found; added to web_watchlist.json" + candidate_note + url_note
        summary["watchlisted"] += 1

    return summary


# ---------------------------------------------------------------------------
# Web job discovery -> canonical web_jobs.json (or company promotion)
# ---------------------------------------------------------------------------

def company_has_working_ats(name: str, companies: list[dict]) -> dict | None:
    for c in companies:
        if names_match(name, c["name"]) and c.get("verified", True):
            return c
    return None


def job_already_known(job_url: str, web_jobs: list[dict]) -> bool:
    return any(j.get("job_url") == job_url for j in web_jobs)


def process_web_job_discoveries(discoveries: list[dict], companies: list[dict],
                                 web_jobs: list[dict], seen: dict,
                                 watchlist: list[dict], dry_run: bool) -> dict:
    summary = {"promoted": 0, "company_promoted": 0, "superseded_by_ats": 0,
               "duplicate": 0, "rejected": 0, "needs_review": 0, "skipped": 0, "malformed": 0}
    watchlist_by_name = {normalise_name(w["company"]): w for w in watchlist}

    for d in discoveries:
        if not isinstance(d, dict):
            print(f"  [!] web job discovery entry is a {type(d).__name__}, not an object — skipped: {d!r:.100}")
            summary["malformed"] += 1
            continue
        if d.get("status") != "new":
            summary["skipped"] += 1
            continue

        company, company_type_error = extract_string_field(d, "company")
        title, title_type_error = extract_string_field(d, "title")
        job_url = d.get("job_url")

        type_errors = [e for e in (company_type_error, title_type_error) if e]
        if type_errors:
            d["status"] = "rejected"
            d["notes"] = "; ".join(type_errors)
            summary["rejected"] += 1
            continue

        if not company or not title or not is_plausible_url(job_url):
            d["status"] = "rejected"
            d["notes"] = "missing company, title, or a well-formed job_url"
            summary["rejected"] += 1
            continue

        # Guardrail: never duplicate a company the deterministic monitor
        # already covers. That's strictly worse — slower, less reliable,
        # and it's exactly the busywork this system exists to avoid.
        existing = company_has_working_ats(company, companies)
        if existing:
            d["status"] = "superseded_by_ats"
            d["notes"] = f"{company} already monitored via {existing['ats']} — ignoring web discovery"
            summary["superseded_by_ats"] += 1
            continue

        if job_already_known(job_url, web_jobs):
            d["status"] = "duplicate"
            d["notes"] = "job_url already in web_jobs.json"
            summary["duplicate"] += 1
            continue

        resolves = url_resolves(job_url)
        if resolves is not True:
            # False (non-2xx/3xx) or None (couldn't even connect — often a
            # sign the URL is fake, not a transient blip) both mean "don't
            # trust this without a human/Claude looking at it." Promoting
            # on an unconfirmed URL is exactly the kind of guess rule 8
            # rules out — review, don't guess.
            d["status"] = "needs_review"
            d["notes"] = (
                "job_url did not resolve — left for manual review, not auto-promoted"
                if resolves is False else
                "job_url could not be reached (connection/DNS failure) — left for manual review, not auto-promoted"
            )
            summary["needs_review"] += 1
            continue

        # If Grok found (or we can confirm) an actual ATS while it was
        # looking at the careers page, that's worth more than this one job —
        # it means the deterministic monitor can take over this company
        # going forward.
        ats_hit = None
        if d.get("ats_provider_guess") and d.get("ats_token_guess"):
            result = ats_probe.probe(d["ats_provider_guess"], d["ats_token_guess"], expected_name=company)
            # A hint came from Grok actually looking at the page, so it gets
            # the benefit of the doubt unless the API's own identity check
            # actively contradicts it.
            if result.get("confirmed") and result.get("identity_confirmed") is not False:
                ats_hit = result

        if ats_hit:
            companies.append({
                "sector": "unclassified",
                "name": company,
                "ats": ats_hit["provider"],
                "token": ats_hit["token"],
                "verified": True,
                "source": "grok_web",
                "source_url": d.get("source_url") or d.get("careers_url"),
                "discovered_date": d.get("discovery_date"),
                "ats_confirmed_date": TODAY.isoformat(),
            })
            summary["company_promoted"] += 1
            key = normalise_name(company)
            if key in watchlist_by_name:
                watchlist_by_name[key]["status"] = "resolved"
                watchlist_by_name[key]["notes"] = (
                    watchlist_by_name[key].get("notes", "") +
                    f" | resolved {TODAY.isoformat()}: ATS confirmed ({ats_hit['provider']}/{ats_hit['token']})"
                ).strip(" |")

        job_id = stable_id("web", company, title, job_url)
        web_jobs.append({
            "id": job_id,
            "company": company,
            "title": title,
            "location": d.get("location"),
            "employment_type": d.get("employment_type"),
            "job_url": job_url,
            "careers_url": d.get("careers_url"),
            "source": "grok_web",
            "source_url": d.get("source_url"),
            "discovery_method": d.get("discovery_method"),
            "discovery_date": d.get("discovery_date"),
            "promoted_date": TODAY.isoformat(),
            "evidence": d.get("evidence"),
            "status": "active",
        })
        seen[job_id] = NOW_ISO
        d["status"] = "promoted"
        d["notes"] = "promoted to web_jobs.json" + (
            f"; company also promoted to companies.json via {ats_hit['provider']}" if ats_hit else ""
        )
        summary["promoted"] += 1

        if not ats_hit:
            key = normalise_name(company)
            if key not in watchlist_by_name:
                entry = {
                    "company": company,
                    "reason": "no_machine_readable_feed",
                    "priority": "medium",
                    "source": "grok_web_job_discovery",
                    "added_date": TODAY.isoformat(),
                    "last_checked": TODAY.isoformat(),
                    "next_check": (TODAY + timedelta(days=WATCHLIST_RECHECK_DAYS)).isoformat(),
                    "status": "active",
                    "notes": "job found via web research; no usable ATS identified",
                }
                watchlist.append(entry)
                watchlist_by_name[key] = entry
            else:
                watchlist_by_name[key]["last_checked"] = TODAY.isoformat()

    return summary


# ---------------------------------------------------------------------------
# Watchlist self-healing: cheap deterministic re-probes for companies we
# previously couldn't place, in case a token surfaces later. This is NOT
# the same as asking Grok to re-browse — it's a handful of HTTP calls
# against a (small, by design) watchlist, same cost class as health_check.py
# already pays against the full 214-company universe every week.
# ---------------------------------------------------------------------------

def reprobe_watchlist(watchlist: list[dict], companies: list[dict]) -> dict:
    summary = {"resolved": 0, "flagged_candidate": 0, "checked": 0}
    reprobe_reasons = (
        "no_usable_ats_found", "ats_dead", "no_machine_readable_feed", "unconfirmed_ats_candidate",
    )
    for entry in watchlist:
        if entry.get("status") != "active":
            continue
        if entry.get("reason") not in reprobe_reasons:
            continue
        summary["checked"] += 1
        hit = ats_probe.auto_detect(entry["company"])
        entry["last_checked"] = TODAY.isoformat()
        entry["next_check"] = (TODAY + timedelta(days=WATCHLIST_RECHECK_DAYS)).isoformat()

        if hit and hit.get("promotable"):
            companies.append({
                "sector": "unclassified",
                "name": entry["company"],
                "ats": hit["provider"],
                "token": hit["token"],
                "verified": True,
                "source": "watchlist_reprobe",
                "ats_confirmed_date": TODAY.isoformat(),
            })
            entry["status"] = "resolved"
            entry["notes"] = (entry.get("notes", "") +
                               f" | resolved {TODAY.isoformat()}: ATS confirmed on reprobe "
                               f"({hit['provider']}/{hit['token']})").strip(" |")
            summary["resolved"] += 1
        elif hit and not hit.get("promotable") and entry.get("reason") != "unconfirmed_ats_candidate":
            # Found something real but can't confirm identity (Ashby/Lever
            # slug guess) — flag it rather than silently promoting.
            entry["reason"] = "unconfirmed_ats_candidate"
            entry["notes"] = (entry.get("notes", "") +
                               f" | reprobe {TODAY.isoformat()}: unconfirmed candidate "
                               f"{hit['provider']}/{hit['token']} — {hit['reason']}").strip(" |")
            summary["flagged_candidate"] += 1
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_discovery_list(path: Path) -> tuple[list, bool]:
    """
    Load a Grok landing file defensively. Returns (list_to_process, shape_ok).

    Grok output is untrusted input — if the file's root isn't a JSON array
    (e.g. wrapped as {"discoveries": [...]}, a common LLM mistake) this must
    not crash the whole reconciliation run. When shape_ok is False, the
    caller must NOT write anything back to this file: the original,
    however malformed, is left completely untouched on disk rather than
    silently replaced with an empty list — discoveries are never deleted,
    including broken ones, so a human can recover/fix them.
    """
    raw = load_json(path, [])
    if not isinstance(raw, list):
        print(f"  [!] {path.relative_to(ROOT)}: root is a {type(raw).__name__}, not a list — "
              f"this file will NOT be processed or overwritten this run. Fix it manually.")
        return [], False
    return raw, True


def main():
    dry_run = "--dry-run" in sys.argv

    companies = load_json(COMPANIES_FILE, [])
    seen = load_json(SEEN_FILE, {})
    web_jobs = load_json(WEB_JOBS_FILE, [])
    company_discoveries, company_discoveries_ok = load_discovery_list(COMPANY_DISCOVERIES_FILE)
    web_job_discoveries, web_job_discoveries_ok = load_discovery_list(WEB_JOB_DISCOVERIES_FILE)
    watchlist = load_json(WATCHLIST_FILE, [])

    companies_before = len(companies)
    web_jobs_before = len(web_jobs)
    watchlist_before = len(watchlist)

    print(f"\nReconcile Grok — {NOW_ISO}")
    print("=" * 60)
    print(f"  companies.json: {companies_before} | web_jobs.json: {web_jobs_before} | "
          f"watchlist: {watchlist_before}")
    print(f"  new company discoveries: "
          f"{sum(1 for d in company_discoveries if isinstance(d, dict) and d.get('status') == 'new')}")
    print(f"  new web job discoveries: "
          f"{sum(1 for d in web_job_discoveries if isinstance(d, dict) and d.get('status') == 'new')}")

    company_summary = process_company_discoveries(company_discoveries, companies, watchlist, dry_run)
    job_summary = process_web_job_discoveries(
        web_job_discoveries, companies, web_jobs, seen, watchlist, dry_run
    )
    reprobe_summary = reprobe_watchlist(watchlist, companies)

    print("\n  Company discoveries:", company_summary)
    print("  Web job discoveries:", job_summary)
    print("  Watchlist reprobe:  ", reprobe_summary)
    print(f"\n  companies.json: {companies_before} -> {len(companies)}")
    print(f"  web_jobs.json:  {web_jobs_before} -> {len(web_jobs)}")
    print(f"  watchlist:      {watchlist_before} -> {len(watchlist)}")

    log_entry = {
        "run_at": NOW_ISO,
        "dry_run": dry_run,
        "company_discoveries": company_summary,
        "web_job_discoveries": job_summary,
        "watchlist_reprobe": reprobe_summary,
        "companies_count": {"before": companies_before, "after": len(companies)},
        "web_jobs_count": {"before": web_jobs_before, "after": len(web_jobs)},
        "watchlist_count": {"before": watchlist_before, "after": len(watchlist)},
    }

    if dry_run:
        print("\n  --dry-run: no files written.")
    else:
        save_json(COMPANIES_FILE, companies)
        save_json(WEB_JOBS_FILE, web_jobs)
        save_json(SEEN_FILE, seen)
        if company_discoveries_ok:
            save_json(COMPANY_DISCOVERIES_FILE, company_discoveries)
        if web_job_discoveries_ok:
            save_json(WEB_JOB_DISCOVERIES_FILE, web_job_discoveries)
        save_json(WATCHLIST_FILE, watchlist)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        print(f"\n  Wrote updates. Logged to {LOG_FILE.relative_to(ROOT)}")

    print("=" * 60)


if __name__ == "__main__":
    main()
