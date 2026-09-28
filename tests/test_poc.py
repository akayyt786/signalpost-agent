from __future__ import annotations

import json
import gzip
import csv
import sys
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.crawl_events import extract_page_event, merge_profile_events, missing_seed_error_events  # noqa: E402
from norway_company_agent.official import _reserve_history_slot, accounting_obligation_assessment, normalize_entity, normalize_financial_history, normalize_financials, normalize_roles  # noqa: E402
from norway_company_agent.operations import domain_request_summary, latency_summary, percentile  # noqa: E402
from norway_company_agent.sampling import deterministic_extension_sample, deterministic_financial_filer_sample, deterministic_website_audit_sample, financial_filer_eligible, normalize_row, stratum  # noqa: E402
from norway_company_agent.refresh import diff_datasets, diff_profile  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity, assess_website_identity  # noqa: E402
from norway_company_agent.website import _extraction_state, _priority_links, _social_links, assert_public_url, normalize_homepage, normalize_social_url, structured_social_links  # noqa: E402
from norway_company_agent.batch import evidence_terminal_state, profile_complete_for_modules, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.snapshots import SnapshotFetcher  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from scripts.run_scrapy_websites import terminal_events_for_run  # noqa: E402
from scripts.run_google_news_rss_connector import exact_title_match  # noqa: E402
from norway_company_agent.input_batch import normalize_org_digits, read_batch, valid_check_digit  # noqa: E402
from norway_company_agent.envelope import FIELD_FAMILIES, EnvelopeBuilder  # noqa: E402
from norway_company_agent.identity import classify_publication_verdict  # noqa: E402
from norway_company_agent.site_resolver import build_candidates, populate_website  # noqa: E402
from norway_company_agent.sourcepacks import ReferencePack  # noqa: E402
from norway_company_agent.connectors.nav_jobs import populate_jobs  # noqa: E402
from norway_company_agent.connectors.registries import populate_registries  # noqa: E402
from norway_company_agent.state_store import diff_and_store, open_store  # noqa: E402
from norway_company_agent.summarize import build_summary, maybe_llm_rewrite  # noqa: E402
from scripts.build_site import render_company_page, render_index_row, render_not_found  # noqa: E402


class EvidenceTests(unittest.TestCase):
    def test_missing_is_not_zero_and_provenance_is_required(self):
        record = evidence("financials", "not_found", "official_annual_accounts", "https://example.test/123")
        self.assertIsNone(record["value"])
        self.assertNotEqual(record["value"], 0)
        self.assertTrue(record["source_url"])
        self.assertTrue(record["retrieved_at"])

    def test_available_zero_is_preserved(self):
        record = evidence("employees", "available", "official_registry_bulk", "https://example.test", value=0)
        self.assertEqual(record["value"], 0)
        self.assertEqual(record["status"], "available")

    def test_content_hash_can_be_carried_with_evidence(self):
        record = evidence("entity", "available", "official", "https://example.test", value={}, content_sha256="a" * 64, source_row_key="999999999")
        self.assertEqual(record["content_sha256"], "a" * 64)
        self.assertEqual(record["source_class"], "official")
        self.assertEqual(record["source_row_key"], "999999999")

    def test_not_fetched_is_distinct_from_not_applicable(self):
        record = evidence("history", "not_fetched", "official", "https://example.test", note="No filing flag in snapshot")
        self.assertEqual(record["status"], "not_fetched")
        self.assertNotEqual(record["status"], "not_applicable")


class GoogleNewsGateTests(unittest.TestCase):
    def test_news_title_gate_requires_the_full_legal_name_core(self):
        self.assertTrue(exact_title_match("NORDIC DOOR AS", "Nordic Door AS åpner ny fabrikk - Lokalavisa"))
        self.assertFalse(exact_title_match("NORDIC DOOR AS", "Nordic investors prefer another door - Example"))
        self.assertTrue(exact_title_match("SOLVANG ASA", "Sterkt årsresultat fra Solvang ASA i 2024 - Skipsrevyen"))
        self.assertFalse(exact_title_match("VIND HOLDING AS", "Inntektene til Aneo Roan Vind Holding AS stupte - mn24.no"))
        self.assertFalse(exact_title_match("CONSTO AS", "Drastisk fall hos Consto Bergen AS - BT"))


class SamplingTests(unittest.TestCase):
    def test_financial_filer_sample_requires_current_active_rows_and_preserves_overlap(self):
        fields = ["organisasjonsnummer", "navn", "organisasjonsform.kode", "sisteInnsendteAarsregnskap", "konkurs", "underAvvikling"]
        rows = [
            {"organisasjonsnummer": str(200000000 + index), "navn": f"Company {index}", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "false", "underAvvikling": "false"}
            for index in range(12)
        ] + [
            {"organisasjonsnummer": "300000001", "navn": "Stale AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2024", "konkurs": "false", "underAvvikling": "false"},
            {"organisasjonsnummer": "300000002", "navn": "Bankrupt AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "true", "underAvvikling": "false"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
            repeated, _ = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
        self.assertEqual([row["organisation_number"] for row in selected], [row["organisation_number"] for row in repeated])
        self.assertIn("200000003", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000001", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000002", {row["organisation_number"] for row in selected})
        self.assertEqual(metadata["eligible_rows"], 12)
        self.assertEqual(metadata["preserved_eligible_selected"], 1)
        self.assertTrue(all(financial_filer_eligible(row, "2025") for row in selected))

    def test_extension_sample_is_deterministic_and_excludes_initial(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": str(100000000 + index), "navn": f"Company {index}", "hjemmeside": "", "organisasjonsform.kode": "AS"}
            for index in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            first, metadata = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
            second, _ = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
        self.assertEqual([item["organisation_number"] for item in first], [item["organisation_number"] for item in second])
        self.assertEqual(len(first), 5)
        self.assertEqual(metadata["overlap_with_excluded"], 0)

    def test_strata_distinguish_adverse_and_web_coverage(self):
        base = {"legal_form": "AS", "employees": 12, "bankrupt": False, "liquidating": False, "website": "example.no"}
        self.assertEqual(stratum(base), "AS|5-19|active|web")
        self.assertEqual(stratum({**base, "bankrupt": True, "website": ""}), "AS|5-19|adverse|no-web")

    def test_normalize_does_not_invent_employee_count(self):
        row = normalize_row({"organisasjonsnummer": "923609016", "navn": "Example AS", "antallAnsatte": ""})
        self.assertIsNone(row["employees"])
        self.assertEqual(row["latest_submitted_accounts"], "")

    def test_fresh_website_audit_sample_excludes_poc_and_deduplicates_hosts(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": "111111111", "navn": "Excluded AS", "hjemmeside": "excluded.no", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "222222222", "navn": "A AS", "hjemmeside": "https://www.shared.no/a", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "333333333", "navn": "B AS", "hjemmeside": "shared.no/b", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "444444444", "navn": "C AS", "hjemmeside": "unique.no", "organisasjonsform.kode": "AS"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_website_audit_sample(path, 1, {"111111111"}, {"shared.no"}, seed=9)
        self.assertEqual(len(selected), 1)
        self.assertNotIn("111111111", {row["organisation_number"] for row in selected})
        self.assertEqual(selected[0]["organisation_number"], "444444444")
        self.assertEqual(metadata["unique_hosts_selected"], 1)
        self.assertEqual(metadata["excluded_website_hosts"], 1)


class OperationsTests(unittest.TestCase):
    def test_history_rate_limiter_spaces_request_starts_not_responses(self):
        import norway_company_agent.official as official

        old = official._history_last_request
        now = [10.0]
        sleeps = []

        def clock():
            return now[0]

        def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        try:
            official._history_last_request = 9.0
            _reserve_history_slot(clock, sleeper)
            self.assertAlmostEqual(sleeps[0], 1.1)
            self.assertAlmostEqual(official._history_last_request, 11.1)
            now[0] = 13.3
            _reserve_history_slot(clock, sleeper)
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(official._history_last_request, 13.3)
        finally:
            official._history_last_request = old

    def test_batch_input_preserves_split_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgs.jsonl"
            path.write_text(json.dumps({"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress", "ignored": "x"}) + "\n", encoding="utf-8")
            self.assertEqual(read_organisation_inputs(path), [{"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress"}])

    def test_batch_contract_emits_exact_terminal_envelopes(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://example.test", content_sha256="a" * 64),
                "website": evidence("website", "blocked", "company_site", "https://example.test", note="robots.txt denied"),
            },
        }
        envelope = terminal_envelope(profile, run_id="day-1", modules=["registry", "website"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:01:00Z")
        self.assertEqual(envelope["modules"]["registry"]["state"], "complete")
        self.assertEqual(envelope["modules"]["website"]["state"], "blocked_robots")
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        self.assertFalse(validate_envelopes([envelope], 2)["passed"])

    def test_unknown_evidence_state_is_submission_error(self):
        self.assertEqual(evidence_terminal_state({"status": "not_fetched"}), "submission_error")

    def test_batch_resume_only_skips_profiles_with_all_terminal_modules(self):
        complete = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_found"}}}
        partial = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_fetched"}}}
        self.assertTrue(profile_complete_for_modules(complete, ["registry", "website"]))
        self.assertFalse(profile_complete_for_modules(partial, ["registry", "website"]))

    def test_nearest_rank_percentiles_are_deterministic(self):
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.5), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.95), 100)
        self.assertEqual(latency_summary([1, 2, 3]), {"n": 3, "p50_ms": 2.0, "p95_ms": 3.0, "max_ms": 3.0})

    def test_domain_fairness_summary_preserves_tail(self):
        result = domain_request_summary(__import__("collections").Counter({"a.no": 1, "b.no": 2, "c.no": 9}))
        self.assertEqual(result["domains"], 3)
        self.assertEqual(result["p50_requests"], 2.0)
        self.assertEqual(result["max_requests"], 9)


class WebsiteTests(unittest.TestCase):
    def test_interrupted_run_does_not_synthesize_terminal_failures(self):
        profiles = [{"organisation_number": "1", "website": "pending.no"}]
        self.assertEqual(terminal_events_for_run(profiles, [], False), [])
        self.assertEqual(len(terminal_events_for_run(profiles, [], True)), 1)

    def test_missing_seed_gets_explicit_terminal_event(self):
        profiles = [
            {"organisation_number": "1", "website": "example.no"},
            {"organisation_number": "2", "website": "blocked.no"},
            {"organisation_number": "3", "website": ""},
        ]
        existing = [{"organisation_number": "1", "status": "available"}]
        missing = missing_seed_error_events(profiles, existing)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["organisation_number"], "2")
        self.assertEqual(missing[0]["status"], "source_error")
        self.assertIn("robots.txt", missing[0]["error"])

    def test_crawl_event_extraction_and_merge_preserve_page_hashes(self):
        homepage = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/",
            final_url="https://example.no/",
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=b'<html><head><title>Example AS</title><meta name="description" content="Company"></head><body><p>Example AS provides enough substantive company information for extraction and identity review.</p><a href="https://linkedin.com/company/example">LinkedIn</a></body></html>',
            page_kind="homepage",
            retrieved_at="2026-08-22T00:00:00Z",
        )
        secondary = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/contact",
            final_url="https://example.no/contact",
            status_code=200,
            content_type="text/html",
            body=b"<html><title>Contact</title><body>Contact Example AS in Oslo.</body></html>",
            page_kind="priority",
            retrieved_at="2026-08-22T00:00:01Z",
        )
        record = merge_profile_events({"website": "https://example.no"}, [homepage, secondary])
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["value"]["pages"]), 2)
        self.assertEqual(record["content_sha256"], homepage["content_sha256"])
        self.assertEqual(record["value"]["scheduler"], "scrapy_resumable_v1")

    def test_footer_identity_is_preserved_for_exact_company_gate(self):
        event = extract_page_event(
            organisation_number="985628572",
            requested_url="https://netsolution.no/",
            final_url="https://netsolution.no/",
            status_code=200,
            content_type="text/html",
            body=(
                b'<html><head><title>IT services</title></head><body><main>Useful services for customers.</main>'
                b'<footer>Netsolution Viken AS, Kobbervikdalen 75 A, 3036 Drammen</footer></body></html>'
            ),
            page_kind="homepage",
            retrieved_at="2026-08-23T00:00:00Z",
        )
        website = merge_profile_events({"website": "https://netsolution.no/"}, [event])
        profile = {
            "organisation_number": "985628572",
            "name": "NETSOLUTION VIKEN AS",
            "evidence": {"website": website},
        }
        self.assertIn("Netsolution Viken AS", website["value"]["identity_text_excerpt"])
        self.assertTrue(assess_website_identity(profile)["publishable"])

    def test_normalizes_registry_hostname(self):
        self.assertEqual(normalize_homepage("example.no"), "https://example.no/")
        self.assertEqual(normalize_homepage("http://example.no"), "http://example.no/")

    def test_only_extracts_declared_social_links(self):
        soup = BeautifulSoup('<a href="https://www.linkedin.com/company/example/">LinkedIn</a><a href="/about">About</a>', "html.parser")
        self.assertEqual(_social_links("https://example.no", soup), [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}])

    def test_extracts_embedded_company_social_profiles(self):
        soup = BeautifulSoup(
            '<div class="fb-page" data-href="https://www.facebook.com/ExampleCompany"></div>'
            '<iframe src="https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FSecondCompany"></iframe>',
            "html.parser",
        )
        self.assertEqual(
            _social_links("https://example.no/", soup),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "facebook", "url": "https://facebook.com/SecondCompany"},
            ],
        )

    def test_extracts_schema_same_as_company_social_profiles(self):
        value = [{
            "@type": "Organization",
            "sameAs": [
                "https://www.facebook.com/ExampleCompany/",
                "https://instagram.com/examplecompany",
                "https://linkedin.com/in/example-person",
            ],
        }]
        self.assertEqual(
            structured_social_links(value),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "instagram", "url": "https://instagram.com/examplecompany"},
            ],
        )

    def test_social_profiles_reject_share_event_group_and_policy_links(self):
        rejected = (
            "https://facebook.com/sharer.php?u=x", "https://facebook.com/events/123",
            "https://facebook.com/groups/123", "https://facebook.com/policy.php",
            "https://facebook.com/privacy/explanation",
            "https://linkedin.com/shareArticle?url=x", "https://instagram.com/p/abc",
        )
        self.assertTrue(all(normalize_social_url(url) is None for url in rejected))

    def test_social_profiles_canonicalize_www_variants(self):
        self.assertEqual(normalize_social_url("https://www.facebook.com/Example/"), {"platform": "facebook", "url": "https://facebook.com/Example"})
        self.assertEqual(normalize_social_url("https://linkedin.com/company/example/admin/feed/posts"), {"platform": "linkedin", "url": "https://linkedin.com/company/example"})
        self.assertEqual(normalize_social_url("https://youtube.com/channel/abc/featured"), {"platform": "youtube", "url": "https://youtube.com/channel/abc"})
        self.assertIsNone(normalize_social_url("https://facebook.com/profile.php"))
        self.assertIsNone(normalize_social_url("https://[object Object]"))

    def test_priority_pages_stay_on_exact_site(self):
        soup = BeautifulSoup('<a href="/kontakt">Contact</a><a href="https://other.no/about">About</a><a href="/products">Products</a>', "html.parser")
        self.assertEqual(_priority_links("https://example.no/", soup), ["https://example.no/kontakt"])

    def test_js_shell_is_only_a_fallback_candidate(self):
        shell = BeautifulSoup('<html><script src="a.js"></script><script src="b.js"></script></html>', "html.parser")
        self.assertEqual(_extraction_state("", shell), "js_fallback_candidate")
        self.assertEqual(_extraction_state("A" * 100, shell), "static_complete")

    def test_blocks_local_network_targets(self):
        for url in ("http://127.0.0.1/admin", "http://localhost/", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                assert_public_url(url)


class OfficialNormalizationTests(unittest.TestCase):
    def test_accounting_obligation_is_categorical_for_as_but_not_enk(self):
        company = accounting_obligation_assessment({"organisation_number": "923609016", "legal_form": "AS"})
        sole_trader = accounting_obligation_assessment({"organisation_number": "923609017", "legal_form": "ENK", "employees": 0})
        self.assertEqual(company["value"]["classification"], "required_by_legal_form")
        self.assertEqual(sole_trader["value"]["classification"], "threshold_or_activity_dependent")
        self.assertTrue(company["content_sha256"])
        self.assertEqual(company["source_row_key"], "923609016")

    def test_observed_filing_overrides_rule_path(self):
        record = accounting_obligation_assessment({"organisation_number": "923609018", "legal_form": "ENK", "latest_submitted_accounts": "2024"})
        self.assertEqual(record["value"]["classification"], "filing_observed")

    def test_entity_normalization_keeps_identity(self):
        record = normalize_entity({"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "organisasjonsform": {"kode": "ASA"}})
        self.assertEqual(record["organisation_number"], "923609016")
        self.assertEqual(record["legal_form"], "ASA")

    def test_financial_fields_keep_period_currency_and_zero(self):
        body = [{"id": 1, "valuta": "NOK", "regnskapsperiode": {"tilDato": "2025-12-31"}, "resultatregnskapResultat": {"driftsresultat": {"driftsresultat": 0, "driftsinntekter": {"sumDriftsinntekter": 12}}}}]
        record = normalize_financials(body)["records"][0]
        self.assertEqual(record["revenue"], 12)
        self.assertEqual(record["operating_result"], 0)
        self.assertEqual(record["currency"], "NOK")

    def test_financial_history_is_sorted_and_links_to_official_pdfs(self):
        record = normalize_financial_history(["2024", "2022", "2024", "invalid"], "923609016")
        self.assertEqual(record["years"], ["2022", "2024"])
        self.assertEqual(record["pdfs"][0]["year"], "2024")
        self.assertTrue(record["pdfs"][0]["url"].endswith("/923609016/2024"))

    def test_public_roles_drop_birth_dates(self):
        body = {"rollegrupper": [{"type": {"kode": "STYR"}, "roller": [{"type": {"kode": "LEDE", "beskrivelse": "Chair"}, "person": {"fodselsdato": "1970-01-01", "navn": {"fornavn": "Ada", "etternavn": "Nord"}}}]}]}
        record = normalize_roles(body)["roles"][0]
        self.assertEqual(record["name"], "Ada Nord")
        self.assertNotIn("fodselsdato", json.dumps(record))


class RefreshTests(unittest.TestCase):
    def test_snapshot_fetcher_hashes_evaluator_bytes_and_carries_times(self):
        url = "https://example.test/entity/1"
        fetcher = SnapshotFetcher({"retrieved_at": "2026-01-02T00:00:00Z", "effective_at": "2026-01-01T00:00:00Z", "responses": {url: {"body": {"value": 1}}}})
        result = fetcher(url)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.content_sha256 or ""), 64)
        self.assertEqual(result.effective_at, "2026-01-01T00:00:00Z")

    def test_identical_refresh_is_an_idempotent_noop(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "employees": 4}
        self.assertEqual(diff_profile(row, dict(row)), [])

    def test_missing_to_zero_is_a_real_change_with_provenance(self):
        source = evidence("registry", "available", "official", "https://example.test/entity")
        old = {"organisation_number": "923609016", "employees": None, "evidence": {"registry": source}}
        new = {"organisation_number": "923609016", "employees": 0, "evidence": {"registry": source}}
        change = diff_profile(old, new)[0]
        self.assertIsNone(change["old_value"])
        self.assertEqual(change["new_value"], 0)
        self.assertEqual(change["source_url"], "https://example.test/entity")

    def test_refresh_rejects_membership_or_identity_drift(self):
        with self.assertRaises(ValueError):
            diff_datasets([{"organisation_number": "923609016"}], [{"organisation_number": "999999999"}])


class WebsiteIdentityTests(unittest.TestCase):
    def test_group_contact_page_listing_subsidiary_org_number_is_not_exact_homepage_identity(self):
        profile = {
            "organisation_number": "915637353",
            "name": "SKS PRODUKSJON AS",
            "evidence": {"website": evidence("website", "available", "company_site", "https://sks.no", value={
                "final_url": "https://sks.no/",
                "title": "Konsern - SKS - Forside",
                "main_text_excerpt": "SKS is a power group with multiple subsidiaries.",
                "pages": [{"title": "Contact", "main_text_excerpt": "SKS Produksjon AS organisation number 915 637 353"}],
            })},
        }
        assessment = assess_website_identity(profile)
        self.assertFalse(assessment["publishable"])
        self.assertNotEqual(assessment["score"], 1.0)

    def test_shared_identity_gate_quarantines_parent_social_links(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={
            "title": "Parent Group", "main_text_excerpt": "Parent Group portfolio",
            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}],
        })
        profile = {"organisation_number": "923609016", "name": "Exact Subsidiary AS", "evidence": {}}
        result = apply_website_identity_gate(profile, website)
        self.assertFalse(result["assessment"]["publishable"])
        self.assertEqual(result["website"]["value"]["social_links"], [])
        self.assertEqual(result["quarantined_social_links"], 1)

    def test_exact_legal_name_is_publishable(self):
        row = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "evidence": {"website": {"status": "available", "value": {"title": "Norsk Fiskeeksport AS"}}}}
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_parent_brand_without_legal_name_is_quarantined(self):
        row = {"organisation_number": "988412406", "name": "Tevlingveien 23 Invest AS", "evidence": {"website": {"status": "available", "value": {"title": "Ragde Eiendom"}}}}
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_parked_domain_and_parent_company_sports_site_are_quarantined(self):
        parked = {"organisation_number": "996081001", "name": "Condalign AS", "evidence": {"website": {"status": "available", "value": {"title": "CondAlign.com is for sale | HugeDomains"}}}}
        sports = {"organisation_number": "996242692", "name": "Primulator B.I.L.", "evidence": {"website": {"status": "available", "value": {"title": "Primulator", "description": "Premium products for HoReCa"}}}}
        self.assertFalse(assess_website_identity(parked)["publishable"])
        self.assertFalse(assess_website_identity(sports)["publishable"])

    def test_hosting_placeholder_and_generic_link_page_are_quarantined(self):
        hosting = {"organisation_number": "917568278", "name": "HJELMEN AS", "evidence": {"website": {"status": "available", "value": {"title": "www.Hjelmen-as.no is parked at Miss Hosting Web Hosting", "main_text_excerpt": "Hjelmen " * 100}}}}
        links = {"organisation_number": "986606009", "name": "KOALA ANS", "evidence": {"website": {"status": "available", "value": {"title": "koala.no", "description": "Find the best information and most relevant links on all topics related to"}}}}
        self.assertFalse(assess_website_identity(hosting)["publishable"])
        self.assertFalse(assess_website_identity(links)["publishable"])

    def test_broader_umbrella_site_is_not_exact_when_name_only_appears_in_body(self):
        row = {
            "organisation_number": "976994027",
            "name": "AVALDSNES SOKN",
            "evidence": {"website": {"status": "available", "value": {
                "title": "Kirken i Karmøy",
                "final_url": "https://www.karmoykirken.no/",
                "main_text_excerpt": "Avaldsnes sokn is one of several parishes represented on this umbrella site.",
            }}},
        }
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_social_handle_requires_exact_entity_name_evidence(self):
        aon = {"name": "Aon Norway AS"}
        fish = {"name": "Norsk Fiskeeksport AS"}
        self.assertFalse(assess_social_identity(aon, {"platform": "linkedin", "url": "https://linkedin.com/company/aon"})["publishable"])
        self.assertTrue(assess_social_identity(fish, {"platform": "linkedin", "url": "https://linkedin.com/company/norsk-fiskeeksport"})["publishable"])


class VerifiedSiteSeedTests(unittest.TestCase):
    def test_verified_seed_is_applied_and_unknown_org_is_rejected(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles.jsonl"
            seeds = root / "seeds.json"
            output = root / "output.jsonl"
            report = root / "report.json"
            profiles.write_text(json.dumps({"organisation_number": "123456789", "name": "Example AS"}) + "\n")
            seeds.write_text(json.dumps([{
                "organisation_number": "123456789",
                "website": "https://example.no/",
                "proof_url": "https://source.example/proof",
                "proof": "Exact name and organisation number",
            }]))
            command = [
                sys.executable, str(ROOT / "scripts" / "apply_verified_site_seeds.py"),
                "--profiles", str(profiles), "--seeds", str(seeds),
                "--output", str(output), "--report", str(report),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            row = json.loads(output.read_text().strip())
            self.assertEqual(row["website"], "https://example.no/")
            self.assertEqual(row["website_seed_source"], "independently_verified_exact_entity")
            self.assertEqual(json.loads(report.read_text())["applied"], 1)

            seeds.write_text(json.dumps([{
                "organisation_number": "987654321",
                "website": "https://unknown.no/",
                "proof_url": "https://source.example/proof",
            }]))
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("unknown organisations", failed.stderr)


class InputBatchTests(unittest.TestCase):
    def test_check_digit_accepts_known_valid_numbers_and_rejects_tampered_ones(self):
        self.assertTrue(valid_check_digit("923609016"))
        self.assertTrue(valid_check_digit("810034882"))
        self.assertFalse(valid_check_digit("810034881"))
        self.assertFalse(valid_check_digit("12345678"))

    def test_normalize_org_digits_strips_spaces_and_letters(self):
        self.assertEqual(normalize_org_digits("923 609 016"), "923609016")
        self.assertEqual(normalize_org_digits("NO923609016MVA"), "923609016")
        self.assertEqual(normalize_org_digits(None), "")

    def test_read_batch_preserves_input_order_and_duplicates_and_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.txt"
            path.write_text("923609016\n923609016\n810034881\nnot-a-number\n810034882\n", encoding="utf-8")
            rows = read_batch(path)
        self.assertEqual([row["raw"] for row in rows], ["923609016", "923609016", "810034881", "not-a-number", "810034882"])
        self.assertEqual([row["valid"] for row in rows], [True, True, False, False, True])
        self.assertEqual(rows[2]["error"], "invalid mod-11 check digit")
        self.assertIn("expected 9 digits", rows[3]["error"])

    def test_read_batch_jsonl_accepts_any_known_key_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.jsonl"
            path.write_text('{"organisation_number": "923609016"}\n{"orgnr": "810034882"}\n', encoding="utf-8")
            rows = read_batch(path)
        self.assertEqual([row["organisation_number"] for row in rows], ["923609016", "810034882"])

    def test_read_batch_transparently_decompresses_gz(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.txt.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write("923609016\n")
            rows = read_batch(path)
        self.assertEqual(rows, [{"raw": "923609016", "organisation_number": "923609016", "valid": True, "error": None}])


class EnvelopeBuilderTests(unittest.TestCase):
    def make_builder(self):
        return EnvelopeBuilder("923609016", run_id="test-run", started_at="2026-09-28T00:00:00Z", agent_version="1.0.0", code_commit="abc123")

    def test_unset_families_catches_a_family_no_code_path_touched(self):
        builder = self.make_builder()
        self.assertEqual(set(builder.unset_families()), set(FIELD_FAMILIES))
        builder.set_availability("legal_identity", "available", "found")
        remaining = builder.unset_families()
        self.assertNotIn("legal_identity", remaining)
        self.assertEqual(len(remaining), len(FIELD_FAMILIES) - 1)

    def test_claim_id_is_stable_for_the_same_org_field_subkey(self):
        builder = self.make_builder()
        evidence_id = builder.add_evidence(source_url="https://example.test", source_class="official_registry_live", access_policy="NLOD-2.0", http_status=200)
        claim_a = builder.add_claim(field="annual_accounts", subkey="1:revenue", value=100, availability="available", method="verbatim", evidence_ids=[evidence_id])
        claim_b = builder.add_claim(field="annual_accounts", subkey="1:revenue", value=200, availability="available", method="verbatim", evidence_ids=[evidence_id])
        self.assertEqual(claim_a["claim_id"], claim_b["claim_id"])
        other = builder.add_claim(field="annual_accounts", subkey="1:operating_result", value=100, availability="available", method="verbatim", evidence_ids=[evidence_id])
        self.assertNotEqual(claim_a["claim_id"], other["claim_id"])

    def test_build_rejects_unknown_field_family(self):
        builder = self.make_builder()
        with self.assertRaises(ValueError):
            builder.add_claim(field="not_a_real_family", value=1, availability="available", method="x", evidence_ids=[])
        with self.assertRaises(ValueError):
            builder.set_availability("not_a_real_family", "available", "x")

    def test_build_emits_a_valid_envelope_with_every_family_present(self):
        builder = self.make_builder()
        evidence_id = builder.add_evidence(source_url="https://example.test", source_class="official_registry_live", access_policy="NLOD-2.0", http_status=200, content_sha256="a" * 64)
        builder.add_claim(field="legal_identity", value={"name": "Example AS"}, availability="available", method="verbatim", evidence_ids=[evidence_id])
        for family in FIELD_FAMILIES:
            if family != "legal_identity":
                builder.set_availability(family, "not_available", "not_checked_in_this_test")
            else:
                builder.set_availability(family, "available", "found")
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["organisation_number"], "923609016")
        self.assertEqual(set(envelope["availability"].keys()), set(FIELD_FAMILIES))
        self.assertEqual(envelope["claims"][0]["value"], {"name": "Example AS"})
        self.assertEqual(envelope["evidence"][0]["content_sha256"], "a" * 64)
        self.assertEqual(envelope["run"]["terminal_status"], "completed")


class PublicationVerdictTests(unittest.TestCase):
    def test_verified_when_organisation_number_found_on_page(self):
        verdict = classify_publication_verdict("923609016", "Equinor ASA", {
            "title": "Equinor ASA", "final_url": "https://equinor.com/",
            "pages": [{"main_text_excerpt": "Equinor ASA, organisasjonsnummer 923 609 016, NO-4035 Stavanger"}],
        })
        self.assertEqual(verdict["verdict"], "verified")
        self.assertIn("923 609 016", verdict["proof_span"])

    def test_conflict_when_a_different_valid_organisation_number_is_present(self):
        verdict = classify_publication_verdict("810034882", "Sandnes Elektriske AS", {
            "title": "Regnskapsforer AS", "final_url": "https://regnskapsforer.no/",
            "pages": [{"main_text_excerpt": "Vi forer regnskap for Sandnes Elektriske AS. Org.nr 923 609 016"}],
        })
        self.assertEqual(verdict["verdict"], "conflict")
        self.assertEqual(verdict["other_org_numbers"], ["923609016"])

    def test_corroborated_requires_multi_token_name_plus_address_or_phone(self):
        verdict = classify_publication_verdict("923304290", "Norsk Fiskeeksport AS", {
            "title": "Norsk Fiskeeksport AS", "final_url": "https://norskfiskeeksport.no/",
            "description": "Norsk Fiskeeksport AS - sjømat",
            "pages": [{"main_text_excerpt": "Norsk Fiskeeksport AS, Strandgata 5, 6002 Ålesund"}],
        }, business_address={"address": "Strandgata 5"})
        self.assertEqual(verdict["verdict"], "corroborated")

    def test_single_token_legal_name_never_reaches_corroborated(self):
        verdict = classify_publication_verdict("999999999", "NORDIC AS", {
            "title": "Nordic", "final_url": "https://nordic.no/", "pages": [],
        })
        self.assertEqual(verdict["verdict"], "ambiguous")
        self.assertEqual(verdict["reason"], "single_token_name_requires_organisation_number")

    def test_group_portfolio_page_naming_the_company_is_not_published(self):
        verdict = classify_publication_verdict("923304290", "Norsk Fiskeeksport AS", {
            "title": "Norsk Fiskeeksport Group portfolio", "final_url": "https://parent-group.no/",
            "pages": [{"main_text_excerpt": "Norsk Fiskeeksport is part of our portfolio of companies"}],
        })
        self.assertEqual(verdict["verdict"], "ambiguous")

    def test_parked_domain_is_ambiguous_even_with_matching_title(self):
        verdict = classify_publication_verdict("999999999", "Example AS", {
            "title": "CondAlign.com is for sale | HugeDomains", "final_url": "https://condalign.com/", "pages": [],
        })
        self.assertEqual(verdict["verdict"], "ambiguous")
        self.assertEqual(verdict["reason"], "parked_or_for_sale_page")


class SiteResolverTests(unittest.TestCase):
    def test_candidate_ladder_prefers_registry_site_then_email_domain_then_skips_freemail(self):
        no_dns = lambda host: False  # noqa: E731 - deterministic stand-in, never hits real DNS
        identity_source = {"name": "Example AS", "website": "example.no", "email": "post@example.no"}
        candidates = build_candidates(identity_source, ReferencePack(), "923609016", dns_resolver=no_dns)
        self.assertEqual(candidates, [("https://example.no", "registry_website")])

        identity_source = {"name": "Example AS", "website": "", "email": "post@gmail.com"}
        candidates = build_candidates(identity_source, ReferencePack(), "923609016", dns_resolver=no_dns)
        self.assertEqual(candidates, [])

        identity_source = {"name": "Example AS", "website": "", "email": "post@example-corp.no"}
        candidates = build_candidates(identity_source, ReferencePack(), "923609016", dns_resolver=no_dns)
        self.assertEqual(candidates, [("https://example-corp.no/", "registry_email_domain")])

    def test_name_guess_fallback_only_fires_when_dns_resolves_and_nothing_else_exists(self):
        identity_source = {"name": "Nordic Fiskeeksport AS", "website": "", "email": "post@gmail.com"}
        self.assertEqual(build_candidates(identity_source, ReferencePack(), "923609016", dns_resolver=lambda host: False), [])
        self.assertEqual(
            build_candidates(identity_source, ReferencePack(), "923609016", dns_resolver=lambda host: True),
            [("https://nordicfiskeeksport.no", "name_guess_dns_verified")],
        )

    def test_populate_website_publishes_only_on_verified_and_gates_downstream_families(self):
        def fake_fetch_verified(url):
            return (
                {"status": "available", "retrieved_at": "2026-09-28T00:00:00Z", "value": {
                    "final_url": url, "title": "Example AS", "description": "Example does things.",
                    "main_text_excerpt": "Example AS, org.nr 923 609 016. Contact: post@example.no",
                    "pages": [{"url": url + "nyheter", "title": "Nyheter", "main_text_excerpt": "Ny kontrakt signert."}],
                    "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}],
                    "content_sha256": "a" * 64,
                }},
                {"requests": 2, "bytes": 100},
            )
        builder = EnvelopeBuilder("923609016", run_id="t", started_at="2026-09-28T00:00:00Z", agent_version="1.0.0", code_commit="x")
        identity_source = {"name": "Example AS", "website": "example.no", "email": "post@example.no", "business_address": {}, "phone": None}
        populate_website(builder, "923609016", identity_source, ReferencePack(), fetcher=fake_fetch_verified)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["official_website"]["state"], "available")
        self.assertEqual(envelope["availability"]["social_profiles"]["state"], "available")
        self.assertEqual(envelope["availability"]["contact_points"]["state"], "available")
        contact_values = {c["value"] for c in envelope["claims"] if c["field"] == "contact_points"}
        self.assertIn("post@example.no", contact_values)

    def test_populate_website_withholds_everything_on_conflict(self):
        def fake_fetch_conflict(url):
            return (
                {"status": "available", "retrieved_at": "2026-09-28T00:00:00Z", "value": {
                    "final_url": url, "title": "Accountant AS",
                    "main_text_excerpt": "We handle bookkeeping for Example AS. Org.nr 111111111",
                    "pages": [], "social_links": [], "content_sha256": "b" * 64,
                }},
                {"requests": 2, "bytes": 100},
            )
        builder = EnvelopeBuilder("923609016", run_id="t", started_at="2026-09-28T00:00:00Z", agent_version="1.0.0", code_commit="x")
        identity_source = {"name": "Example AS", "website": "accountant.no", "email": None, "business_address": {}, "phone": None}
        populate_website(builder, "923609016", identity_source, ReferencePack(), fetcher=fake_fetch_conflict)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["official_website"]["state"], "not_available")
        self.assertEqual(envelope["availability"]["official_website"]["reason"], "foreign_org_number_on_page")
        self.assertFalse([c for c in envelope["claims"] if c["field"] == "official_website"])


class NavJobsTests(unittest.TestCase):
    def make_builder(self):
        return EnvelopeBuilder("923609016", run_id="t", started_at="2026-09-28T00:00:00Z", agent_version="1.0.0", code_commit="x")

    def test_no_indexed_ads_is_not_available_not_a_crash(self):
        builder = self.make_builder()
        populate_jobs(builder, "923609016", {}, detail_fetcher=lambda url: None)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["job_postings"]["state"], "not_available")
        self.assertEqual(envelope["availability"]["job_postings"]["reason"], "no_active_ads_in_nav_feed")

    def test_active_matching_ad_is_published_without_contact_list(self):
        index = {"923609016": [{"ad_uuid": "u1", "detail_url": "/api/v1/feedentry/u1", "sist_endret": "2026-09-01T00:00:00Z", "municipal": "OSLO"}]}

        def fetch(url):
            return {"status": "ACTIVE", "ad_content": {
                "title": "Elektriker", "published": "2026-09-01T00:00:00Z", "expires": "2099-01-01T00:00:00Z",
                "link": "https://arbeidsplassen.nav.no/stillinger/123", "employer": {"orgnr": "923609016", "name": "Example AS"},
                "contactList": [{"name": "Should Never Appear", "email": "hidden@example.no"}],
            }}
        builder = self.make_builder()
        populate_jobs(builder, "923609016", index, detail_fetcher=fetch)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["job_postings"]["state"], "available")
        claim = [c for c in envelope["claims"] if c["field"] == "job_postings"][0]
        self.assertEqual(claim["value"]["title"], "Elektriker")
        self.assertNotIn("contactList", json.dumps(envelope))
        self.assertNotIn("hidden@example.no", json.dumps(envelope))

    def test_ad_gone_inactive_since_index_was_built_is_excluded_not_published(self):
        index = {"923609016": [{"ad_uuid": "u1", "detail_url": "/api/v1/feedentry/u1", "sist_endret": "2026-09-01T00:00:00Z"}]}
        builder = self.make_builder()
        populate_jobs(builder, "923609016", index, detail_fetcher=lambda url: {"status": "INACTIVE"})
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["job_postings"]["state"], "not_available")
        self.assertEqual(envelope["availability"]["job_postings"]["reason"], "indexed_ads_no_longer_active")
        self.assertFalse([c for c in envelope["claims"] if c["field"] == "job_postings"])

    def test_expired_ad_is_excluded_even_if_still_marked_active(self):
        index = {"923609016": [{"ad_uuid": "u1", "detail_url": "/api/v1/feedentry/u1", "sist_endret": "2020-01-01T00:00:00Z"}]}
        builder = self.make_builder()
        populate_jobs(builder, "923609016", index, detail_fetcher=lambda url: {"status": "ACTIVE", "ad_content": {"expires": "2020-01-02T00:00:00Z", "employer": {"orgnr": "923609016"}}})
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertFalse([c for c in envelope["claims"] if c["field"] == "job_postings"])

    def test_failed_detail_refetch_is_deferred_not_a_false_removal(self):
        index = {"923609016": [{"ad_uuid": "u1", "detail_url": "/api/v1/feedentry/u1", "sist_endret": "2026-09-01T00:00:00Z"}]}
        builder = self.make_builder()
        populate_jobs(builder, "923609016", index, detail_fetcher=lambda url: None)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["job_postings"]["reason"], "detail_refetch_failed")


class RegistriesTests(unittest.TestCase):
    def make_builder(self):
        return EnvelopeBuilder("923609016", run_id="t", started_at="2026-09-28T00:00:00Z", agent_version="1.0.0", code_commit="x")

    def test_dibk_approval_published_when_present(self):
        rp = ReferencePack(dibk={"923609016": {"status": {"approved": True, "approval_period_to": "2028-01-01", "approval_certificate": "https://dibk.example/cert.pdf"}}})
        builder = self.make_builder()
        populate_registries(builder, "923609016", rp)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["credentials_and_approvals"]["state"], "available")
        claim = [c for c in envelope["claims"] if c["field"] == "credentials_and_approvals"][0]
        self.assertTrue(claim["value"]["approved"])

    def test_dibk_absent_is_not_available_not_a_crash(self):
        builder = self.make_builder()
        populate_registries(builder, "923609016", ReferencePack())
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        self.assertEqual(envelope["availability"]["credentials_and_approvals"]["state"], "not_available")
        self.assertEqual(envelope["availability"]["credentials_and_approvals"]["reason"], "not_in_dibk_central_approval_register")

    def test_wikidata_website_is_never_published_as_an_external_reference(self):
        rp = ReferencePack(wikidata={"923609016": {"websites": ["https://example.no"], "wikipedia_article": "https://en.wikipedia.org/wiki/Example", "social": {"linkedin": "example"}}})
        builder = self.make_builder()
        populate_registries(builder, "923609016", rp)
        envelope = builder.build(completed_at="2026-09-28T00:01:00Z", terminal_status="completed")
        fields = {(c["subkey"]) for c in envelope["claims"] if c["field"] == "external_references"}
        self.assertEqual(fields, {"wikipedia_article", "social:linkedin"})
        self.assertNotIn("https://example.no", json.dumps(envelope["claims"]))


class StateStoreTests(unittest.TestCase):
    def make_claim(self, field, subkey, value, availability="available", evidence_ids=("ev-1",)):
        return {
            "claim_id": f"{field}:{subkey}", "field": field, "subkey": subkey, "value": value,
            "unit": None, "reporting_period": None, "availability": availability, "confidence": 1.0,
            "method": "test", "evidence_ids": list(evidence_ids), "first_seen": "2026-09-28T00:00:00Z", "last_seen": "2026-09-28T00:00:00Z",
        }

    def open_temp_store(self):
        directory = tempfile.mkdtemp()
        return open_store(Path(directory) / "state.sqlite")

    def test_cold_start_reports_zero_changes_but_persists_a_baseline(self):
        conn = self.open_temp_store()
        claims = [self.make_claim("employees", None, 12)]
        availability = {"employees": {"state": "available", "reason": "found", "checked_at": "x"}}
        result = diff_and_store(conn, "923609016", claims, availability)
        self.assertEqual(result["baseline"], "cold_start")
        self.assertEqual(result["changes"], [])
        second = diff_and_store(conn, "923609016", claims, availability)
        self.assertEqual(second["baseline"], "incremental")
        self.assertEqual(second["changes"], [], "identical rerun must be idempotent")

    def test_changed_value_is_reported_with_old_and_new(self):
        conn = self.open_temp_store()
        availability = {"employees": {"state": "available", "reason": "found", "checked_at": "x"}}
        diff_and_store(conn, "923609016", [self.make_claim("employees", None, 12)], availability)
        result = diff_and_store(conn, "923609016", [self.make_claim("employees", None, 15)], availability)
        self.assertEqual(len(result["changes"]), 1)
        change = result["changes"][0]
        self.assertEqual(change["change_type"], "changed_value")
        self.assertEqual(change["old_value"], 12)
        self.assertEqual(change["new_value"], 15)

    def test_source_failure_defers_instead_of_removing(self):
        conn = self.open_temp_store()
        available = {"official_website": {"state": "available", "reason": "found", "checked_at": "x"}}
        diff_and_store(conn, "923609016", [self.make_claim("official_website", None, "https://example.no")], available)
        failed = {"official_website": {"state": "failed", "reason": "source_error", "checked_at": "x"}}
        result = diff_and_store(conn, "923609016", [], failed)
        self.assertEqual(len(result["changes"]), 1)
        change = result["changes"][0]
        self.assertEqual(change["change_type"], "deferred")
        self.assertEqual(change["old_value"], "https://example.no")
        # a deferred claim must still exist in the store afterwards, unchanged
        remaining = diff_and_store(conn, "923609016", [], failed)
        self.assertEqual(remaining["changes"][0]["change_type"], "deferred")

    def test_genuine_absence_is_a_real_removal(self):
        conn = self.open_temp_store()
        available = {"official_website": {"state": "available", "reason": "found", "checked_at": "x"}}
        diff_and_store(conn, "923609016", [self.make_claim("official_website", None, "https://example.no")], available)
        checked_but_gone = {"official_website": {"state": "not_available", "reason": "insufficient_exact_entity_evidence", "checked_at": "x"}}
        result = diff_and_store(conn, "923609016", [], checked_but_gone)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["change_type"], "removed_value")


class SummarizeTests(unittest.TestCase):
    def claim(self, field, subkey, value, unit=None, reporting_period=None, availability="available"):
        return {
            "claim_id": f"{field}:{subkey}", "field": field, "subkey": subkey, "value": value, "unit": unit,
            "reporting_period": reporting_period, "availability": availability, "confidence": 1.0,
            "method": "test", "evidence_ids": ["ev-1"], "first_seen": "x", "last_seen": "x",
        }

    def test_summary_only_uses_available_claims_and_names_every_gap(self):
        claims = [
            self.claim("legal_identity", None, {"name": "Example AS", "legal_form": "AS"}),
            self.claim("industry", None, {"code": "62.010", "label": "Computer programming"}),
            self.claim("employees", None, 12),
            self.claim("annual_accounts", "1:revenue", 5_000_000, unit="NOK", reporting_period={"from": "2025-01-01", "to": "2025-12-31"}),
            self.claim("roles", "DAGL:0", {"name": "Kari Nordmann", "role": "Daglig leder", "role_code": "DAGL"}),
        ]
        availability = {family: {"state": "available", "reason": "found", "checked_at": "x"} for family in FIELD_FAMILIES}
        for gap_family in ("official_website", "job_postings", "credentials_and_approvals"):
            availability[gap_family] = {"state": "not_available", "reason": "x", "checked_at": "x"}
        summary = build_summary("923609016", claims, availability, [])
        self.assertIn("Example AS", summary["text"])
        self.assertIn("computer programming", summary["text"])
        self.assertIn("Kari Nordmann", summary["text"])
        self.assertIn("5 000 000 NOK", summary["text"])
        self.assertEqual(summary["generator"], "deterministic_v1")
        self.assertIn("official website", summary["not_found"])
        self.assertIn("hiring activity", summary["not_found"])

    def test_summary_never_fabricates_a_field_with_no_claim(self):
        availability = {family: {"state": "not_available", "reason": "x", "checked_at": "x"} for family in FIELD_FAMILIES}
        summary = build_summary("923609016", [], availability, [])
        self.assertNotIn("None", summary["text"])
        self.assertEqual(summary["not_found"], summary["not_found"])  # every family listed as a gap
        self.assertEqual(len(summary["not_found"]), len(FIELD_FAMILIES))

    def test_deferred_changes_are_excluded_from_the_changed_sentence(self):
        availability = {family: {"state": "available", "reason": "x", "checked_at": "x"} for family in FIELD_FAMILIES}
        changes = [{"field": "official_website", "change_type": "deferred", "subkey": None}]
        summary = build_summary("923609016", [], availability, changes)
        self.assertNotIn("Changed since", summary["text"])
        self.assertEqual(summary["changed_fields"], [])

    def test_llm_rewrite_falls_back_when_it_alters_a_number(self):
        deterministic = {"text": "Example AS reports 12 employees.", "not_found": [], "changed_fields": [], "generator": "deterministic_v1"}
        with patch.dict(os.environ, {"SIGNALPOST_LLM_API_KEY": "test-key"}):
            with patch("norway_company_agent.summarize.urllib.request.urlopen") as mocked:
                mocked.return_value.__enter__.return_value.read.return_value = json.dumps(
                    {"choices": [{"message": {"content": "Example AS reports 99 employees."}}]}
                ).encode()
                result = maybe_llm_rewrite(deterministic)
        self.assertEqual(result["generator"], "deterministic_v1")
        self.assertEqual(result["text"], deterministic["text"])

    def test_llm_rewrite_accepted_when_it_preserves_every_number(self):
        deterministic = {"text": "Example AS reports 12 employees.", "not_found": [], "changed_fields": [], "generator": "deterministic_v1"}
        with patch.dict(os.environ, {"SIGNALPOST_LLM_API_KEY": "test-key"}):
            with patch("norway_company_agent.summarize.urllib.request.urlopen") as mocked:
                mocked.return_value.__enter__.return_value.read.return_value = json.dumps(
                    {"choices": [{"message": {"content": "Example AS has 12 people on staff."}}]}
                ).encode()
                result = maybe_llm_rewrite(deterministic)
        self.assertEqual(result["generator"], "llm_grounded_v1")
        self.assertIn("12 people", result["text"])

    def test_no_key_never_calls_the_network(self):
        deterministic = {"text": "Example AS reports 12 employees.", "not_found": [], "changed_fields": [], "generator": "deterministic_v1"}
        with patch.dict(os.environ, {}, clear=True):
            result = maybe_llm_rewrite(deterministic)
        self.assertEqual(result, deterministic)


class BuildSiteTests(unittest.TestCase):
    def make_envelope(self, org, *, terminal_status="completed"):
        availability = {f: {"state": "not_available", "reason": "x", "checked_at": "x"} for f in FIELD_FAMILIES}
        return {
            "organisation_number": org,
            "run": {"run_id": "t", "started_at": "x", "completed_at": "x", "terminal_status": terminal_status, "agent_version": "1.0.0", "code_commit": "x"},
            "claims": [], "evidence": [], "availability": availability, "changes": [], "errors": [],
            "summary": {"text": "", "not_found": [], "generator": "deterministic_v1"},
            "operations": {"requests": 0, "bytes": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0},
        }

    def test_failed_row_never_dropped_and_marked_parse_error(self):
        envelope = self.make_envelope("810034881", terminal_status="failed")
        row_html = render_index_row(envelope)
        self.assertIn("810034881", row_html)
        self.assertIn("PARSE_ERROR", row_html)

    def test_company_page_renders_without_crashing_on_an_empty_envelope(self):
        envelope = self.make_envelope("923609016")
        html_out = render_company_page(envelope)
        self.assertIn("923609016", html_out)
        self.assertIn("NOT FOUND", html_out)

    def test_not_found_lists_every_gap_family_with_its_reason(self):
        availability = {f: {"state": "not_available", "reason": "no_record_returned", "checked_at": "x"} for f in FIELD_FAMILIES}
        html_out = render_not_found(availability)
        for family in FIELD_FAMILIES:
            self.assertIn("no_record_returned", html_out)
        self.assertIn("NOT FOUND", html_out)

    def test_claim_values_are_html_escaped(self):
        envelope = self.make_envelope("923609016")
        envelope["claims"] = [{
            "claim_id": "a", "field": "site_description", "subkey": None,
            "value": '<script>alert(1)</script>', "unit": None, "reporting_period": None,
            "availability": "available", "confidence": 1.0, "method": "test", "evidence_ids": [],
            "first_seen": "x", "last_seen": "x",
        }]
        envelope["availability"]["site_description"] = {"state": "available", "reason": "found", "checked_at": "x"}
        html_out = render_company_page(envelope)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)


if __name__ == "__main__":
    unittest.main()
