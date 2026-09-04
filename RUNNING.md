# Running PriceScout End-to-End

## 0. Prerequisites
- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Node.js (for `npx`, used by the filesystem MCP server) — https://nodejs.org
- An Anthropic API key and a Firecrawl API key (free at https://www.firecrawl.dev/signin)

## 1. Set up the environment
```bash
cd project-Starter
uv venv
source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
uv sync
```

## 2. Add your API keys
```bash
cp .env.example .env
```
Edit `.env` and fill in your real keys:
```
ANTHROPIC_API_KEY=sk-ant-...
FIRECRAWL_API_KEY=fc-...
```

## 3. Confirm server_config.json
`server_config.json` already points `llm_inference` at `starter_server.py`:
```json
"llm_inference": { "command": "uv", "args": ["run", "starter_server.py"] }
```
No changes needed unless you renamed the server file.

## 4. (Optional but recommended) Run the automated tests
These use a mocked Firecrawl client, so no API key or network access is
needed:
```bash
uv run pytest test_server.py test_client.py -v
```
All tests should pass — this exercises both required tools, the standout
retry/robots/cache/diff logic, the SQL-escaping fix, and a schema/smoke
test for the SQLite table the client writes to.

## 5. Run the client
```bash
python starter_client.py
```
You should see it connect to 3 servers: `llm_inference` (your custom
scraper), `sqlite`, and `filesystem`, and list the available tools —
including the standout `check_pricing_changes` tool.

## 6. Try the test prompts
At the `Query:` prompt:

**Scrape the sites (Screenshot 1 for evidence):**
```
scrape these sites: {'cloudrift': 'https://www.cloudrift.ai/inference', 'deepinfra': 'https://deepinfra.com/pricing', 'fireworks': 'https://fireworks.ai/pricing#serverless-pricing', 'groq': 'https://groq.com/pricing'}
```
Look for `Successfully scraped 4 out of 4 websites` in the logs.

**Ask about pricing:**
```
How much does cloudrift ai (https://www.cloudrift.ai/inference) charge for deepseek v3?
How much does deepinfra (https://deepinfra.com/pricing) charge for deepseek v3
Try searching for more specific CloudRift AI pricing documentation or check if they have other pricing pages. The price qoutes are needed to compare cloudrift ai and deepinfra's costs for deepseek v3.
```

**Compare providers (Screenshot 2 for evidence):**
```
Compare cloudrift ai and deepinfra's costs for deepseek v3
```
Tip from the project instructions: run this *after* the individual queries
above so the comparison can reuse already-scraped/stored data instead of
re-scraping (saves Firecrawl/Anthropic credits).

**Check the database (Screenshot 3 for evidence):**
```
show data
```

**Try the standout pricing-change alert (re-scrape with force, then diff):**
```
Re-scrape these sites and force a fresh scrape: {'groq': 'https://groq.com/pricing'}
Has groq's pricing changed since the last scrape?
```
Claude will call `scrape_websites` with `force: true` and then
`check_pricing_changes` to summarize any price-line differences.

Exit any time with:
```
quit
```