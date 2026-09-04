
import os
import re
import json
import time
import shutil
import hashlib
import logging
import difflib
from typing import List, Dict, Optional, Tuple
from firecrawl import FirecrawlApp
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from datetime import datetime
from mcp.server.fastmcp import FastMCP
from bs4 import BeautifulSoup

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SCRAPE_DIR = "scraped_content"

# ---------------------------------------------------------------------------
# Standout-feature configuration
# ---------------------------------------------------------------------------
MIN_REQUEST_INTERVAL_SECONDS = 2.0   # per-provider (per-domain) rate limit
MAX_SCRAPE_RETRIES = 3               # retry/backoff attempts
BACKOFF_BASE_SECONDS = 2.0           # exponential backoff base
CACHE_TTL_HOURS = 24                 # caching layer: consider data stale after this long

# Guardrail against "prompt is too long" errors from the Anthropic API.
# Raw HTML (and even markdown) from a real pricing page can run into the
# hundreds of thousands of characters once you include nav bars, footers,
# inline styles, etc. We (a) strip HTML down to visible text instead of
# storing markup, and (b) hard-cap how much text we ever persist or return
# for a single provider/format so a single tool result can never blow the
# model's context window. This is configurable via an environment variable
# so it can be tuned without editing code.
MAX_CONTENT_CHARS = int(os.getenv("SCRAPE_MAX_CONTENT_CHARS", "50000"))

# Tracks the last time we hit a given domain, used for per-provider rate limiting.
_last_request_time: Dict[str, float] = {}

mcp = FastMCP("llm_inference")


# ---------------------------------------------------------------------------
# Helpers (standout features: retry/backoff, robots-aware crawling,
# per-provider rate limiting, dedup/caching)
# ---------------------------------------------------------------------------

def _check_robots_allowed(url: str) -> bool:
    """Standout #1: robots-aware crawl policy.

    Returns True if the given URL is allowed to be fetched by a generic
    user-agent according to the site's robots.txt. Fails "open" (allows the
    fetch) if robots.txt cannot be retrieved/parsed, since that's the most
    common and safest behavior for public marketing/pricing pages, but logs
    a warning so the decision is visible.
    """
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        allowed = rp.can_fetch("*", url)
        if not allowed:
            logger.warning(f"robots.txt disallows fetching {url}")
        return allowed
    except Exception as e:
        logger.warning(f"Could not read robots.txt for {url} ({e}); proceeding.")
        return True


def _respect_rate_limit(domain: str) -> None:
    """Standout #1: per-provider rate limiting.

    Ensures we wait at least MIN_REQUEST_INTERVAL_SECONDS between requests to
    the same domain, so we don't hammer any single provider's site.
    """
    now = time.time()
    last = _last_request_time.get(domain, 0.0)
    elapsed = now - last
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        wait = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        logger.info(f"Rate limiting {domain}: waiting {wait:.1f}s")
        time.sleep(wait)
    _last_request_time[domain] = time.time()


def _scrape_with_retry(app: FirecrawlApp, url: str, formats: List[str],
                        max_retries: int = MAX_SCRAPE_RETRIES) -> dict:
    """Standout #1: retry/backoff around the Firecrawl call."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return app.scrape(url, formats=formats).model_dump()
        except Exception as e:
            last_exc = e
            wait = BACKOFF_BASE_SECONDS ** attempt
            if attempt < max_retries:
                logger.warning(
                    f"Scrape attempt {attempt}/{max_retries} for {url} failed: {e}. "
                    f"Retrying in {wait:.1f}s"
                )
                time.sleep(wait)
            else:
                logger.warning(
                    f"Scrape attempt {attempt}/{max_retries} for {url} failed: {e}. Giving up."
                )
    raise last_exc


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _is_cache_fresh(metadata_entry: dict, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    """Standout #2: caching layer freshness check."""
    try:
        scraped_at = datetime.fromisoformat(metadata_entry["scraped_at"])
        age_hours = (datetime.now() - scraped_at).total_seconds() / 3600
        return age_hours < ttl_hours
    except Exception:
        return False


def _resolve_provider(identifier: str, scraped_metadata: dict) -> Optional[str]:
    """Match an identifier against provider name, URL, or domain."""
    for provider_name, metadata in scraped_metadata.items():
        if identifier in (provider_name, metadata.get('url', ''), metadata.get('domain', '')):
            return provider_name
    return None


def _load_metadata(metadata_file: str) -> dict:
    try:
        with open(metadata_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _extract_relevant_text(content: str, format_type: str) -> str:
    """Reduce a scraped payload to just the relevant raw text.

    For HTML, this strips tags/attributes/scripts/styles and keeps only the
    visible text (which is what actually matters for pricing info, and is a
    fraction of the size of the raw markup). Markdown and other formats are
    passed through as-is (already close to raw text) but still whitespace-
    normalized.
    """
    if not content:
        return ""

    if format_type == "html":
        try:
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
        except Exception as e:
            logger.warning(f"Failed to parse HTML content, falling back to raw text: {e}")
            text = content
    else:
        text = content

    # Collapse runs of whitespace so we're not paying "context" for
    # formatting noise.
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _truncate_content(text: str, max_chars: int = MAX_CONTENT_CHARS) -> Tuple[str, bool, int]:
    """Limit a string's length programmatically before it is stored or sent
    to the model, so a single tool result can never overflow the model's
    context window.

    Returns (possibly-truncated text, whether it was truncated, original length).
    """
    original_length = len(text)
    if original_length <= max_chars:
        return text, False, original_length

    # Only send the first `max_chars` characters, and note that it was cut.
    truncated = text[:max_chars]
    truncated += (
        f"\n\n[...content truncated: showing {max_chars:,} of "
        f"{original_length:,} characters...]"
    )
    return truncated, True, original_length


# ---------------------------------------------------------------------------
# Tool 1: scrape_websites
# ---------------------------------------------------------------------------

@mcp.tool()
def scrape_websites(
    websites: Dict[str, str],
    formats: List[str] = ['markdown', 'html'],
    api_key: Optional[str] = None,
    force: bool = False
) -> List[str]:
    """
    Scrape multiple websites using Firecrawl and store their content.

    Includes several production-friendly behaviors:
      - Retry with exponential backoff on transient Firecrawl errors.
      - robots.txt-aware crawling (skips URLs disallowed for scraping).
      - Per-provider (per-domain) rate limiting between requests.
      - Dedup of duplicate URLs within a single call.
      - A caching layer: if a provider was scraped within CACHE_TTL_HOURS,
        the cached copy is reused instead of re-scraping (pass force=True
        to bypass the cache and force a fresh scrape, equivalent to a
        --force flag).
      - Change tracking: stores a content hash and keeps one prior snapshot
        of each file so the check_pricing_changes tool can diff pricing
        changes between scrapes.

    Args:
        websites: Dictionary of provider_name -> URL mappings
        formats: List of formats to scrape ['markdown', 'html'] (default: both)
        api_key: Firecrawl API key (if None, expects environment variable)
        force: If True, bypass the cache and re-scrape even if fresh data exists

    Returns:
        List of provider names for successfully scraped (or freshly-cached) websites
    """

    if api_key is None:
        api_key = os.getenv('FIRECRAWL_API_KEY')
        if not api_key:
            raise ValueError("API key must be provided or set as FIRECRAWL_API_KEY environment variable")

    app = FirecrawlApp(api_key=api_key)

    path = os.path.join(SCRAPE_DIR)
    os.makedirs(path, exist_ok=True)

    metadata_file = os.path.join(path, "scraped_metadata.json")

    # 1. Load existing metadata (or start fresh)
    scraped_metadata = _load_metadata(metadata_file)

    # 2. Track successes
    successful_scrapes: List[str] = []
    seen_urls = set()

    # 3. Loop through websites
    for provider_name, url in websites.items():

        # Dedup within this call
        if url in seen_urls:
            logger.info(f"Skipping duplicate URL for {provider_name}: {url}")
            continue
        seen_urls.add(url)

        domain = urlparse(url).netloc
        existing = scraped_metadata.get(provider_name)

        # Caching layer: reuse fresh data unless force=True
        if not force and existing and existing.get("success") == "true" and _is_cache_fresh(existing):
            logger.info(f"Cache hit for {provider_name} (fresh within {CACHE_TTL_HOURS}h); skipping re-scrape.")
            successful_scrapes.append(provider_name)
            continue

        metadata = {
            "provider_name": provider_name,
            "url": url,
            "domain": domain,
            "scraped_at": datetime.now().isoformat(),
        }

        try:
            logger.info(f"Scraping {provider_name}: {url}")

            # robots-aware crawl policy
            if not _check_robots_allowed(url):
                metadata["success"] = "false"
                metadata["error"] = "Disallowed by robots.txt"
                scraped_metadata[provider_name] = metadata
                continue

            # per-provider rate limiting
            _respect_rate_limit(domain)

            # retry/backoff scrape call
            scrape_result = _scrape_with_retry(app, url, formats)

            metadata["formats"] = formats

            # NOTE: firecrawl-py's API has changed across versions. Older
            # releases returned a plain dict with an explicit "success" /
            # "error" field. The currently installed firecrawl-py (v2 API,
            # `FirecrawlApp().scrape(...)`) instead returns a `Document` on
            # success (no "success"/"error" keys at all) and *raises* an
            # exception on failure - it never returns success=False. Treat
            # both shapes as valid so this works regardless of which
            # firecrawl-py version ends up installed:
            if 'success' in scrape_result:
                scrape_succeeded = bool(scrape_result.get('success'))
            else:
                # We got a result object back without an exception being
                # raised, so the scrape succeeded.
                scrape_succeeded = True

            if scrape_succeeded:
                content_files = {}
                content_lengths = {}
                truncated_formats = []
                combined_for_hash = ""

                for format_type in formats:
                    raw_content = scrape_result.get(format_type, "") or ""

                    # 1. Extract only the relevant raw text (strip HTML markup
                    #    down to visible text; normalize whitespace).
                    cleaned_content = _extract_relevant_text(raw_content, format_type)

                    # 2. Limit the string length programmatically so nothing
                    #    we persist can later blow the model's context window.
                    content, was_truncated, original_length = _truncate_content(
                        cleaned_content, MAX_CONTENT_CHARS
                    )
                    if was_truncated:
                        truncated_formats.append(format_type)
                        logger.warning(
                            f"{provider_name} ({format_type}): content truncated from "
                            f"{original_length:,} to {MAX_CONTENT_CHARS:,} characters "
                            f"to stay within the model's context window."
                        )

                    combined_for_hash += content
                    content_lengths[format_type] = len(content)

                    filename = f"{provider_name}_{format_type}.txt"
                    filepath = os.path.join(path, filename)

                    # Keep one prior snapshot before overwriting, so we can
                    # diff pricing changes later (see check_pricing_changes).
                    if os.path.exists(filepath):
                        prior_path = os.path.join(path, f"{provider_name}_{format_type}_prior.txt")
                        shutil.copyfile(filepath, prior_path)

                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(content)
                    content_files[format_type] = filename

                new_hash = _content_hash(combined_for_hash)
                previous_hash = existing.get("content_hash") if existing else None
                content_changed = previous_hash is not None and previous_hash != new_hash

                page_meta = scrape_result.get("metadata", {}) or {}

                metadata["success"] = "true"
                metadata["content_files"] = content_files
                metadata["content_lengths"] = content_lengths
                metadata["truncated"] = bool(truncated_formats)
                metadata["truncated_formats"] = truncated_formats
                metadata["title"] = page_meta.get("title", scrape_result.get("title", "") or "")
                metadata["description"] = page_meta.get("description", scrape_result.get("description", "") or "")
                metadata["content_hash"] = new_hash
                metadata["content_changed"] = content_changed
                if content_changed:
                    metadata["previous_scraped_at"] = existing.get("scraped_at")
                    metadata["previous_content_hash"] = previous_hash
                    logger.info(f"Pricing content for {provider_name} changed since last scrape.")

                successful_scrapes.append(provider_name)
            else:
                metadata["success"] = "false"
                metadata["error"] = scrape_result.get("error", "Unknown error")
                logger.error(f"Failed to scrape {provider_name}: {metadata['error']}")

        except Exception as e:
            logger.error(f"Exception while scraping {provider_name}: {e}")
            metadata["success"] = "false"
            metadata["error"] = str(e)
        finally:
            scraped_metadata[provider_name] = metadata

    # 5. Persist metadata and log results
    with open(metadata_file, "w") as f:
        json.dump(scraped_metadata, f, indent=2)

    logger.info(f"Successfully scraped {len(successful_scrapes)} out of {len(websites)} websites")
    return successful_scrapes


# ---------------------------------------------------------------------------
# Tool 2: extract_scraped_info
# ---------------------------------------------------------------------------

@mcp.tool()
def extract_scraped_info(identifier: str) -> str:
    """
    Extract information about a scraped website.

    Args:
        identifier: The provider name, full URL, or domain to look for

    Returns:
        Formatted JSON string with the scraped information
    """

    logger.info(f"Extracting information for identifier: {identifier}")

    if os.path.isdir(SCRAPE_DIR):
        logger.info(f"Files in {SCRAPE_DIR}: {os.listdir(SCRAPE_DIR)}")

    metadata_file = os.path.join(SCRAPE_DIR, "scraped_metadata.json")
    logger.info(f"Checking metadata file: {metadata_file}")

    try:
        with open(metadata_file, "r") as f:
            scraped_metadata = json.load(f)

        for provider_name, metadata in scraped_metadata.items():
            if identifier in (provider_name, metadata.get('url', ''), metadata.get('domain', '')):
                result = metadata.copy()

                if 'content_files' in metadata:
                    result['content'] = {}
                    for format_type, filename in metadata['content_files'].items():
                        filepath = os.path.join(SCRAPE_DIR, filename)
                        try:
                            with open(filepath, "r", encoding="utf-8") as cf:
                                file_content = cf.read()
                        except FileNotFoundError:
                            file_content = ""

                        # Defensive re-truncation: even if a file on disk
                        # predates this guardrail (or MAX_CONTENT_CHARS was
                        # lowered since), never return more than
                        # MAX_CONTENT_CHARS characters for a single format.
                        # This is what actually protects the Anthropic call
                        # in starter_client.py from a
                        # "prompt is too long" (400) error.
                        safe_content, was_truncated, original_length = _truncate_content(
                            file_content, MAX_CONTENT_CHARS
                        )
                        result['content'][format_type] = safe_content
                        if was_truncated:
                            result.setdefault('content_truncated', {})[format_type] = {
                                "original_length": original_length,
                                "returned_length": len(safe_content),
                            }

                return json.dumps(result, indent=2)

        return f"There's no saved information related to identifier '{identifier}'."

    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error loading metadata: {e}")
        return f"There's no saved information related to identifier '{identifier}'."


# ---------------------------------------------------------------------------
# Tool 3 (Standout #3): check_pricing_changes
# ---------------------------------------------------------------------------

@mcp.tool()
def check_pricing_changes(identifier: Optional[str] = None) -> str:
    """
    Standout feature: "pricing change alert" tool.

    Diffs the current scrape against the prior scrape (saved automatically by
    scrape_websites) for one provider or all providers, and summarizes any
    lines that look like pricing changes (dollar amounts, per-token rates,
    etc).

    Args:
        identifier: provider name, URL, or domain to check. If omitted,
            checks every provider that has been scraped at least twice.

    Returns:
        A formatted JSON string summarizing detected pricing changes, or a
        plain-text message if there is nothing to compare yet.
    """
    metadata_file = os.path.join(SCRAPE_DIR, "scraped_metadata.json")
    scraped_metadata = _load_metadata(metadata_file)

    if not scraped_metadata:
        return "No scraped data available yet. Run scrape_websites first."

    if identifier:
        provider_name = _resolve_provider(identifier, scraped_metadata)
        if not provider_name:
            return f"There's no saved information related to identifier '{identifier}'."
        provider_names = [provider_name]
    else:
        provider_names = list(scraped_metadata.keys())

    price_pattern = re.compile(r'[$€£]\s?\d|\d+\.\d{2}|per\s+1?[MK]?\s*tokens?', re.IGNORECASE)
    report = {}

    for provider_name in provider_names:
        metadata = scraped_metadata.get(provider_name, {})
        if metadata.get("success") != "true":
            continue

        provider_changes = {
            "scraped_at": metadata.get("scraped_at"),
            "previous_scraped_at": metadata.get("previous_scraped_at"),
            "content_changed": metadata.get("content_changed", False),
            "changes": []
        }

        for format_type, filename in metadata.get("content_files", {}).items():
            current_path = os.path.join(SCRAPE_DIR, filename)
            prior_path = os.path.join(SCRAPE_DIR, f"{provider_name}_{format_type}_prior.txt")

            if not os.path.exists(prior_path) or not os.path.exists(current_path):
                continue

            with open(current_path, "r", encoding="utf-8") as f:
                current_lines = f.readlines()
            with open(prior_path, "r", encoding="utf-8") as f:
                prior_lines = f.readlines()

            diff_lines = difflib.unified_diff(prior_lines, current_lines, lineterm="")
            for line in diff_lines:
                if line.startswith(("+++", "---")):
                    continue
                if line.startswith(("+", "-")) and price_pattern.search(line):
                    provider_changes["changes"].append({
                        "format": format_type,
                        "change": "added" if line.startswith("+") else "removed",
                        "line": line[1:].strip()[:200]
                    })

        if provider_changes["changes"] or provider_changes["content_changed"]:
            report[provider_name] = provider_changes

    if not report:
        return json.dumps(
            {"message": "No pricing changes detected.", "checked_providers": provider_names},
            indent=2
        )

    return json.dumps(report, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
