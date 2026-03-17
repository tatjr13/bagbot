"""Tests for operator-facing Arbos status rendering."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from Brains.arbos_status import build_status


class TestArbosStatus(unittest.TestCase):
    def test_build_status_reports_promoted_derived_wallets(self):
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            log_path = base / "staking.log"
            wallet_report = base / "WALLET_TRACKERS.md"
            log_path.write_text(
                "\n".join(
                    [
                        '2026-03-17 06:16:19,179 - INFO - {wallet_value:"30.39 + 0.00", sn11: 922.8, sn62: 447.8, sn71: 303.4}',
                        "2026-03-17 06:16:19,180 - INFO - Brains runtime roster refreshed: live=[4, 75, 93], buy_enabled=[4, 75], exit_only=[11]",
                        "2026-03-17 06:16:19,181 - INFO - Fee buffer reached or remaining spendable balance too small to trade",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            wallet_report.write_text(
                "\n".join(
                    [
                        "# Wallet Trackers",
                        "",
                        "Generated: `2026-03-17T06:16:12Z`",
                        "",
                        "## Active Watchlist",
                        "",
                        "1. **Seed** | tier=`seed` | intel_score=`10.00`",
                        "2. **Derived A** | tier=`derived` | intel_score=`5.00`",
                        "3. **Derived B** | tier=`derived` | intel_score=`4.50`",
                        "",
                        "## Ranked Precursor Candidates",
                        "",
                        "1. `wallet-a` | score=`5.00` | lead_count=`2` | mev_ratio=`0.00`",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            status = build_status(log_path=log_path, wallet_report_path=wallet_report, tail_count=100)
            self.assertIn("2 derived wallets are currently promoted into the active watchlist.", status)
            self.assertIn("Wallet-intel report generated: `2026-03-17T06:16:12Z`", status)


if __name__ == "__main__":
    unittest.main()
