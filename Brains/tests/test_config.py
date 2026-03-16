"""Tests for Brains config reload behavior."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from Brains import config


class TestConfigReload(unittest.TestCase):

    def test_load_config_reloads_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "threshold_farm.yaml"
            cfg_path.write_text("risk_mode_default: conservative\n", encoding="utf-8")

            with patch.object(config, "_CONFIG_PATH", str(cfg_path)):
                config.reload_config()
                first = config.load_config()
                self.assertEqual(first["risk_mode_default"], "conservative")

                time.sleep(0.001)
                cfg_path.write_text("risk_mode_default: aggressive\n", encoding="utf-8")
                second = config.load_config()
                self.assertEqual(second["risk_mode_default"], "aggressive")


if __name__ == "__main__":
    unittest.main()
