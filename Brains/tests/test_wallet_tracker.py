"""Tests for wallet tracker ranking and MEV filtering."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from Brains.wallet_tracker import (
    WalletEvent,
    find_precursor_events,
    is_probable_mev_wallet,
    merge_candidate_scores,
    refresh_tracker,
    select_active_wallets,
    select_promoted_wallets,
)


class TestWalletTracker(unittest.TestCase):
    def _event(self, wallet, netuid, action, amount_tao, minutes_offset):
        base = datetime(2026, 3, 17, 5, 0, tzinfo=timezone.utc)
        return WalletEvent(
            wallet_ss58=wallet,
            netuid=netuid,
            action=action,
            amount_tao=amount_tao,
            timestamp=base + timedelta(minutes=minutes_offset),
            extrinsic_id=f"{wallet}-{netuid}-{action}-{minutes_offset}",
            delegate_ss58=None,
            delegate_name=None,
        )

    def test_mev_detection_flags_small_roundtrip(self):
        candidate = self._event("precursor", 66, "DELEGATE", 0.9, 0)
        subnet_events = [
            candidate,
            self._event("precursor", 66, "UNDELEGATE", 0.8, 8),
        ]
        self.assertTrue(
            is_probable_mev_wallet(
                subnet_events,
                candidate,
                {
                    "roundtrip_minutes": 20,
                    "burst_minutes": 60,
                    "burst_count": 4,
                    "max_notional_tao": 2.0,
                    "burst_max_notional_tao": 5.0,
                },
            )
        )

    def test_find_precursor_events_filters_self_and_marks_mev(self):
        tracked = self._event("tracked", 66, "DELEGATE", 12.0, 60)
        subnet_events = [
            self._event("tracked", 66, "DELEGATE", 10.0, 30),
            self._event("good", 66, "DELEGATE", 25.0, 20),
            self._event("mev", 66, "DELEGATE", 0.9, 10),
            self._event("mev", 66, "UNDELEGATE", 0.8, 18),
        ]
        hits = find_precursor_events(
            tracked,
            subnet_events,
            {
                "precursor_min_lead_minutes": 5,
                "precursor_max_lead_hours": 24,
                "candidate_min_amount_tao": 0.5,
                "mev_filters": {
                    "roundtrip_minutes": 20,
                    "burst_minutes": 60,
                    "burst_count": 4,
                    "max_notional_tao": 2.0,
                    "burst_max_notional_tao": 5.0,
                },
            },
        )
        wallets = [hit["wallet_ss58"] for hit in hits]
        self.assertIn("good", wallets)
        self.assertIn("mev", wallets)
        good_hit = next(hit for hit in hits if hit["wallet_ss58"] == "good")
        mev_hit = next(hit for hit in hits if hit["wallet_ss58"] == "mev")
        self.assertFalse(good_hit["mev_like"])
        self.assertTrue(mev_hit["mev_like"])
        self.assertGreater(good_hit["score"], mev_hit["score"])

    def test_promoted_wallets_require_repeated_non_mev_hits(self):
        merged = merge_candidate_scores(
            {},
            [
                {
                    "wallet_ss58": "good",
                    "netuid": 66,
                    "tracked_wallet_ss58": "seed-a",
                    "timestamp": "2026-03-17T05:00:00Z",
                    "score": 3.0,
                    "mev_like": False,
                },
                {
                    "wallet_ss58": "good",
                    "netuid": 71,
                    "tracked_wallet_ss58": "seed-b",
                    "timestamp": "2026-03-17T06:00:00Z",
                    "score": 3.0,
                    "mev_like": False,
                },
                {
                    "wallet_ss58": "mev",
                    "netuid": 66,
                    "tracked_wallet_ss58": "seed-a",
                    "timestamp": "2026-03-17T05:00:00Z",
                    "score": 4.0,
                    "mev_like": True,
                },
                {
                    "wallet_ss58": "mev",
                    "netuid": 71,
                    "tracked_wallet_ss58": "seed-b",
                    "timestamp": "2026-03-17T06:00:00Z",
                    "score": 4.0,
                    "mev_like": True,
                },
            ],
        )
        promoted = select_promoted_wallets(
            merged,
            {
                "promotion": {"min_score": 4.0, "min_lead_count": 2},
                "max_dynamic_wallets": 8,
            },
        )
        promoted_wallets = [wallet["wallet_ss58"] for wallet in promoted]
        self.assertIn("good", promoted_wallets)
        self.assertNotIn("mev", promoted_wallets)

    def test_merge_candidate_scores_deduplicates_repolled_hits(self):
        hit = {
            "wallet_ss58": "good",
            "netuid": 66,
            "tracked_wallet_ss58": "seed-a",
            "tracked_extrinsic_id": "tracked-1",
            "candidate_extrinsic_id": "candidate-1",
            "timestamp": "2026-03-17T05:00:00Z",
            "score": 3.0,
            "mev_like": False,
        }
        merged = merge_candidate_scores({}, [hit, dict(hit)])
        candidate = merged["good"]
        self.assertEqual(candidate["lead_count"], 1)
        self.assertEqual(candidate["intel_score"], 3.0)
        self.assertEqual(candidate["seed_hits"]["seed-a"], 1)
        self.assertEqual(len(candidate["seen_hit_ids"]), 1)

    def test_active_wallet_selection_rotates_medium_priority_seeds(self):
        seeds = [
            {"label": "high", "ss58": "high", "priority": "high", "intel_score": 10.0},
            {"label": "m1", "ss58": "m1", "priority": "medium", "intel_score": 6.0},
            {"label": "m2", "ss58": "m2", "priority": "medium", "intel_score": 5.5},
            {"label": "m3", "ss58": "m3", "priority": "medium", "intel_score": 5.0},
            {"label": "m4", "ss58": "m4", "priority": "medium", "intel_score": 4.5},
        ]
        active, cursor = select_active_wallets(
            seeds,
            [],
            {"meta": {"wallet_cursor": 1}},
            {"max_rotating_wallets_per_cycle": 2},
        )
        self.assertEqual([wallet["ss58"] for wallet in active], ["high", "m2", "m3"])
        self.assertEqual(cursor, 3)

    def test_active_wallet_selection_caps_promoted_wallets_per_cycle(self):
        seeds = [
            {"label": "high", "ss58": "high", "priority": "high", "intel_score": 10.0},
            {"label": "m1", "ss58": "m1", "priority": "medium", "intel_score": 6.0},
        ]
        promoted = [
            {"wallet_ss58": "p1", "tier": "derived", "intel_score": 9.0},
            {"wallet_ss58": "p2", "tier": "derived", "intel_score": 8.0},
            {"wallet_ss58": "p3", "tier": "derived", "intel_score": 7.0},
        ]
        active, cursor = select_active_wallets(
            seeds,
            promoted,
            {"meta": {"wallet_cursor": 0}},
            {"max_rotating_wallets_per_cycle": 1, "max_promoted_wallets_per_cycle": 2},
        )
        self.assertEqual([wallet["ss58"] for wallet in active], ["high", "p1", "p2", "m1"])
        self.assertEqual(cursor, 0)

    @patch("Brains.wallet_tracker.fetch_subnet_events", return_value=[])
    @patch("Brains.wallet_tracker.fetch_wallet_events", return_value=[])
    @patch("Brains.wallet_tracker.fetch_wallet_positions", return_value=[])
    def test_refresh_tracker_drops_stale_skipped_wallet_snapshots(
        self,
        _positions_mock,
        _events_mock,
        _subnet_mock,
    ):
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            config_path = base / "cfg.json"
            state_path = base / "state.json"
            report_path = base / "report.md"
            config_path.write_text(
                """
{
  "refresh_minutes": 0,
  "tracked_event_lookback_hours": 24,
  "tracked_event_limit": 24,
  "subnet_event_limit": 120,
  "max_rotating_wallets_per_cycle": 1,
  "wallets": [
    {"label": "high", "ss58": "high", "priority": "high", "intel_score": 10.0},
    {"label": "m1", "ss58": "m1", "priority": "medium", "intel_score": 6.0},
    {"label": "m2", "ss58": "m2", "priority": "medium", "intel_score": 5.0}
  ]
}
                """.strip(),
                encoding="utf-8",
            )
            state_path.write_text(
                """
{
  "meta": {"state_schema_version": 2, "wallet_cursor": 0},
  "wallet_snapshots": {
    "m2": {
      "label": "stale",
      "positions": [{"netuid": 66, "balance_tao": 9.0}],
      "recent_events": [{"action": "DELEGATE", "netuid": 66, "amount_tao": 9.0, "timestamp": "2026-03-17T05:00:00Z"}]
    }
  },
  "candidates": {}
}
                """.strip(),
                encoding="utf-8",
            )

            refreshed = refresh_tracker(
                config_path=config_path,
                state_path=state_path,
                report_output=report_path,
                desktop_output=None,
                timeout=5.0,
                force=True,
            )

            self.assertIn("high", refreshed["wallet_snapshots"])
            self.assertIn("m1", refreshed["wallet_snapshots"])
            self.assertNotIn("m2", refreshed["wallet_snapshots"])


if __name__ == "__main__":
    unittest.main()
