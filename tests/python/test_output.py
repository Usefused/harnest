import unittest

from harnest.output import OutputPolicy


class OutputPolicyTests(unittest.TestCase):
    def test_default_suppresses_only_messages_attached_to_tool_calls(self):
        policy = OutputPolicy()

        self.assertFalse(policy.includes_intermediate_message(has_tool_calls=True))
        self.assertTrue(policy.includes_intermediate_message(has_tool_calls=False))

    def test_include_mode_and_invalid_values_are_explicit(self):
        policy = OutputPolicy(subagent_messages="include")

        self.assertTrue(policy.includes_intermediate_message(has_tool_calls=True))
        with self.assertRaisesRegex(ValueError, "subagent_messages"):
            OutputPolicy(subagent_messages="unexpected")


if __name__ == "__main__":
    unittest.main()
