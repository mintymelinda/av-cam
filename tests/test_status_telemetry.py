from __future__ import annotations

import builtins
import unittest
from io import StringIO
from unittest.mock import patch

from petcam.infrastructure.system.metrics import ResourceMetrics


class ResourceMetricsTests(unittest.TestCase):
    def test_resource_metrics_sample_changes_between_intervals(self):
        proc_stat = ["cpu 100 0 0 100 0 0 0 0\n", "cpu 150 0 0 150 0 0 0 0\n"]
        meminfo = "MemTotal:       438272 kB\nMemAvailable:   327680 kB\n"
        responses = iter([proc_stat[0], meminfo, proc_stat[1], meminfo])

        def fake_open(*args, **kwargs):
            return StringIO(next(responses))

        metrics = ResourceMetrics()
        with patch.object(builtins, "open", side_effect=fake_open):
            first = metrics.sample()
            second = metrics.sample()

        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(second["cpu_percent"], 50.0)
        self.assertEqual(second["memory_used_mb"], 108)
        self.assertEqual(second["memory_total_mb"], 428)


if __name__ == "__main__":
    unittest.main()
