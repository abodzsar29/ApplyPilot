"""Optional installed-dependency smoke tests: RUN_AGENT_TESTS=1 to enable."""

import asyncio
import os
import sys
import unittest


@unittest.skipUnless(os.environ.get("RUN_AGENT_TESTS") == "1", "requires qwen extra")
class QwenMCPDependencyTests(unittest.TestCase):
    def test_assistant_import(self):
        from qwen_agent.agents import Assistant

        self.assertTrue(callable(Assistant))

    def test_openrouter_model_serializes_tool_call_ids(self):
        from applypilot.qwen_mcp import _openrouter_chat_model
        from qwen_agent.llm.schema import FunctionCall, Message

        model = _openrouter_chat_model({
            "model": "mistralai/mistral-nemo",
            "model_type": "oai",
            "model_server": "https://openrouter.ai/api/v1",
            "api_key": "not-used",
            "generate_cfg": {"use_raw_api": True},
        })
        converted = model.convert_messages_to_dicts([
            Message(role="user", content="inspect"),
            Message(
                role="assistant",
                content="",
                function_call=FunctionCall(name="browser_snapshot", arguments="{}"),
                extra={"function_id": ""},
            ),
            Message(
                role="function",
                name="browser_snapshot",
                content="snapshot",
                extra={"function_id": ""},
            ),
        ])
        call_id = converted[1]["tool_calls"][0]["id"]
        self.assertTrue(call_id)
        self.assertEqual(converted[2]["tool_call_id"], call_id)

    def test_stdio_connection_and_tool_call(self):
        from qwen_agent.tools.mcp_manager import MCPClient

        server_code = """
from mcp.server.fastmcp import FastMCP
server = FastMCP("dependency-smoke")
@server.tool()
def ping() -> str:
    return "dependency-smoke-ok"
server.run(transport="stdio")
"""

        async def check_connection():
            client = MCPClient()
            try:
                await client.connection_server(
                    "dependency-smoke", {"command": sys.executable, "args": ["-c", server_code]}
                )
                self.assertIn("ping", [tool.name for tool in client.tools])
                result = await client.session.call_tool("ping", {})
                self.assertFalse(result.isError)
                self.assertEqual(result.content[0].text, "dependency-smoke-ok")
            finally:
                await client.cleanup()

        asyncio.run(asyncio.wait_for(check_connection(), timeout=30))
