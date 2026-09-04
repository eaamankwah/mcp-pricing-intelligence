# Standout Features Implemented

## Bugfix: `show data` fails with "Expecting property name enclosed in double quotes"

**Where:** `starter_client.py` — new `_parse_sql_tool_result` helper, used in
`ChatSession.show_stored_data`.

The reference `mcp-server-sqlite` implementation returns query results via
plain `str(results)` on a `list[dict]` (confirmed by reading its source),
e.g.:
```
[{'company_name': 'CloudRift', 'plan_name': 'Serverless', 'input_tokens': 0.5, ...}]
```
That's a **Python repr** with single quotes — not JSON — so
`json.loads(pricing.content[0].text)` correctly fails with `Expecting
property name enclosed in double quotes`.

**Fix:** `_parse_sql_tool_result()` tries `json.loads` first (in case a
different/future sqlite server returns real JSON), and falls back to
`ast.literal_eval` (safe parsing of Python literal syntax, no code
execution) — which is exactly what the reference server emits. Covered by
`test_parse_sql_tool_result_handles_python_repr_from_mcp_server_sqlite`,
`_still_accepts_real_json`, and `_handles_empty_result` in `test_client.py`.



## Bugfix: scrapes always failing with "Unknown error"

**Where:** `starter_server.py` — `scrape_websites`.

The currently-installed `firecrawl-py` uses the v2 API
(`FirecrawlApp().scrape(...)`), which returns a `Document` object with
**no `success` or `error` field at all** on success — it raises an
exception instead if the scrape fails. The original code assumed the
older-style `{"success": bool, "error": str}` dict shape, so
`scrape_result.get('success', False)` was always `False` and every scrape
was logged as `Failed to scrape ...: Unknown error` even when the scrape
actually worked.

**Fix:** check whether a `"success"` key is even present in the result. If
it is, honor it (supports older firecrawl-py releases). If it isn't, treat
reaching that line without an exception as success (matches the current
v2 API). Covered by `test_scrape_websites_handles_v2_document_response_without_success_key`
and `test_scrape_websites_handles_exception_from_v2_api` in `test_server.py`.

## Bugfix: `Database error ... near "cloudrift": syntax error`

**Where:** `starter_client.py` — `DataExtractor.extract_and_store_data`.

The `INSERT` query was built with plain f-string interpolation, so any
value containing a single quote broke out of the SQL string literal. The
most common trigger is the chat query itself — e.g.
`scrape these sites: {'cloudrift': 'https://...', ...}` is full of literal
`'` characters — but plan names/features with an apostrophe (`"Developer's
Tier"`) hit the same bug.

**Fix:** added `_sql_escape()`, which doubles embedded single quotes
(the standard SQL escaping convention), and applied it to every string
field interpolated into the `INSERT` (`company_name`, `plan_name`,
`currency`, `billing_period`, `features` JSON, `limitations`,
`source_query`). The `mcp-server-sqlite` `write_query` tool only accepts a
raw SQL string (no parameter binding), so this is the minimal safe fix
without changing the tool/schema. Covered by `test_client.py`.



## Bugfix: "prompt is too long" (400) errors from Claude

**Where:** `starter_server.py` — `_extract_relevant_text`, `_truncate_content`,
and their use in `scrape_websites` / `extract_scraped_info`.

Sending a page's raw HTML (or even a large markdown dump) straight into a
Claude prompt can easily blow past the 200K-token context limit once you
factor in nav bars, inline styles, and repeated markup. Two changes fix
this:

1. **Extract only the relevant text before it's ever stored.** HTML content
   is parsed with BeautifulSoup, `<script>`/`<style>` tags are dropped, and
   only the visible text is kept. Whitespace is normalized. Markdown is
   passed through as-is (it's already close to raw text).
2. **Hard-cap the string length.** `_truncate_content` limits any single
   provider/format's text to `MAX_CONTENT_CHARS` (default 50,000 characters,
   configurable via the `SCRAPE_MAX_CONTENT_CHARS` env var) before it is
   written to disk in `scrape_websites`, **and again** defensively when it's
   read back in `extract_scraped_info` — so even a file saved before this
   fix (or a lower limit set later) can never be returned in full. When
   truncation happens, the metadata records `truncated: true` /
   `truncated_formats`, and the response includes a `content_truncated`
   note with the original vs. returned length, so it's visible rather than
   silent.

Covered by `test_scrape_websites_strips_html_to_text`,
`test_scrape_websites_truncates_oversized_content`, and
`test_extract_scraped_info_defensively_truncates_legacy_files` in
`test_server.py`.


This project implements all five "stand out" suggestions from the rubric.

## 1. Retry/backoff + robots-aware crawl policy with rate limits & dedup
**Where:** `starter_server.py` — `_scrape_with_retry`, `_check_robots_allowed`,
`_respect_rate_limit`, and the dedup check inside `scrape_websites`.

- `_scrape_with_retry` retries a failed Firecrawl call up to 3 times with
  exponential backoff (2s, 4s, 8s).
- `_check_robots_allowed` parses each site's `robots.txt` and skips the URL
  (marking it `success: "false"`) if scraping is disallowed for `*`.
- `_respect_rate_limit` enforces a minimum 2-second gap between requests to
  the same domain.
- `scrape_websites` skips duplicate URLs within a single call (dedup).

**Demo:** call `scrape_websites` twice quickly with the same site and watch
the log line `Rate limiting <domain>: waiting Xs`; pass a URL whose
`robots.txt` disallows `/` to see it get skipped.

## 2. Caching layer with staleness check + force option
**Where:** `starter_server.py` — `_is_cache_fresh` and the cache check at the
top of the per-website loop in `scrape_websites`.

- Every scrape stores `scraped_at`. On the next call, if a provider's data is
  less than `CACHE_TTL_HOURS` (24h) old, it's reused instead of re-scraping.
- Pass `force=True` to the `scrape_websites` tool to bypass the cache and
  force a fresh scrape (the tool-level equivalent of a `--force` CLI flag).

**Demo:** call `scrape_websites` for the same site twice in a row — the
second call logs `Cache hit for <provider>` and does not call Firecrawl.
Call again with `force: true` to force a re-scrape.

## 3. "Pricing change alert" tool
**Where:** `starter_server.py` — the new `check_pricing_changes` MCP tool.

- Every time `scrape_websites` overwrites a provider's content file, it first
  backs up the old version as `{provider}_{format}_prior.txt` and stores a
  SHA-256 content hash + `content_changed` flag in the metadata.
- `check_pricing_changes(identifier=None)` diffs the current file against
  the prior snapshot (via `difflib.unified_diff`) and returns only the added/
  removed lines that look like pricing (dollar signs, decimals, "per token").

**Demo:** scrape a site, edit its saved markdown file to change a price,
re-run `scrape_websites` with `force: true`, then call
`check_pricing_changes` for that provider — you'll see the before/after
price lines in the JSON response.

## 4. Unit tests + smoke test
**Where:** `test_server.py`.

- Unit tests for `scrape_websites` (file/metadata creation, failure handling,
  caching, robots.txt enforcement, dedup) and `extract_scraped_info`
  (matching by provider/URL/domain, no-match message), all with a mocked
  `FirecrawlApp` — no network or API key required.
- A smoke test that validates the `pricing_plans` SQLite schema used by
  `DataExtractor` in `starter_client.py`.
- A smoke test that runs a full mocked scrape → extract → pricing-change-check
  flow end-to-end.

**Run:** `uv run pytest test_server.py -v`

## 5. Streaming terminal UI for tool events
**Where:** `starter_client.py` — the `_emit` helper and its calls throughout
`ChatSession.process_query`.

Every stage of a query is printed live with a timestamp and icon:
`🧠 THINKING`, `🔧 TOOL_CALL`, `✅ TOOL_RESULT` / `❌ TOOL_ERROR`,
`🗄️ DB`, and `💬 ANSWER` — so you can see the scrape → parse → DB write →
answer pipeline as it happens instead of only seeing the final answer.
