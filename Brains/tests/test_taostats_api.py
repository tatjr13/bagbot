"""Tests for the Taostats helper."""

import unittest
from unittest.mock import patch

from Brains import taostats_api


class TestTaostatsApi(unittest.TestCase):

    def test_build_url_without_query(self):
        self.assertEqual(
            taostats_api.build_url("/api/stats/latest/v1", []),
            "https://api.taostats.io/api/stats/latest/v1",
        )

    def test_build_url_with_query(self):
        self.assertEqual(
            taostats_api.build_url("api/subnets/v1", ["page=1", "limit=50"]),
            "https://api.taostats.io/api/subnets/v1?page=1&limit=50",
        )

    def test_build_url_rejects_invalid_param(self):
        with self.assertRaises(ValueError):
            taostats_api.build_url("/api/subnets/v1", ["broken"])

    def test_fetch_requires_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                taostats_api.fetch("https://api.taostats.io/api/stats/latest/v1", 1.0)


if __name__ == "__main__":
    unittest.main()
