from __future__ import annotations

import unittest
from unittest.mock import patch

from nova_validator_parity import (
    CandidateParity,
    check_submission,
    latest_valid_submission_block,
)


TEST_CONFIG = {
    "min_heavy_atoms": 2,
    "min_rotatable_bonds": 0,
    "max_rotatable_bonds": 10,
    "banned_atom_types": ["Se"],
}


class NovaValidatorParityTests(unittest.TestCase):
    @patch("nova_validator_parity.resolve_smiles")
    def test_duplicate_names_reject_whole_submission(self, resolve_smiles) -> None:
        resolve_smiles.return_value = "CCO"
        summary = check_submission(["rxn:1:1:2", "rxn:1:1:2"], config=TEST_CONFIG)
        self.assertFalse(summary.ok)
        self.assertEqual(summary.reason, "duplicate_name")

    @patch("nova_validator_parity.resolve_smiles")
    def test_archive_duplicate_rejects_submission(self, resolve_smiles) -> None:
        resolve_smiles.return_value = "CCO"
        summary = check_submission(
            ["rxn:1:1:2"],
            config=TEST_CONFIG,
            archive_inchikeys={"LFQSCWFLJHTTHZ-UHFFFAOYSA-N"},
        )
        self.assertFalse(summary.ok)
        self.assertEqual(summary.reason, "archive_duplicate")

    @patch("nova_validator_parity.resolve_smiles")
    def test_valid_candidate_passes(self, resolve_smiles) -> None:
        resolve_smiles.return_value = "CCO"
        summary = check_submission(["rxn:1:1:2"], config=TEST_CONFIG, archive_inchikeys=set())
        self.assertTrue(summary.ok)
        self.assertEqual(summary.reason, "ok")
        self.assertIsInstance(summary.results[0], CandidateParity)

    def test_latest_valid_submission_block_is_strict(self) -> None:
        self.assertEqual(latest_valid_submission_block(1000, 10), 989)


if __name__ == "__main__":
    unittest.main()
