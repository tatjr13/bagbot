"""YAML config loader for Brains strategy plugin."""

import os
import yaml
import logging

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'threshold_farm.yaml')

_cached_config = None
_cached_mtime_ns = None


def load_config(path=None):
    """Load and return the threshold_farm config as a dict."""
    global _cached_config, _cached_mtime_ns

    path = path or _CONFIG_PATH
    try:
        current_mtime_ns = os.stat(path).st_mtime_ns
        if path == _CONFIG_PATH and _cached_config is not None and _cached_mtime_ns == current_mtime_ns:
            return _cached_config

        with open(path, 'r') as f:
            cfg = yaml.safe_load(f)
        if path == _CONFIG_PATH:
            _cached_config = cfg
            _cached_mtime_ns = current_mtime_ns
        return cfg
    except FileNotFoundError:
        logger.error(f'Config file not found: {path}')
        raise
    except yaml.YAMLError as e:
        logger.error(f'Invalid YAML in config: {e}')
        raise


def reload_config():
    """Force reload config from disk."""
    global _cached_config, _cached_mtime_ns
    _cached_config = None
    _cached_mtime_ns = None
    return load_config()


def get(key, default=None):
    """Get a top-level config value."""
    cfg = load_config()
    return cfg.get(key, default)


def get_lookback(key, default=None):
    """Get a lookback window config value."""
    cfg = load_config()
    lookbacks = cfg.get('lookbacks', {})
    return lookbacks.get(key, default)
