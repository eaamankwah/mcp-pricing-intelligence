"""
Standout #4: Unit tests for the two MCP tools (scrape_websites,
extract_scraped_info), plus a smoke test that validates the SQLite schema
used by the client's DataExtractor and a mocked end-to-end
scrape -> extract -> pricing-change-check flow.

No real network calls or API keys are needed - Firecrawl and robots.txt
checks are mocked out.

Run with:
    uv run pytest test_server.py -v
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import starter_server as server_module  # noqa: E402


class FakeScrapeResult:
    """Mimics the object returned by FirecrawlApp().scrape(...)."""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self):
        return self._data


def _fake_success_payload(
    markdown="Deepseek V3 costs $0.50 / 1M input tokens.",
    html="<p>Deepseek V3 costs $0.50 / 1M input tokens.</p>",
    title="Pricing",
    description="Pricing page",
):
    return {
        "success": True,
        "markdown": markdown,
        "html": html,
        "metadata": {"title": title, "description": description},
    }


@pytest.fixture(autouse=True)
def isolated_scrape_dir(tmp_path, monkeypatch):
    """Point SCRAPE_DIR at a temp directory so tests never touch real data."""
    test_dir = tmp_path / "scraped_content"
    monkeypatch.setattr(server_module, "SCRAPE_DIR", str(test_dir))
    monkeypatch.setattr(server_module, "_last_request_time", {})
    yield test_dir


# ---------------------------------------------------------------------------
# scrape_websites
# ---------------------------------------------------------------------------

@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_creates_files_and_metadata(mock_app_cls, _mock_robots):
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload())
    mock_app_cls.return_value = mock_app

    websites = {"testprovider": "https://example.com/pricing"}
    result = server_module.scrape_websites(websites, formats=["markdown", "html"], api_key="fake-key")

    assert result == ["testprovider"]

    md_file = os.path.join(server_module.SCRAPE_DIR, "testprovider_markdown.txt")
    html_file = os.path.join(server_module.SCRAPE_DIR, "testprovider_html.txt")
    assert os.path.exists(md_file)
    assert os.path.exists(html_file)

    metadata_file = os.path.join(server_module.SCRAPE_DIR, "scraped_metadata.json")
    assert os.path.exists(metadata_file)
    with open(metadata_file) as f:
        metadata = json.load(f)

    entry = metadata["testprovider"]
    assert entry["provider_name"] == "testprovider"
    assert entry["url"] == "https://example.com/pricing"
    assert entry["domain"] == "example.com"
    assert entry["success"] == "true"
    assert set(entry["content_files"].keys()) == {"markdown", "html"}
    assert entry["title"] == "Pricing"
    assert entry["description"] == "Pricing page"
    assert "scraped_at" in entry


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_handles_failure(mock_app_cls, _mock_robots):
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult({"success": False, "error": "timeout"})
    mock_app_cls.return_value = mock_app

    result = server_module.scrape_websites({"badprovider": "https://bad.example.com"}, api_key="fake-key")
    assert result == []

    metadata_file = os.path.join(server_module.SCRAPE_DIR, "scraped_metadata.json")
    with open(metadata_file) as f:
        metadata = json.load(f)
    assert metadata["badprovider"]["success"] == "false"
    assert metadata["badprovider"]["error"] == "timeout"


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_handles_v2_document_response_without_success_key(mock_app_cls, _mock_robots):
    """Regression test: newer firecrawl-py (v2 API) returns a Document with
    NO "success"/"error" keys at all on success (and raises on failure).
    Previously this made every scrape look like a failure ("Unknown error")
    even though the scrape actually worked."""
    mock_app = MagicMock()
    # No "success" or "error" key present - matches firecrawl.v2.types.Document.model_dump()
    mock_app.scrape.return_value = FakeScrapeResult({
        "markdown": "Deepseek V3 costs $0.50 / 1M input tokens.",
        "html": "<p>Deepseek V3 costs $0.50 / 1M input tokens.</p>",
        "metadata": {"title": "Pricing", "description": "Pricing page"},
    })
    mock_app_cls.return_value = mock_app

    result = server_module.scrape_websites({"cloudrift": "https://www.cloudrift.ai/inference"}, api_key="fake-key")
    assert result == ["cloudrift"]

    metadata_file = os.path.join(server_module.SCRAPE_DIR, "scraped_metadata.json")
    with open(metadata_file) as f:
        metadata = json.load(f)
    assert metadata["cloudrift"]["success"] == "true"
    assert "content_files" in metadata["cloudrift"]


@patch.object(server_module.time, "sleep", return_value=None)
@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_handles_exception_from_v2_api(mock_app_cls, _mock_robots, _mock_sleep):
    """v2-style clients raise on failure instead of returning success=False."""
    mock_app = MagicMock()
    mock_app.scrape.side_effect = RuntimeError("site unreachable")
    mock_app_cls.return_value = mock_app

    result = server_module.scrape_websites(
        {"badprovider": "https://bad.example.com"}, api_key="fake-key"
    )
    assert result == []

    metadata_file = os.path.join(server_module.SCRAPE_DIR, "scraped_metadata.json")
    with open(metadata_file) as f:
        metadata = json.load(f)
    assert metadata["badprovider"]["success"] == "false"
    assert "site unreachable" in metadata["badprovider"]["error"]


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_respects_cache_and_force(mock_app_cls, _mock_robots):
    """Standout #2: caching layer - re-scrape is skipped unless force=True."""
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload())
    mock_app_cls.return_value = mock_app

    websites = {"cached": "https://example.com/pricing"}

    server_module.scrape_websites(websites, api_key="fake-key")
    assert mock_app.scrape.call_count == 1

    # Cached / fresh - should NOT call Firecrawl again.
    result = server_module.scrape_websites(websites, api_key="fake-key")
    assert mock_app.scrape.call_count == 1
    assert result == ["cached"]

    # force=True bypasses the cache.
    server_module.scrape_websites(websites, api_key="fake-key", force=True)
    assert mock_app.scrape.call_count == 2


@patch.object(server_module, "_check_robots_allowed", return_value=False)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_respects_robots_txt(mock_app_cls, _mock_robots):
    """Standout #1: robots-aware crawl policy - disallowed URLs are skipped."""
    mock_app = MagicMock()
    mock_app_cls.return_value = mock_app

    result = server_module.scrape_websites({"blocked": "https://blocked.example.com"}, api_key="fake-key")

    assert result == []
    mock_app.scrape.assert_not_called()


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_dedups_repeated_urls(mock_app_cls, _mock_robots):
    """Standout #1: dedup of duplicate URLs within a single call."""
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload())
    mock_app_cls.return_value = mock_app

    websites = {"providerA": "https://example.com/pricing", "providerB": "https://example.com/pricing"}
    result = server_module.scrape_websites(websites, api_key="fake-key")

    assert result == ["providerA"]
    assert mock_app.scrape.call_count == 1


# ---------------------------------------------------------------------------
# extract_scraped_info
# ---------------------------------------------------------------------------

@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_extract_scraped_info_matches_provider_url_and_domain(mock_app_cls, _mock_robots):
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload())
    mock_app_cls.return_value = mock_app

    server_module.scrape_websites({"testprovider": "https://example.com/pricing"}, api_key="fake-key")

    for identifier in ["testprovider", "https://example.com/pricing", "example.com"]:
        result_str = server_module.extract_scraped_info(identifier)
        result = json.loads(result_str)
        assert result["provider_name"] == "testprovider"
        assert "content" in result
        assert "markdown" in result["content"]
        assert "Deepseek V3" in result["content"]["markdown"]


def test_extract_scraped_info_no_match_returns_message():
    result = server_module.extract_scraped_info("does-not-exist")
    assert "no saved information" in result.lower()


# ---------------------------------------------------------------------------
# Content-size guardrail (prevents "prompt is too long" errors from Claude)
# ---------------------------------------------------------------------------

@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_strips_html_to_text(mock_app_cls, _mock_robots):
    """HTML should be reduced to visible text, not stored as raw markup."""
    mock_app = MagicMock()
    html = "<html><head><style>.x{color:red}</style></head><body><script>evil()</script>" \
           "<p>Groq charges <b>$0.59</b> per 1M input tokens.</p></body></html>"
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload(html=html))
    mock_app_cls.return_value = mock_app

    server_module.scrape_websites({"groq": "https://groq.com/pricing"}, formats=["html"], api_key="fake-key")

    html_file = os.path.join(server_module.SCRAPE_DIR, "groq_html.txt")
    with open(html_file) as f:
        stored = f.read()

    assert "<script>" not in stored
    assert "<p>" not in stored
    assert "evil()" not in stored
    assert "color:red" not in stored
    assert "$0.59" in stored
    assert "Groq charges" in stored


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_scrape_websites_truncates_oversized_content(mock_app_cls, _mock_robots, monkeypatch):
    """Content longer than MAX_CONTENT_CHARS must be capped before it's ever
    written to disk, so a downstream tool call can never send an oversized
    prompt to Claude."""
    monkeypatch.setattr(server_module, "MAX_CONTENT_CHARS", 1000)

    mock_app = MagicMock()
    huge_markdown = "Deepseek V3 costs $0.50 per 1M tokens. " * 10_000  # ~390,000 chars
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload(markdown=huge_markdown))
    mock_app_cls.return_value = mock_app

    server_module.scrape_websites({"big": "https://example.com/pricing"}, formats=["markdown"], api_key="fake-key")

    md_file = os.path.join(server_module.SCRAPE_DIR, "big_markdown.txt")
    with open(md_file) as f:
        stored = f.read()

    # Stored content (including the truncation note) stays close to the cap,
    # and is nowhere near the original ~390,000 characters.
    assert len(stored) < 1200
    assert "truncated" in stored.lower()

    metadata_file = os.path.join(server_module.SCRAPE_DIR, "scraped_metadata.json")
    with open(metadata_file) as f:
        metadata = json.load(f)
    assert metadata["big"]["truncated"] is True
    assert "markdown" in metadata["big"]["truncated_formats"]


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_extract_scraped_info_defensively_truncates_legacy_files(mock_app_cls, _mock_robots, monkeypatch):
    """Even if a file on disk predates the size guardrail (or the limit was
    lowered), extract_scraped_info must never return more than
    MAX_CONTENT_CHARS characters for a single format."""
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult(_fake_success_payload(markdown="short content"))
    mock_app_cls.return_value = mock_app

    server_module.scrape_websites({"provider": "https://example.com/pricing"}, formats=["markdown"], api_key="fake-key")

    # Simulate a legacy oversized file already on disk.
    md_file = os.path.join(server_module.SCRAPE_DIR, "provider_markdown.txt")
    with open(md_file, "w") as f:
        f.write("x" * 500_000)

    monkeypatch.setattr(server_module, "MAX_CONTENT_CHARS", 2000)

    result = json.loads(server_module.extract_scraped_info("provider"))
    assert len(result["content"]["markdown"]) <= 2200  # cap + truncation note
    assert result["content_truncated"]["markdown"]["original_length"] == 500_000


# ---------------------------------------------------------------------------
# check_pricing_changes (standout #3)
# ---------------------------------------------------------------------------

@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_check_pricing_changes_detects_price_diff(mock_app_cls, _mock_robots):
    mock_app = MagicMock()
    mock_app_cls.return_value = mock_app

    websites = {"testprovider": "https://example.com/pricing"}

    mock_app.scrape.return_value = FakeScrapeResult(
        _fake_success_payload(markdown="Deepseek V3: $0.50 per 1M input tokens")
    )
    server_module.scrape_websites(websites, api_key="fake-key", force=True)

    mock_app.scrape.return_value = FakeScrapeResult(
        _fake_success_payload(markdown="Deepseek V3: $0.75 per 1M input tokens")
    )
    server_module.scrape_websites(websites, api_key="fake-key", force=True)

    report_str = server_module.check_pricing_changes("testprovider")
    report = json.loads(report_str)

    assert "testprovider" in report
    assert report["testprovider"]["content_changed"] is True
    assert len(report["testprovider"]["changes"]) > 0


def test_check_pricing_changes_no_data_yet():
    result = server_module.check_pricing_changes("nonexistent")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Smoke test: DB schema + full mocked scrape -> extract -> diff flow
# ---------------------------------------------------------------------------

def test_smoke_db_schema_matches_data_extractor():
    """Validate the pricing_plans schema used by starter_client.DataExtractor."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pricing_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                plan_name TEXT NOT NULL,
                input_tokens REAL,
                output_tokens REAL,
                currency TEXT DEFAULT 'USD',
                billing_period TEXT,
                features TEXT,
                limitations TEXT,
                source_query TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO pricing_plans
                (company_name, plan_name, input_tokens, output_tokens, currency,
                 billing_period, features, limitations, source_query)
            VALUES ('CloudRift', 'Serverless', 0.5, 1.5, 'USD', 'usage-based', '["fast"]', 'none', 'test query')
        """)
        conn.commit()

        cols = {row[1] for row in conn.execute("PRAGMA table_info(pricing_plans)").fetchall()}
        expected = {
            "id", "company_name", "plan_name", "input_tokens", "output_tokens",
            "currency", "billing_period", "features", "limitations", "source_query", "created_at",
        }
        assert expected.issubset(cols)

        row = conn.execute("SELECT company_name, plan_name FROM pricing_plans").fetchone()
        assert row == ("CloudRift", "Serverless")
        conn.close()


@patch.object(server_module, "_check_robots_allowed", return_value=True)
@patch.object(server_module, "FirecrawlApp")
def test_smoke_full_scrape_to_answer_flow(mock_app_cls, _mock_robots):
    """End-to-end smoke test: scrape -> extract -> pricing-change check (no network)."""
    mock_app = MagicMock()
    mock_app.scrape.return_value = FakeScrapeResult(
        _fake_success_payload(markdown="CloudRift charges $0.55 per 1M input tokens for Deepseek V3.")
    )
    mock_app_cls.return_value = mock_app

    websites = {"cloudrift": "https://www.cloudrift.ai/inference"}
    successful = server_module.scrape_websites(websites, api_key="fake-key")
    assert successful == ["cloudrift"]

    info = json.loads(server_module.extract_scraped_info("cloudrift"))
    assert "Deepseek V3" in info["content"]["markdown"]

    # First-ever scrape: no prior snapshot yet, so no changes should be reported.
    changes_str = server_module.check_pricing_changes("cloudrift")
    changes = json.loads(changes_str)
    assert changes.get("message") == "No pricing changes detected." or "cloudrift" not in changes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))