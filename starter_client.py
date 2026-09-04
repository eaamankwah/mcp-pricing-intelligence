import asyncio
import ast
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any, List, Dict, TypedDict
from datetime import datetime, timedelta
from pathlib import Path
import re

from dotenv import load_dotenv
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Model used for both the tool-selecting calls and the structured-extraction calls.
MODEL_NAME = "claude-sonnet-4-5-20250929"


class ToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict


class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration with environment variables."""
        self.load_env()
        self.api_key = os.getenv("ANTHROPIC_API_KEY")

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str | Path) -> dict[str, Any]:
        """Load server configuration from JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Dict containing server configuration.

        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
            ValueError: If configuration file is missing required fields.
        """
        try:
            with open(file_path, "r") as f:
                config = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file not found: {file_path}")
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(f"Invalid JSON in configuration file {file_path}: {e.msg}", e.doc, e.pos)

        if "mcpServers" not in config:
            raise ValueError("Configuration file must contain an 'mcpServers' key")

        return config

    @property
    def anthropic_api_key(self) -> str:
        """Get the Anthropic API key.

        Returns:
            The API key as a string.

        Raises:
            ValueError: If the API key is not found in environment variables.
        """
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment variables")
        return self.api_key


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config: dict[str, Any] = config
        self.stdio_context: Any | None = None
        self.session: ClientSession | None = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.exit_stack: AsyncExitStack = AsyncExitStack()

    async def initialize(self) -> None:
        """Initialize the server connection."""
        command = shutil.which("npx") if self.config["command"] == "npx" else self.config["command"]
        if command is None:
            raise ValueError("The command must be a valid string and cannot be None.")

        server_params = StdioServerParameters(
            command=command,
            args=self.config["args"],
            env={**os.environ, **self.config["env"]} if self.config.get("env") else None,
        )
        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = stdio_transport
            session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
            logging.info(f"✓ Server '{self.name}' initialized")
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> List[ToolDefinition]:
        """List available tools from the server.

        Returns:
            A list of available tool definitions.

        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server '{self.name}' is not initialized")

        tools_response = await self.session.list_tools()
        tools: List[ToolDefinition] = []

        for tool in tools_response.tools:
            tool_def: ToolDefinition = {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema
            }
            tools.append(tool_def)

        return tools

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """Execute a tool with retry mechanism.

        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.

        Returns:
            Tool execution result.

        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server '{self.name}' is not initialized")

        attempt = 0
        while True:
            attempt += 1
            try:
                logging.info(f"Executing {tool_name}...")
                result = await self.session.call_tool(
                    name=tool_name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=60)
                )
                return result
            except Exception as e:
                logging.warning(f"Error executing tool '{tool_name}' (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error(f"Max retries reached for tool '{tool_name}'. Giving up.")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
                self.session = None
                self.stdio_context = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")


def _sql_escape(value: Any) -> str:
    """Escape a value for safe interpolation inside a single-quoted SQL
    string literal (doubles any embedded single quotes, SQL-standard style).

    The sqlite MCP server's `write_query` tool only accepts a raw SQL
    string (no parameter binding), so values built via f-string
    interpolation - like the user's original chat query, which can contain
    apostrophes or, as here, literal single quotes from a Python-dict-style
    query - must be escaped or they'll break out of the string literal and
    cause a "syntax error near ..." from SQLite.
    """
    return str(value).replace("'", "''")


def _parse_sql_tool_result(text: str) -> list:
    """Parse the text returned by the sqlite MCP server's read_query /
    write_query tools into Python data.

    The reference `mcp-server-sqlite` implementation returns its rows via
    `str(results)` (a Python list-of-dicts *repr*, e.g.
    `[{'company_name': 'CloudRift', ...}]`) rather than `json.dumps(...)`.
    That string uses single quotes around keys/values, so `json.loads`
    correctly rejects it with an error like:
        "Expecting property name enclosed in double quotes"
    We try JSON first (in case a different/updated sqlite server returns
    proper JSON), and fall back to `ast.literal_eval`, which safely parses
    Python literal syntax (no code execution) - exactly what this server
    emits.
    """
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return ast.literal_eval(text)


class DataExtractor:
    """Handles extraction and storage of structured data from LLM responses."""
    
    def __init__(self, sqlite_server: Server, anthropic_client: Anthropic):
        self.sqlite_server = sqlite_server
        self.anthropic = anthropic_client
        
    async def setup_data_tables(self) -> None:
        """Setup tables for storing extracted data."""
        try:
            
            await self.sqlite_server.execute_tool("write_query", {
                "query": """
                CREATE TABLE IF NOT EXISTS pricing_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    input_tokens REAL,
                    output_tokens REAL,
                    currency TEXT DEFAULT 'USD',
                    billing_period TEXT,  -- 'monthly', 'yearly', 'one-time'
                    features TEXT,  -- JSON array
                    limitations TEXT,
                    source_query TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            })
            
            logging.info("✓ Data extraction tables initialized")
            
        except Exception as e:
            logging.error(f"Failed to setup data tables: {e}")

    async def _get_structured_extraction(self, prompt: str) -> str:
        """Use Claude to extract structured data."""
        try:
            response = self.anthropic.messages.create(
                max_tokens=1024,
                model=MODEL_NAME,
                messages=[{'role': 'user', 'content': prompt}]
            )
            
            text_content = ""
            for content in response.content:
                if content.type == 'text':
                    text_content += content.text
            
            return text_content.strip()
            
        except Exception as e:
            logging.error(f"Error in structured extraction: {e}")
            return '{"error": "extraction failed"}'
    
    async def extract_and_store_data(self, user_query: str, llm_response: str, 
                                   source_url: str = None) -> None:
        """Extract structured data from LLM response and store it."""
        try:            
            extraction_prompt = f"""
            Analyze this text and extract pricing information in JSON format:
            
            Text: {llm_response}
            
            Extract pricing plans with this structure:
            {{
                "company_name": "company name",
                "plans": [
                    {{
                        "plan_name": "plan name",
                        "input_tokens": number or null,
                        "output_tokens": number or null,
                        "currency": "USD",
                        "billing_period": "monthly/yearly/one-time",
                        "features": ["feature1", "feature2"],
                        "limitations": "any limitations mentioned",
                        "query": "the user's query"
                    }}
                ]
            }}
            
            Return only valid JSON, no other text. Do not return your response enclosed in ```json```
            """
            
            extraction_response = await self._get_structured_extraction(extraction_prompt)
            extraction_response = extraction_response.replace("```json\n", "").replace("```", "")
            pricing_data = json.loads(extraction_response)
            
            for plan in pricing_data.get("plans", []):
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

                await self.sqlite_server.execute_tool("write_query", {
                    "query": f"""
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
                })
            
            logger.info(f"Stored {len(pricing_data.get('plans', []))} pricing plans")
            
        except Exception as e:
            logging.error(f"Error extracting pricing data: {e}")


# ---------------------------------------------------------------------------
# Standout #5: small terminal UI that streams tool events for transparency
# (scrape -> parse -> DB write -> answer)
# ---------------------------------------------------------------------------
_STAGE_ICONS = {
    "THINKING": "🧠",
    "TOOL_CALL": "🔧",
    "TOOL_RESULT": "✅",
    "TOOL_ERROR": "❌",
    "DB": "🗄️",
    "ANSWER": "💬",
}


def _emit(stage: str, message: str) -> None:
    """Print a single streamed event line for the given pipeline stage."""
    icon = _STAGE_ICONS.get(stage, "•")
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"  [{timestamp}] {icon} {stage:<11} {message}")


class ChatSession:
    """Orchestrates the interaction between user, LLM, and tools."""

    def __init__(self, servers: list[Server], api_key: str) -> None:
        self.servers: list[Server] = servers
        self.anthropic = Anthropic(api_key=api_key)
        self.available_tools: List[ToolDefinition] = []
        self.tool_to_server: Dict[str, str] = {}
        self.sqlite_server: Server | None = None
        self.data_extractor: DataExtractor | None = None

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    async def process_query(self, query: str) -> None:
        """Process a user query and extract/store relevant data."""
        messages = [{'role': 'user', 'content': query}]

        _emit("THINKING", "Sending query to Claude...")
        response = self.anthropic.messages.create(
            max_tokens=2024,
            model=MODEL_NAME,
            tools=self.available_tools,
            messages=messages
        )
        
        full_response = ""
        source_url = None
        used_web_search = False
        
        process_query = True
        while process_query:
            assistant_content = []
            for content in response.content:
                if content.type == 'text':
                    # 1. Accumulate the text into the running full_response.
                    full_response += content.text + "\n"
                    # 2. Keep it as part of the assistant turn.
                    assistant_content.append(content)
                    # 3. If this text block is the *only* content, Claude is done.
                    if len(response.content) == 1:
                        _emit("ANSWER", content.text[:100] + ("..." if len(content.text) > 100 else ""))
                        process_query = False

                elif content.type == 'tool_use':
                    # 1. Append the tool use request to assistant_content, then to messages.
                    assistant_content.append(content)
                    messages.append({'role': 'assistant', 'content': assistant_content})

                    # 2. Get the tool id, args, and name from the content.
                    tool_id = content.id
                    tool_args = content.input
                    tool_name = content.name

                    # 3. Find the server that has this tool.
                    server_name = self.tool_to_server.get(tool_name)
                    server = next((s for s in self.servers if s.name == server_name), None)

                    _emit("TOOL_CALL", f"{tool_name} on '{server_name}' with args={tool_args}")

                    # 4. Execute the tool on that server.
                    if server is None:
                        result_text = f"Error: tool '{tool_name}' is not available on any connected server."
                        _emit("TOOL_ERROR", result_text)
                    else:
                        try:
                            result = await server.execute_tool(tool_name, tool_args)
                            result_text = "".join(
                                block.text for block in result.content if hasattr(block, "text")
                            )
                            _emit("TOOL_RESULT", f"{tool_name} -> {len(result_text)} chars returned")

                            if server.name.lower() == "sqlite":
                                _emit("DB", f"sqlite tool '{tool_name}' executed")
                            else:
                                found_url = self._extract_url_from_result(result_text)
                                if found_url:
                                    source_url = found_url
                        except Exception as e:
                            result_text = f"Error executing tool '{tool_name}': {e}"
                            _emit("TOOL_ERROR", result_text)

                    # 5. Append the tool_result to messages.
                    messages.append({
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': tool_id,
                            'content': result_text
                        }]
                    })

                    # 6. Call the model again with the updated messages list.
                    _emit("THINKING", "Sending tool result back to Claude...")
                    response = self.anthropic.messages.create(
                        max_tokens=2024,
                        model=MODEL_NAME,
                        tools=self.available_tools,
                        messages=messages
                    )

                    # 7. If the new response is just text, capture it and stop the loop;
                    #    otherwise, break out to let the outer while loop process the
                    #    new (possibly tool-using) response.
                    if len(response.content) == 1 and response.content[0].type == 'text':
                        full_response += response.content[0].text + "\n"
                        _emit("ANSWER", response.content[0].text[:100] +
                              ("..." if len(response.content[0].text) > 100 else ""))
                        process_query = False

                    break

        print(f"\n{full_response.strip()}")

        if self.data_extractor and full_response.strip():
            _emit("DB", "Extracting structured pricing data for storage...")
            await self.data_extractor.extract_and_store_data(query, full_response.strip(), source_url)
            _emit("DB", "Pricing data extraction complete.")

    def _extract_url_from_result(self, result_text: str) -> str | None:
        """Extract URL from tool result."""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        urls = re.findall(url_pattern, result_text)
        return urls[0] if urls else None

    async def chat_loop(self) -> None:
        """Run an interactive chat loop."""
        print("\nMCP Chatbot with Data Extraction Started!")
        print("Type your queries, 'show data' to view stored data, or 'quit' to exit.")
        
        while True:
            try:
                query = input("\nQuery: ").strip()
        
                if query.lower() == 'quit':
                    break
                elif query.lower() == 'show data':
                    await self.show_stored_data()
                    continue
                    
                await self.process_query(query)
                print("\n")
                    
            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except Exception as e:
                print(f"\nError: {str(e)}")

    async def show_stored_data(self) -> None:
        """Show recently stored data."""
        if not self.sqlite_server:
            logger.info("No database available")
            return
            
        try:
            pricing = await self.sqlite_server.execute_tool("read_query", {
                "query": "SELECT company_name, plan_name, input_tokens, output_tokens, currency FROM pricing_plans ORDER BY created_at DESC LIMIT 5"
            })

            print("\nRecently Stored Data:")
            print("=" * 50)
            print("\nPricing Plans:")

            # result.content[0].text is the sqlite server's string repr of the
            # rows (single-quoted Python literal, not JSON) --- parse it
            # robustly rather than assuming json.loads will work.
            rows = _parse_sql_tool_result(pricing.content[0].text)

            for plan in rows:
                print(f"  • {plan['company_name']}: {plan['plan_name']} - Input Token ${plan['input_tokens']}, Output Tokens ${plan['output_tokens']}")

            print("=" * 50)
        except Exception as e:
            print(f"Error showing data: {e}")

    async def start(self) -> None:
        """Main chat session handler."""
        try:
            for server in self.servers:
                try:
                    await server.initialize()
                    if "sqlite" in server.name.lower():
                        self.sqlite_server = server
                except Exception as e:
                    logging.error(f"Failed to initialize server: {e}")
                    await self.cleanup_servers()
                    return

            for server in self.servers:
                tools = await server.list_tools()
                self.available_tools.extend(tools)
                for tool in tools:
                    self.tool_to_server[tool["name"]] = server.name

            print(f"\nConnected to {len(self.servers)} server(s)")
            print(f"Available tools: {[tool['name'] for tool in self.available_tools]}")
            
            if self.sqlite_server:
                self.data_extractor = DataExtractor(self.sqlite_server, self.anthropic)
                await self.data_extractor.setup_data_tables()
                print("Data extraction enabled")

            await self.chat_loop()

        finally:
            await self.cleanup_servers()


async def main() -> None:
    """Initialize and run the chat session."""
    config = Configuration()
    
    script_dir = Path(__file__).parent
    config_file = script_dir / "server_config.json"
    
    server_config = config.load_config(config_file)
    
    servers = [Server(name, srv_config) for name, srv_config in server_config["mcpServers"].items()]
    chat_session = ChatSession(servers, config.anthropic_api_key)
    await chat_session.start()


if __name__ == "__main__":
    asyncio.run(main())
