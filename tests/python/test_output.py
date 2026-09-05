import unittest

from harnest.output import AgentMetadata, OutputPolicy, TokenUsage


class OutputPolicyTests(unittest.TestCase):
    def test_default_suppresses_only_messages_attached_to_tool_calls(self):
        policy = OutputPolicy()

        self.assertFalse(policy.includes_intermediate_message(has_tool_calls=True))
        self.assertTrue(policy.includes_intermediate_message(has_tool_calls=False))

    def test_include_mode_and_invalid_values_are_explicit(self):
        policy = OutputPolicy(
            subagent_messages="include", agent_metadata="raw"
        )

        self.assertTrue(policy.includes_intermediate_message(has_tool_calls=True))
        self.assertEqual(policy.agent_metadata, "raw")
        with self.assertRaisesRegex(ValueError, "subagent_messages"):
            OutputPolicy(subagent_messages="unexpected")
        with self.assertRaisesRegex(ValueError, "agent_metadata"):
            OutputPolicy(agent_metadata="hidden")

    def test_agent_metadata_has_one_typed_portable_shape(self):
        metadata = AgentMetadata(
            framework="adk",
            usage=TokenUsage(
                input_tokens=12, output_tokens=4, total_tokens=17
            ),
            model="gemini-test",
            finish_reason="STOP",
            raw={"thoughts_token_count": 1},
        )

        self.assertEqual(
            metadata.as_dict(),
            {
                "framework": "adk",
                "usage": {
                    "inputTokens": 12,
                    "outputTokens": 4,
                    "totalTokens": 17,
                },
                "model": "gemini-test",
                "finishReason": "STOP",
                "raw": {"thoughts_token_count": 1},
            },
        )
        with self.assertRaisesRegex(TypeError, "input_tokens"):
            TokenUsage(input_tokens=True, output_tokens=1, total_tokens=2)


if __name__ == "__main__":
    unittest.main()
