from __future__ import annotations

import unittest

from harnest.lifecycle_transition import Finish, Next, TransitionContext, UNCHANGED


class LifecycleTransitionTests(unittest.TestCase):
    def test_next_without_replacement_is_distinct_from_none(self):
        context = TransitionContext()

        unchanged = context.next()
        replaced = context.next(None)

        self.assertIsInstance(unchanged, Next)
        self.assertIs(unchanged.value, UNCHANGED)
        self.assertFalse(unchanged.replaces)
        self.assertTrue(replaced.replaces)
        self.assertIsNone(replaced.value)

    def test_finish_preserves_the_terminal_result(self):
        result = object()

        self.assertEqual(TransitionContext().finish(result), Finish(result))


if __name__ == "__main__":
    unittest.main()
