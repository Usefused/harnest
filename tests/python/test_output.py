import unittest

from harnest.output import (
    AgentMetadata,
    AgentMetadataMode,
    OutputPolicy,
    TokenUsage,
)


class OutputPolicyTests(unittest.TestCase):
    def test_default_suppresses_thinking_and_messages_attached_to_tool_calls(self):
        policy = OutputPolicy()

        self.assertFalse(policy.thinking)
        self.assertTrue(policy.tool_activity)
        self.assertIs(policy.agent_metadata, AgentMetadataMode.NORMALIZED)
        self.assertFalse(policy.includes_intermediate_message(has_tool_calls=True))
        self.assertTrue(policy.includes_intermediate_message(has_tool_calls=False))
        self.assertFalse(policy.includes_event("thinking"))
        self.assertTrue(policy.includes_event("tool_call"))
        self.assertTrue(policy.includes_event("agent_metadata"))

    def test_explicit_policy_values_and_invalid_values(self):
        policy = OutputPolicy(
            subagent_messages=True,
            tool_activity=False,
            thinking=True,
            agent_metadata=AgentMetadataMode.RAW,
            persist_raw_agent_metadata=True,
        )

        self.assertTrue(policy.includes_intermediate_message(has_tool_calls=True))
        self.assertFalse(policy.includes_event("tool_result"))
        self.assertTrue(policy.thinking)
        self.assertIs(policy.agent_metadata, AgentMetadataMode.RAW)
        self.assertTrue(policy.persist_raw_agent_metadata)
        with self.assertRaisesRegex(TypeError, "subagent_messages"):
            OutputPolicy(subagent_messages="include")
        with self.assertRaisesRegex(TypeError, "agent_metadata"):
            OutputPolicy(agent_metadata="raw")
        with self.assertRaisesRegex(TypeError, "tool_activity"):
            OutputPolicy(tool_activity="suppress")
        with self.assertRaisesRegex(TypeError, "thinking"):
            OutputPolicy(thinking="include")
        with self.assertRaisesRegex(TypeError, "thinking"):
            OutputPolicy(thinking=1)
        with self.assertRaisesRegex(TypeError, "persist_raw_agent_metadata"):
            OutputPolicy(
                agent_metadata=AgentMetadataMode.RAW,
                persist_raw_agent_metadata=1,
            )
        with self.assertRaisesRegex(ValueError, "AgentMetadataMode.RAW"):
            OutputPolicy(persist_raw_agent_metadata=True)
        with self.assertRaisesRegex(ValueError, "AgentMetadataMode.RAW"):
            OutputPolicy(
                agent_metadata=AgentMetadataMode.SUPPRESS,
                persist_raw_agent_metadata=True,
            )

    def test_private_activity_profile_keeps_only_customer_output(self):
        policy = OutputPolicy(
            tool_activity=False,
            thinking=False,
            agent_metadata=AgentMetadataMode.SUPPRESS,
        )

        self.assertTrue(policy.includes_event("message"))
        self.assertTrue(policy.includes_event("graph_output"))
        for event_type in (
            "tool_call",
            "tool_result",
            "thinking",
            "agent_metadata",
        ):
            with self.subTest(event_type=event_type):
                self.assertFalse(policy.includes_event(event_type))

    def test_policy_is_keyword_only(self):
        with self.assertRaises(TypeError):
            OutputPolicy(False)

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
