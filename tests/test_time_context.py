from __future__ import annotations

import unittest
from datetime import datetime, timezone

from memory.time_context import current_lagos_time, with_current_time


class LagosTimeContextTests(unittest.TestCase):
    def test_formats_aware_instants_in_lagos_local_time(self) -> None:
        utc = datetime(2026, 10, 11, 12, 5, 6, tzinfo=timezone.utc)
        formatted = current_lagos_time(utc)
        self.assertIn("Sunday, 11 October 2026 at 01:05:06 PM WAT", formatted)
        self.assertIn("Africa/Lagos", formatted)

    def test_rejects_naive_injected_datetimes(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            current_lagos_time(datetime(2026, 10, 11, 12, 5))

    def test_prompt_receives_authoritative_fresh_time_context(self) -> None:
        utc = datetime(2026, 10, 11, 12, 5, 6, tzinfo=timezone.utc)
        result = with_current_time("Be concise.  \n", utc)
        self.assertTrue(result.startswith("Be concise."))
        self.assertIn("[Current date/time context]", result)
        self.assertIn("01:05:06 PM WAT", result)
        self.assertIn("Use this as the authoritative current time", result)


if __name__ == "__main__":
    unittest.main()
