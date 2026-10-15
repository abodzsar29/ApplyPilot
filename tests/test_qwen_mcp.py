import unittest

from applypilot.qwen_mcp import (
    QwenMCPAgent,
    _has_terminal_result,
    _normalize_openrouter_tool_messages,
)


class QwenMCPTests(unittest.TestCase):
    def test_openrouter_tool_result_gets_matching_nonempty_call_id(self):
        source = [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "",
                    "type": "function",
                    "function": {"name": "browser_snapshot", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "id": "",
                "name": "browser_snapshot",
                "content": "snapshot",
                "extra": {"function_id": ""},
            },
        ]

        normalized = _normalize_openrouter_tool_messages(source)
        call_id = normalized[1]["tool_calls"][0]["id"]
        self.assertTrue(call_id)
        self.assertEqual(normalized[2]["tool_call_id"], call_id)
        self.assertNotIn("id", normalized[2])
        self.assertNotIn("extra", normalized[2])
        self.assertEqual(source[1]["tool_calls"][0]["id"], "")

    def test_openrouter_preserves_valid_matching_call_id(self):
        normalized = _normalize_openrouter_tool_messages([
            {"role": "assistant", "tool_calls": [{"id": "call_valid"}]},
            {"role": "tool", "id": "call_valid", "content": "ok"},
        ])
        self.assertEqual(normalized[0]["tool_calls"][0]["id"], "call_valid")
        self.assertEqual(normalized[1]["tool_call_id"], "call_valid")

    def test_terminal_result_detection_is_line_based(self):
        self.assertFalse(_has_terminal_result("I will output RESULT:APPLIED when finished"))
        self.assertTrue(_has_terminal_result("**RESULT:APPLIED**"))
        self.assertTrue(_has_terminal_result("RESULT:FAILED:stuck"))

    def test_agent_continues_after_nonterminal_model_turn(self):
        class FakeAssistant:
            def __init__(self):
                self.prompts = []

            def run(self, messages):
                self.prompts.append(messages[0]["content"])
                content = (
                    "I will now fill out the form."
                    if len(self.prompts) == 1
                    else "RESULT:FAILED:stuck"
                )
                yield [{"role": "assistant", "content": content}]

        agent = QwenMCPAgent.__new__(QwenMCPAgent)
        agent._assistant = FakeAssistant()
        output = agent.run_prompt("Apply now")

        self.assertEqual(len(agent._assistant.prompts), 2)
        self.assertIn("CONTINUATION AFTER AN INCOMPLETE AGENT TURN", agent._assistant.prompts[1])
        self.assertIn("RESULT:FAILED:stuck", output)


if __name__ == "__main__":
    unittest.main()
