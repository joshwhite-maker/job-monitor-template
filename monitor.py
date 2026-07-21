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

import json
import os
import re
import smtplib
import sys
from datetime import datetime
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
    """Public Ashby posting API — no auth required."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Ashby returns either {"jobPostings": [...]} or {"jobs": [...]} depending on board version
        if isinstance(data, list):
            raw_jobs = data
        else:
            raw_jobs = data.get("jobPostings") or data.get("jobs") or []
        jobs = []
        for job in raw_jobs:
            description = job.get("descriptionPlain", "") or strip_html(
                job.get("descriptionHtml", "")
            )
            jobs.append({
                "id": f"ab-{token}-{job['id']}",
                "title": job["title"],
                "location": job.get("locationName", "") or job.get("location", {}).get("name", ""),
                "url": job.get("jobPostingUrl", job.get("jobUrl", "")),
                "description": description,
            })
        return jobs
    except Exception as e:
        print(f"  [!] Ashby/{token}: {e}")
        return []


def poll_workable(token: str) -> list[dict]:
    """
    Public Workable jobs API — no auth required.
    Paginates via nextPage cursors. Full description fetched on demand
    for high scorers only.
    Token is the company slug from apply.workable.com/SLUG.
    """
    url = f"https://apply.workable.com/api/v3/accounts/{token}/jobs"
    jobs = []
    next_page = None

    while True:
        payload = {"limit": 50, "details": False}
        if next_page:
            payload["nextPage"] = next_page

        try:
            resp = requests.post(url, json=payload,
                                 headers={"Content-Type": "application/json"},
                                 timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [!] Workable/{token}: {e}")
            break

        for job in data.get("results", []):
            if job.get("state", "published") != "published":
                continue

            loc_parts = []
            loc = job.get("location", {})
            if loc.get("city"):
                loc_parts.append(loc["city"])
            if loc.get("country"):
                loc_parts.append(loc["country"])
            location = ", ".join(loc_parts)

            job_url = job.get("url", "")
            if job_url and not job_url.startswith("http"):
                job_url = f"https://apply.workable.com{job_url}"

            jobs.append({
                "id": f"wk-{token}-{job['id']}",
                "_wk_token": token,
                "_wk_shortcode": job.get("shortcode", job["id"]),
                "title": job.get("title", ""),
                "location": location,
                "url": job_url,
                "description": None,
            })

        next_page = data.get("nextPage")
        if not next_page:
            break

    return jobs


def fetch_workable_description(token: str, shortcode: str) -> str:
    """Fetch full job description for a single Workable role."""
    url = f"https://apply.workable.com/api/v3/accounts/{token}/jobs/{shortcode}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        desc = data.get("description", "") or ""
        return strip_html(desc)
    except Exception:
        return ""


POLLERS = {
    "greenhouse": poll_greenhouse,
    "lever": poll_lever,
    "ashby": poll_ashby,
    "workable": poll_workable,
}


# ---------------------------------------------------------------------------
# Description enrichment for high-scoring roles
# ---------------------------------------------------------------------------

def enrich_descriptions(jobs: list[dict], threshold: int) -> list[dict]:
    """
    For roles at or above threshold that don't already have a description,
    fetch it via a second API call (Greenhouse and Workable only — Lever
    and Ashby already include description in the listing response).
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
        elif "_wk_token" in job:
            job["description"] = fetch_workable_description(
                job["_wk_token"], job["_wk_shortcode"]
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

def build_email_body(jobs: list[dict], config: dict) -> str:
    date_str = datetime.now().strftime("%d %b %Y")
    min_score = config.get("min_score_to_highlight", 7)
    enrich_threshold = config.get("enrich_score_threshold", 7)
    scoring_enabled = any("score" in j for j in jobs)

    lines = [
        "<html><body style='max-width:680px;margin:0 auto;font-family:sans-serif'>",
        f"<h2 style='color:#111'>Job Monitor - {date_str}</h2>",
        f"<p style='color:#555'>{len(jobs)} new matching role"
        f"{'s' if len(jobs) != 1 else ''} today.</p>",
    ]

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

            lines.append(
                f"<div style='margin:12px 0;padding:12px 16px;{border};"
                f"background:{bg};border-radius:4px'>"
                f"<strong><a href='{job['url']}' style='color:#111;"
                f"text-decoration:none;font-size:1.05em'>{job['title']}</a></strong><br>"
                f"<span style='color:#555;font-size:0.95em'>{job['company']}"
                + (f" &nbsp;·&nbsp; {job['location']}" if job["location"] else "")
                + (f"<br>{score_str}" if score_str else "")
                + "</span>"
                + desc_html
                + "</div>"
            )

    lines.append("</body></html>")
    return "\n".join(lines)


def send_email(jobs: list[dict], config: dict):
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    date_str = datetime.now().strftime("%d %b %Y")
    subject = f"Job Monitor: {len(jobs)} new role{'s' if len(jobs) != 1 else ''} - {date_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(build_email_body(jobs, config), "html"))

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

    # --- Poll each ATS ---
    all_jobs: list[dict] = []
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
        all_jobs.extend(jobs)

    print(f"\n  Total roles fetched: {len(all_jobs)}")

    # --- Filter and dedupe ---
    new_jobs: list[dict] = []
    for job in all_jobs:
        if not passes_filter(job, config):
            continue
        if job["id"] in seen:
            continue
        new_jobs.append(job)
        seen[job["id"]] = datetime.now().isoformat()

    print(f"  New matching roles: {len(new_jobs)}")

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

    # --- Output ---
    has_email_creds = os.environ.get("EMAIL_FROM") and os.environ.get("EMAIL_APP_PASSWORD")

    if has_email_creds:
        send_email(new_jobs, config)
    else:
        print("\n  No email credentials set - printing results:\n")
        for job in new_jobs:
            score = f"  [{job.get('score')}/10]" if "score" in job else ""
            print(f"  {job['title']} - {job['company']} ({job['location'] or '?'}){score}")
            if job.get("rationale"):
                print(f"  {job['rationale']}")
            print(f"  {job['url']}\n")

    # --- Persist seen IDs ---
    save_json(SEEN_FILE, seen)
    print(f"\n  Saved {len(seen)} seen IDs to seen.json.")
    print("=" * 50)


if __name__ == "__main__":
    main()