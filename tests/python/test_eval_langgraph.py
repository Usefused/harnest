import unittest

from harnest.eval_langgraph import _adk_events
from harnest.runtime_contract import InvocationResult


class LangGraphEvalAdapterTests(unittest.TestCase):
    def test_neutral_tool_trajectory_maps_to_adk_events(self):
        result = InvocationResult(
            text="done",
            events=(
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "lookup",
                    "arguments": {"query": "status"},
                },
                {
                    "type": "tool_result",
                    "id": "call-1",
                    "name": "lookup",
                    "result": {"healthy": True},
                },
                {"type": "message", "role": "assistant", "text": "done"},
            ),
            result=None,
            session_id="session-1",
            metadata={},
        )

        call_event, result_event, final_event = _adk_events(
            result, "invocation-1", "eval_agent"
        )

        call = call_event.content.parts[0].function_call
        response = result_event.content.parts[0].function_response
        self.assertEqual((call.id, call.name, call.args), (
            "call-1", "lookup", {"query": "status"}
        ))
        self.assertEqual(response.id, "call-1")
        self.assertEqual(response.name, "lookup")
        self.assertEqual(response.response, {"healthy": True})
        self.assertEqual(final_event.content.parts[0].text, "done")


if __name__ == "__main__":
    unittest.main()
