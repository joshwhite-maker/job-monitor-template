#!/usr/bin/env python3
"""
ATS Health Check - weekly verification that all companies in companies.json
are returning live data from their respective ATS APIs.

Emails a summary report: which companies are live, which are dead (404/error),
and a count of open roles per company. Useful for catching token rot before
it silently drops companies from your daily monitor.

Run manually:  python health_check.py
Run on schedule: GitHub Actions - recommended weekly (Sunday 7am UTC)
"""

import json
import os
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "companies.json"


def load_json(path: Path, default=None):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}


# ---------------------------------------------------------------------------
# ATS checkers — each returns (live: bool, job_count: int | None, detail: str)
# ---------------------------------------------------------------------------

def check_greenhouse(token: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            count = len(resp.json().get("jobs", []))
            return True, count, ""
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


def check_lever(token: str):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return True, len(data), ""
            return False, None, "Unexpected response format"
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


def check_ashby(token: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            count = len(resp.json().get("jobs", []))
            return True, count, ""
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


def check_workable(token: str):
    # FIXED 2026-08-24: old POST /api/v3/accounts/{token}/jobs path is
    # retired for public callers (now requires an authenticated bearer
    # token). Current public feed is this GET widget endpoint.
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}"
    try:
        resp = requests.get(url, params={"details": "true"}, timeout=10)
        if resp.status_code == 200:
            jobs = resp.json().get("jobs", [])
            live = [
                j for j in jobs
                if not j.get("state") or j["state"] == "published"
                or "region" in str(j.get("state", "")).lower()
            ]
            return True, len(live), ""
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


def check_smartrecruiters(token: str):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    try:
        resp = requests.get(url, params={"limit": 1}, timeout=10)
        if resp.status_code == 200:
            return True, resp.json().get("totalFound", 0), ""
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


def check_personio(token: str):
    url = f"https://{token}.jobs.personio.de/xml?language=en"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            return True, len(root.findall("position")), ""
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


def check_teamtailor(token: str):
    url = f"https://{token}.teamtailor.com/jobs.json"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return True, len(resp.json().get("items", [])), ""
        return False, None, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, None, str(e)[:80]


CHECKERS = {
    "greenhouse": check_greenhouse,
    "lever": check_lever,
    "ashby": check_ashby,
    "workable": check_workable,
    "smartrecruiters": check_smartrecruiters,
    "personio": check_personio,
    "teamtailor": check_teamtailor,
}


# ---------------------------------------------------------------------------
# Email report
# ---------------------------------------------------------------------------

def build_report(results: list[dict]) -> str:
    date_str = datetime.now().strftime("%d %b %Y")
    live = [r for r in results if r["live"]]
    dead = [r for r in results if not r["live"]]

    lines = [
        "<html><body style='max-width:680px;margin:0 auto;font-family:sans-serif'>",
        f"<h2 style='color:#111'>ATS Health Check - {date_str}</h2>",
        f"<p style='color:#555'>"
        f"<strong style='color:#16a34a'>{len(live)} live</strong> &nbsp;|&nbsp; "
        f"<strong style='color:#{'dc2626' if dead else '16a34a'}'>{len(dead)} dead</strong> "
        f"&nbsp;|&nbsp; {len(results)} total</p>",
    ]

    if dead:
        lines.append("<h3 style='color:#dc2626;margin-top:24px'>Dead / Broken</h3>")
        for r in dead:
            lines.append(
                f"<div style='margin:8px 0;padding:10px 14px;"
                f"border-left:4px solid #dc2626;background:#fff5f5;border-radius:4px'>"
                f"<strong>{r['name']}</strong> "
                f"<span style='color:#888;font-size:0.9em'>({r['ats']}/{r['token']})</span><br>"
                f"<span style='color:#dc2626;font-size:0.9em'>{r['detail'] or 'No response'}</span>"
                f"</div>"
            )

    lines.append("<h3 style='color:#16a34a;margin-top:24px'>Live</h3>")

    # Group live by sector
    by_sector: dict[str, list] = {}
    for r in live:
        sector = r.get("sector", "Other")
        by_sector.setdefault(sector, []).append(r)

    for sector, companies in sorted(by_sector.items()):
        lines.append(f"<p style='margin:16px 0 4px;font-weight:bold;color:#374151'>{sector}</p>")
        for r in companies:
            job_str = f"{r['job_count']} roles" if r["job_count"] is not None else "unknown"
            lines.append(
                f"<div style='margin:4px 0;padding:6px 12px;"
                f"border-left:3px solid #d1fae5;background:#f0fdf4;border-radius:3px'>"
                f"<span style='color:#111'>{r['name']}</span> "
                f"<span style='color:#888;font-size:0.85em'>({r['ats']})</span> "
                f"<span style='color:#16a34a;font-size:0.85em'>{job_str}</span>"
                f"</div>"
            )

    lines.append(
        f"<p style='color:#9ca3af;font-size:0.8em;margin-top:24px'>"
        f"Run at {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC</p>"
    )
    lines.append("</body></html>")
    return "\n".join(lines)


def send_report(results: list[dict]):
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    dead_count = sum(1 for r in results if not r["live"])
    date_str = datetime.now().strftime("%d %b %Y")
    subject = (
        f"ATS Health Check - {date_str} - "
        f"{dead_count} dead" if dead_count else f"ATS Health Check - {date_str} - all live"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(build_report(results), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(email_from, app_password)
        server.sendmail(email_from, email_to, msg.as_string())

    print(f"  Sent: '{subject}'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\nATS Health Check - {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 50)

    companies = load_json(COMPANIES_FILE, [])
    if not companies:
        print("No companies found in companies.json.")
        return

    results = []
    for company in companies:
        name = company.get("name", "Unknown")
        ats = company.get("ats", "").lower()
        token = company.get("token", "")

        checker = CHECKERS.get(ats)
        if not checker:
            print(f"  [?] Unknown ATS '{ats}' for {name} - skipping.")
            continue

        print(f"  Checking {name} ({ats})...", end=" ", flush=True)
        live, job_count, detail = checker(token)
        status = f"OK ({job_count} roles)" if live else f"DEAD - {detail}"
        print(status)

        results.append({
            "name": name,
            "ats": ats,
            "token": token,
            "sector": company.get("sector", ""),
            "live": live,
            "job_count": job_count,
            "detail": detail,
        })

    live_count = sum(1 for r in results if r["live"])
    dead_count = len(results) - live_count
    print(f"\n  Results: {live_count} live, {dead_count} dead out of {len(results)} checked.")

    has_email_creds = os.environ.get("EMAIL_FROM") and os.environ.get("EMAIL_APP_PASSWORD")
    if has_email_creds:
        send_report(results)
    else:
        print("\n  No email credentials - results above.")
        if dead_count:
            print("\n  DEAD companies:")
            for r in results:
                if not r["live"]:
                    print(f"    {r['name']} ({r['ats']}/{r['token']}): {r['detail']}")

    print("=" * 50)


if __name__ == "__main__":
    main()
