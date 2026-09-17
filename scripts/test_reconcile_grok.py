#!/usr/bin/env python3
"""
Regression tests for reconcile_grok.py's handling of malformed Grok/agent
output — specifically the crash-on-wrong-type class of bug.

No test framework dependency (the rest of this repo doesn't use one
either) — plain asserts, non-zero exit on failure.

Run:  python scripts/test_reconcile_grok.py

These call process_company_discoveries()/process_web_job_discoveries()
directly against small in-memory fixtures, never touching the real
companies.json/web_watchlist.json/etc. Tests 1-4 make no network calls
(the type check rejects the entry before any ATS probing happens); test 5
does a real auto_detect() call as a sanity check that the hardening
didn't break the normal path, so it needs network access.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ats_probe  # noqa: E402
import reconcile_grok as rg  # noqa: E402


def test_non_string_company_name_does_not_crash():
    """
    The exact failure mode reproduced live: a company_discoveries.json
    entry with company_name as a non-string scalar used to crash
    reconcile_grok.py with:
        AttributeError: 'int' object has no attribute 'strip'
    on the line `name = (d.get("company_name") or "").strip()`.
    """
    discoveries = [{"discovery_id": "cd_regress_int_name", "company_name": 123, "status": "new"}]
    companies, watchlist = [], []

    summary = rg.process_company_discoveries(discoveries, companies, watchlist, dry_run=True)

    assert discoveries[0]["status"] == "rejected", discoveries[0]
    assert "company_name is a int, not a string" in discoveries[0]["notes"], discoveries[0]
    assert summary["rejected"] == 1
    assert companies == [], "must not promote anything from malformed input"
    assert watchlist == [], "must not watchlist anything from malformed input"
    print("PASS: non-string company_name (int) rejected cleanly, not crashed")


def test_equivalent_non_string_scalar_types_for_company_name():
    """Same failure mode, other plausible wrong types an LLM could emit."""
    for bad_value in (123, 123.45, True, ["Foo Inc"], {"name": "Foo Inc"}):
        discoveries = [{
            "discovery_id": f"cd_regress_{type(bad_value).__name__}",
            "company_name": bad_value,
            "status": "new",
        }]
        companies, watchlist = [], []
        rg.process_company_discoveries(discoveries, companies, watchlist, dry_run=True)
        assert discoveries[0]["status"] == "rejected", (bad_value, discoveries[0])
        assert "not a string" in discoveries[0]["notes"], (bad_value, discoveries[0])
        assert companies == []
    print("PASS: equivalent non-string company_name types (float/bool/list/dict) all rejected cleanly")


def test_non_string_company_and_title_in_job_discoveries():
    """The equivalent failure mode in the web-job-discovery path."""
    discoveries = [
        {"discovery_id": "wj_regress_int_company", "company": 456,
         "title": "Some Role", "job_url": "https://example.com/job/1", "status": "new"},
        {"discovery_id": "wj_regress_int_title", "company": "Some Co",
         "title": 789, "job_url": "https://example.com/job/2", "status": "new"},
    ]
    companies, web_jobs, seen, watchlist = [], [], {}, []

    summary = rg.process_web_job_discoveries(discoveries, companies, web_jobs, seen, watchlist, dry_run=True)

    assert discoveries[0]["status"] == "rejected", discoveries[0]
    assert "company is a int, not a string" in discoveries[0]["notes"], discoveries[0]
    assert discoveries[1]["status"] == "rejected", discoveries[1]
    assert "title is a int, not a string" in discoveries[1]["notes"], discoveries[1]
    assert summary["rejected"] == 2
    assert web_jobs == []
    assert seen == {}
    print("PASS: non-string company/title in job discoveries rejected cleanly, not crashed")


def test_non_string_ats_provider_guess_does_not_crash():
    """
    ats_provider_guess flows into ats_probe.probe(), which used to crash
    on provider.lower() for a non-string provider.
    """
    result = ats_probe.probe(123, "sometoken")
    assert result["confirmed"] is False
    assert "provider is a int, not a string" in result["reason"]
    print("PASS: non-string ats_provider_guess handled by ats_probe.probe(), not crashed")


def test_valid_input_still_processes_normally():
    """
    Sanity check that the hardening didn't break the normal, well-formed
    path. Makes real network calls (auto_detect probes live ATS APIs).
    """
    discoveries = [{
        "discovery_id": "cd_valid_regress_test",
        "company_name": "Some Genuinely New Co That Wont Match Anything Xyzzy12345",
        "status": "new",
        "sector": "fintech",
        "source_url": None,
    }]
    companies, watchlist = [], []
    summary = rg.process_company_discoveries(discoveries, companies, watchlist, dry_run=True)
    assert discoveries[0]["status"] in ("watchlisted", "promoted"), discoveries[0]
    assert summary["rejected"] == 0
    print("PASS: valid input still processes normally (no regression)")


if __name__ == "__main__":
    test_non_string_company_name_does_not_crash()
    test_equivalent_non_string_scalar_types_for_company_name()
    test_non_string_company_and_title_in_job_discoveries()
    test_non_string_ats_provider_guess_does_not_crash()
    test_valid_input_still_processes_normally()
    print("\nAll reconcile_grok regression tests passed.")
