"""
Regression tests for the SQL string-escaping fix in starter_client.py's
DataExtractor. Without escaping, a source_query (or company/plan name)
containing single quotes - e.g. the literal chat command
`scrape these sites: {'cloudrift': 'https://...', ...}` - breaks out of the
SQL string literal and causes `sqlite3.OperationalError: near "...":
syntax error`.

Run with:
    uv run pytest test_client.py -v
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from starter_client import _sql_escape, _parse_sql_tool_result  # noqa: E402


@pytest.fixture
def pricing_plans_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE pricing_plans (
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
    yield conn
    conn.close()


def _build_insert_query(pricing_data: dict, plan: dict, user_query: str) -> str:
    """Mirrors the query-building logic in DataExtractor.extract_and_store_data."""
    company_name = _sql_escape(pricing_data.get("company_name", "Unknown"))
    plan_name = _sql_escape(plan.get("plan_name", "Unknown Plan"))
    input_tokens = plan.get("input_tokens", 0)
    input_tokens = 0 if input_tokens is None else input_tokens
    output_tokens = plan.get("output_tokens", 0)
    output_tokens = 0 if output_tokens is None else output_tokens
    currency = _sql_escape(plan.get("currency", "USD"))
    billing_period = _sql_escape(plan.get("billing_period", "unknown"))
    features = _sql_escape(json.dumps(plan.get("features", [])))
    limitations = _sql_escape(plan.get("limitations", ""))
    source_query = _sql_escape(user_query)

    return f"""
    INSERT INTO pricing_plans (company_name, plan_name, input_tokens, output_tokens, currency, billing_period, features, limitations, source_query)
    VALUES (
    '{company_name}',
    '{plan_name}',
    '{input_tokens}',
    '{output_tokens}',
    '{currency}',
    '{billing_period}',
    '{features}',
    '{limitations}',
    '{source_query}')
    """


def test_sql_escape_doubles_single_quotes():
    assert _sql_escape("O'Reilly") == "O''Reilly"
    assert _sql_escape("no quotes here") == "no quotes here"
    assert _sql_escape(0.5) == "0.5"


def test_parse_sql_tool_result_handles_python_repr_from_mcp_server_sqlite():
    """Regression test: the reference mcp-server-sqlite implementation
    returns rows via str(results) - a Python list-of-dicts repr with single
    quotes - not json.dumps(). Feeding that straight to json.loads raises
    'Expecting property name enclosed in double quotes'. This is exactly
    the text the real server returns for `show data`."""
    real_server_text = (
        "[{'company_name': 'CloudRift', 'plan_name': 'Serverless', "
        "'input_tokens': 0.5, 'output_tokens': 1.5, 'currency': 'USD'}, "
        "{'company_name': 'Groq', 'plan_name': 'Standard', "
        "'input_tokens': 0.59, 'output_tokens': 0.79, 'currency': 'USD'}]"
    )

    rows = _parse_sql_tool_result(real_server_text)

    assert len(rows) == 2
    assert rows[0]["company_name"] == "CloudRift"
    assert rows[0]["input_tokens"] == 0.5
    assert rows[1]["company_name"] == "Groq"


def test_parse_sql_tool_result_still_accepts_real_json():
    """If a different sqlite MCP server ever returns proper JSON instead,
    parsing should still work (JSON is tried first)."""
    rows = _parse_sql_tool_result('[{"company_name": "Fireworks", "plan_name": "Base"}]')
    assert rows == [{"company_name": "Fireworks", "plan_name": "Base"}]


def test_parse_sql_tool_result_handles_empty_result():
    assert _parse_sql_tool_result("[]") == []
    assert _parse_sql_tool_result("") == []


def test_insert_survives_query_with_python_dict_syntax(pricing_plans_db):
    """Regression test for the exact real-world failure: a chat query that
    is a Python-dict-style string full of single quotes used to break the
    generated INSERT and raise 'syntax error near "cloudrift"'."""
    user_query = (
        "scrape these sites: {'cloudrift': 'https://www.cloudrift.ai/inference', "
        "'deepinfra': 'https://deepinfra.com/pricing'}"
    )
    pricing_data = {"company_name": "cloudrift"}
    plan = {
        "plan_name": "Unknown Plan",
        "input_tokens": 0,
        "output_tokens": 0,
        "currency": "USD",
        "billing_period": "unknown",
        "features": [],
        "limitations": "",
    }

    query = _build_insert_query(pricing_data, plan, user_query)
    pricing_plans_db.execute(query)  # must not raise

    row = pricing_plans_db.execute(
        "SELECT company_name, source_query FROM pricing_plans"
    ).fetchone()
    assert row[0] == "cloudrift"
    assert row[1] == user_query


def test_insert_survives_apostrophes_in_plan_and_features(pricing_plans_db):
    pricing_data = {"company_name": "DeepInfra"}
    plan = {
        "plan_name": "Developer's Tier",
        "input_tokens": 0.5,
        "output_tokens": 1.5,
        "currency": "USD",
        "billing_period": "usage-based",
        "features": ["O'Reilly-grade support", "24/7"],
        "limitations": "Doesn't include fine-tuning",
    }

    query = _build_insert_query(pricing_data, plan, "compare deepinfra's pricing")
    pricing_plans_db.execute(query)  # must not raise

    row = pricing_plans_db.execute(
        "SELECT plan_name, features, limitations FROM pricing_plans"
    ).fetchone()
    assert row[0] == "Developer's Tier"
    assert json.loads(row[1]) == ["O'Reilly-grade support", "24/7"]
    assert row[2] == "Doesn't include fine-tuning"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
