"""Tests for the wallet tracker sidecar loop."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Brains.wallet_tracker_loop import _unique_paths, run_cycle


class TestWalletTrackerLoop(unittest.TestCase):
    def test_unique_paths_deduplicates_in_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            paths = _unique_paths(
                [
                    base / "a.md",
                    base / "b.md",
                    base / "a.md",
                    Path(str(base / "b.md")),
                ]
            )
            self.assertEqual(paths, [base / "a.md", base / "b.md"])

    @patch("Brains.wallet_tracker_loop.build_status", return_value="# status\n")
    @patch("Brains.wallet_tracker_loop.refresh_tracker")
    def test_run_cycle_mirrors_report_and_status(self, refresh_tracker_mock, build_status_mock):
        refresh_tracker_mock.return_value = {"meta": {"tracked_wallet_count": 3}}
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            config_path = base / "cfg.json"
            state_path = base / "state.json"
            report_output = base / "wallet.md"
            report_output.write_text("# wallet\n", encoding="utf-8")
            report_mirror = base / "mirror" / "wallet.md"
            status_output = base / "status.md"
            status_mirror = base / "mirror" / "status.md"
            log_path = base / "staking.log"
            log_path.write_text("log\n", encoding="utf-8")

            def fake_refresh_tracker(**kwargs):
                kwargs["report_output"].write_text("# wallet\n", encoding="utf-8")
                return {"meta": {"tracked_wallet_count": 3}}

            refresh_tracker_mock.side_effect = fake_refresh_tracker
            state = run_cycle(
                config_path=config_path,
                state_path=state_path,
                report_output=report_output,
                report_mirrors=[report_mirror],
                status_output=status_output,
                status_mirrors=[status_mirror],
                log_path=log_path,
                timeout=20.0,
                tail_lines=100,
                force=True,
            )

            self.assertEqual(state["meta"]["tracked_wallet_count"], 3)
            self.assertEqual(report_mirror.read_text(encoding="utf-8"), "# wallet\n")
            self.assertEqual(status_output.read_text(encoding="utf-8"), "# status\n")
            self.assertEqual(status_mirror.read_text(encoding="utf-8"), "# status\n")
            build_status_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
