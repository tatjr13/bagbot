"""Persistence layer: SQLite price bars + JSON strategy state."""

import json
import os
import sqlite3
import time
import logging
from typing import List, Dict, Optional, Tuple

from Brains.models import SubnetState, FillRecord

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), 'price_history.db')
_STATE_DIR = os.path.join(os.path.dirname(__file__), 'state')
_STATE_PATH = os.path.join(_STATE_DIR, 'threshold_farm_state.json')


class PriceBarStore:
    """SQLite store for 15-minute OHLC price bars per subnet."""

    def __init__(self, db_path=None):
        self.db_path = db_path or _DB_PATH
        self._conn = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS price_bars (
                bar_time INTEGER NOT NULL,
                netuid INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                tao_in REAL NOT NULL,
                alpha_in REAL NOT NULL,
                tick_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (bar_time, netuid)
            )
        ''')
        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                netuid INTEGER NOT NULL,
                side TEXT NOT NULL,
                tao_amount REAL NOT NULL,
                alpha_amount REAL NOT NULL,
                price REAL NOT NULL,
                tx_hash TEXT DEFAULT ''
            )
        ''')
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_bars_netuid_time ON price_bars(netuid, bar_time)'
        )
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_fills_netuid ON fills(netuid, timestamp)'
        )
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def bar_time(self, timestamp: float, bar_minutes: int = 15) -> int:
        """Round a timestamp down to the nearest bar boundary."""
        bar_seconds = bar_minutes * 60
        return int(timestamp // bar_seconds) * bar_seconds

    def record_tick(self, netuid: int, price: float, tao_in: float, alpha_in: float,
                    timestamp: float = None, bar_minutes: int = 15):
        """Record a price tick, aggregating into the current bar."""
        ts = timestamp or time.time()
        bt = self.bar_time(ts, bar_minutes)

        existing = self._conn.execute(
            'SELECT open, high, low, close, tao_in, alpha_in, tick_count '
            'FROM price_bars WHERE bar_time = ? AND netuid = ?',
            (bt, netuid)
        ).fetchone()

        if existing:
            o, h, l, c, ti, ai, tc = existing
            self._conn.execute(
                'UPDATE price_bars SET high = ?, low = ?, close = ?, '
                'tao_in = ?, alpha_in = ?, tick_count = ? '
                'WHERE bar_time = ? AND netuid = ?',
                (max(h, price), min(l, price), price, tao_in, alpha_in, tc + 1, bt, netuid)
            )
        else:
            self._conn.execute(
                'INSERT INTO price_bars (bar_time, netuid, open, high, low, close, tao_in, alpha_in, tick_count) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)',
                (bt, netuid, price, price, price, price, tao_in, alpha_in)
            )
        self._conn.commit()

    def get_bars(self, netuid: int, hours: float, now: float = None) -> List[Tuple]:
        """Get price bars for a subnet over the last N hours.

        Returns list of (bar_time, open, high, low, close, tao_in, alpha_in, tick_count).
        """
        now = now or time.time()
        cutoff = now - (hours * 3600)
        rows = self._conn.execute(
            'SELECT bar_time, open, high, low, close, tao_in, alpha_in, tick_count '
            'FROM price_bars WHERE netuid = ? AND bar_time >= ? ORDER BY bar_time ASC',
            (netuid, int(cutoff))
        ).fetchall()
        return rows

    def get_close_prices(self, netuid: int, hours: float, now: float = None) -> List[float]:
        """Get just close prices for a subnet over the last N hours."""
        bars = self.get_bars(netuid, hours, now)
        return [b[4] for b in bars]  # index 4 = close

    def list_netuids(self) -> List[int]:
        """Return all netuids present in the price history store."""
        rows = self._conn.execute(
            'SELECT DISTINCT netuid FROM price_bars ORDER BY netuid ASC'
        ).fetchall()
        return [row[0] for row in rows]

    def get_bars_between(
        self,
        start_time: float,
        end_time: float,
        netuids: Optional[List[int]] = None,
    ) -> List[Tuple]:
        """Get raw bars across a time range for one or more subnets.

        Returns list of
        (bar_time, netuid, open, high, low, close, tao_in, alpha_in, tick_count).
        """
        params: List[object] = [int(start_time), int(end_time)]
        query = (
            'SELECT bar_time, netuid, open, high, low, close, tao_in, alpha_in, tick_count '
            'FROM price_bars WHERE bar_time >= ? AND bar_time <= ?'
        )
        if netuids:
            placeholders = ','.join('?' for _ in netuids)
            query += f' AND netuid IN ({placeholders})'
            params.extend(int(netuid) for netuid in netuids)
        query += ' ORDER BY bar_time ASC, netuid ASC'
        return self._conn.execute(query, tuple(params)).fetchall()

    def get_bar_count(self, netuid: int, hours: float = None, now: float = None) -> int:
        """Count available bars for a subnet."""
        now = now or time.time()
        if hours is None:
            row = self._conn.execute(
                'SELECT COUNT(*) FROM price_bars WHERE netuid = ?', (netuid,)
            ).fetchone()
        else:
            cutoff = now - (hours * 3600)
            row = self._conn.execute(
                'SELECT COUNT(*) FROM price_bars WHERE netuid = ? AND bar_time >= ?',
                (netuid, int(cutoff))
            ).fetchone()
        return row[0] if row else 0

    def get_earliest_bar_time(self, netuid: int) -> Optional[float]:
        """Get the earliest bar timestamp for a subnet."""
        row = self._conn.execute(
            'SELECT MIN(bar_time) FROM price_bars WHERE netuid = ?', (netuid,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_latest_bar_time(self, netuid: Optional[int] = None) -> Optional[float]:
        """Get the latest recorded bar timestamp."""
        if netuid is None:
            row = self._conn.execute('SELECT MAX(bar_time) FROM price_bars').fetchone()
        else:
            row = self._conn.execute(
                'SELECT MAX(bar_time) FROM price_bars WHERE netuid = ?',
                (netuid,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_history_hours(self, netuid: int, now: float = None) -> float:
        """How many hours of price history do we have for this subnet?"""
        earliest = self.get_earliest_bar_time(netuid)
        if earliest is None:
            return 0.0
        now = now or time.time()
        return (now - earliest) / 3600.0

    def prune(self, max_hours: int = 96):
        """Remove bars older than max_hours."""
        cutoff = time.time() - (max_hours * 3600)
        self._conn.execute('DELETE FROM price_bars WHERE bar_time < ?', (int(cutoff),))
        self._conn.commit()

    def record_fill(self, fill: FillRecord):
        """Record a confirmed trade execution."""
        self._conn.execute(
            'INSERT INTO fills (timestamp, netuid, side, tao_amount, alpha_amount, price, tx_hash) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (fill.timestamp, fill.netuid, fill.side, fill.tao_amount,
             fill.alpha_amount, fill.price, fill.tx_hash)
        )
        self._conn.commit()

    def get_fills(self, netuid: int, hours: float = 24, now: float = None) -> List[FillRecord]:
        """Get fills for a subnet over the last N hours."""
        now = now or time.time()
        cutoff = now - (hours * 3600)
        rows = self._conn.execute(
            'SELECT timestamp, netuid, side, tao_amount, alpha_amount, price, tx_hash '
            'FROM fills WHERE netuid = ? AND timestamp >= ? ORDER BY timestamp ASC',
            (netuid, cutoff)
        ).fetchall()
        return [FillRecord(
            timestamp=r[0], netuid=r[1], side=r[2], tao_amount=r[3],
            alpha_amount=r[4], price=r[5], tx_hash=r[6]
        ) for r in rows]

    def get_daily_turnover(self, netuid: int, now: float = None) -> Tuple[float, float]:
        """Get total buy and sell TAO turnover in the last 24h."""
        now = now or time.time()
        cutoff = now - 86400
        buy_row = self._conn.execute(
            'SELECT COALESCE(SUM(tao_amount), 0) FROM fills '
            'WHERE netuid = ? AND side = ? AND timestamp >= ?',
            (netuid, 'buy', cutoff)
        ).fetchone()
        sell_row = self._conn.execute(
            'SELECT COALESCE(SUM(tao_amount), 0) FROM fills '
            'WHERE netuid = ? AND side = ? AND timestamp >= ?',
            (netuid, 'sell', cutoff)
        ).fetchone()
        return (buy_row[0], sell_row[0])


class StrategyStateStore:
    """JSON file store for per-subnet strategy state."""

    def __init__(self, state_path=None):
        self.state_path = state_path or _STATE_PATH
        self._states: Dict[int, SubnetState] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r') as f:
                    data = json.load(f)
                for netuid_str, state_dict in data.items():
                    netuid = int(netuid_str)
                    self._states[netuid] = SubnetState(netuid=netuid, **state_dict)
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f'Could not load strategy state, starting fresh: {e}')
                self._states = {}

    def save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        data = {}
        for netuid, state in self._states.items():
            d = {
                'last_patch_at': state.last_patch_at,
                'last_trade_at': state.last_trade_at,
                'last_buy_lower': state.last_buy_lower,
                'last_buy_upper': state.last_buy_upper,
                'last_sell_lower': state.last_sell_lower,
                'last_sell_upper': state.last_sell_upper,
                'daily_buy_tao': state.daily_buy_tao,
                'daily_sell_tao': state.daily_sell_tao,
                'daily_turnover_reset_at': state.daily_turnover_reset_at,
                'avg_entry_price': state.avg_entry_price,
                'total_cost_basis_tao': state.total_cost_basis_tao,
                'total_alpha_bought': state.total_alpha_bought,
                'regime': state.regime,
            }
            data[str(netuid)] = d
        with open(self.state_path, 'w') as f:
            json.dump(data, f, indent=2)

    def get(self, netuid: int) -> SubnetState:
        if netuid not in self._states:
            self._states[netuid] = SubnetState(netuid=netuid)
        return self._states[netuid]

    def update_cost_basis(self, netuid: int, fill: FillRecord):
        """Update average entry price from a confirmed fill."""
        state = self.get(netuid)
        if fill.side == 'buy':
            state.total_cost_basis_tao += fill.tao_amount
            state.total_alpha_bought += fill.alpha_amount
            if state.total_alpha_bought > 0:
                state.avg_entry_price = state.total_cost_basis_tao / state.total_alpha_bought
        elif fill.side == 'sell':
            # Reduce cost basis proportionally
            if state.total_alpha_bought > 0:
                sell_fraction = min(fill.alpha_amount / state.total_alpha_bought, 1.0)
                state.total_cost_basis_tao *= (1.0 - sell_fraction)
                state.total_alpha_bought = max(0, state.total_alpha_bought - fill.alpha_amount)
                if state.total_alpha_bought > 0:
                    state.avg_entry_price = state.total_cost_basis_tao / state.total_alpha_bought
                else:
                    state.avg_entry_price = None
        state.last_trade_at = fill.timestamp
        self.save()
