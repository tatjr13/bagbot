"""Read-only wallet intelligence tracker for Arbos."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "arbos" / "WALLET_TRACKERS_SEEDS.json"
DEFAULT_STATE_PATH = ROOT / "arbos" / "WALLET_TRACKERS_STATE.json"
DEFAULT_REPORT_PATH = ROOT / "arbos" / "WALLET_TRACKERS.md"
DEFAULT_DESKTOP_REPORT_PATH = Path.home() / "Desktop" / "WALLET_TRACKERS.md"
DEFAULT_TIMEOUT_SECONDS = 20.0
STATE_SCHEMA_VERSION = 2

if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from Brains import taostats_api


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def dt_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def rao_to_tao(value) -> float:
    try:
        return float(value or 0.0) / 1_000_000_000.0
    except (TypeError, ValueError):
        return 0.0


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def fetch_taostats(path: str, params: Iterable[str], timeout: float) -> dict | list:
    url = taostats_api.build_url(path, list(params))
    retry_attempts = int(os.environ.get("WALLET_TRACKER_FETCH_RETRY_ATTEMPTS", 4) or 4)
    retry_base_seconds = float(os.environ.get("WALLET_TRACKER_FETCH_RETRY_BASE_SECONDS", 15) or 15)
    for attempt in range(retry_attempts):
        taostats_api.enforce_rate_limit(
            float(os.environ.get("TAOSTATS_RATE_LIMIT_PER_MIN", 5) or 5),
            no_wait=False,
        )
        try:
            return taostats_api.fetch(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == retry_attempts - 1:
                raise
            time.sleep(retry_base_seconds * (attempt + 1))
    raise RuntimeError("unreachable")


def _priority_rank(wallet: Dict) -> int:
    priority = str(wallet.get("priority", "")).lower()
    if priority == "high":
        return 0
    if priority == "medium":
        return 1
    return 2


def select_active_wallets(seed_wallets: List[Dict], promoted: List[Dict], state: Dict, cfg: Dict) -> tuple[List[Dict], int]:
    seen = set()
    active: List[Dict] = []
    high_priority_seeds: List[Dict] = []
    promoted_priority: List[Dict] = []
    rotating_pool: List[Dict] = []

    combined = list(seed_wallets) + list(promoted)
    for wallet in combined:
        ss58 = wallet["ss58"] if "ss58" in wallet else wallet["wallet_ss58"]
        if ss58 in seen:
            continue
        seen.add(ss58)
        normalized = dict(wallet)
        normalized.setdefault("ss58", ss58)
        normalized.setdefault("tier", "derived" if "wallet_ss58" in wallet else "seed")
        if normalized.get("tier") == "derived":
            promoted_priority.append(normalized)
        elif _priority_rank(normalized) == 0:
            high_priority_seeds.append(normalized)
        else:
            rotating_pool.append(normalized)

    high_priority_seeds.sort(key=lambda row: (-float(row.get("intel_score", 0.0)), row["ss58"]))
    promoted_priority.sort(key=lambda row: (-float(row.get("intel_score", 0.0)), row["ss58"]))
    rotating_pool.sort(key=lambda row: (_priority_rank(row), -float(row.get("intel_score", 0.0)), row["ss58"]))

    max_promoted = int(cfg.get("max_promoted_wallets_per_cycle", 3) or 3)
    max_rotating = int(cfg.get("max_rotating_wallets_per_cycle", 3) or 3)
    cursor = int(state.get("meta", {}).get("wallet_cursor", 0) or 0)
    if rotating_pool and max_rotating > 0:
        rotating_count = min(max_rotating, len(rotating_pool))
        start = cursor % len(rotating_pool)
        picked = []
        for offset in range(rotating_count):
            picked.append(rotating_pool[(start + offset) % len(rotating_pool)])
        next_cursor = (start + rotating_count) % len(rotating_pool)
    else:
        picked = []
        next_cursor = 0

    active.extend(high_priority_seeds)
    active.extend(promoted_priority[: max(max_promoted, 0)])
    active.extend(picked)
    return active, next_cursor


@dataclass
class WalletEvent:
    wallet_ss58: str
    netuid: int
    action: str
    amount_tao: float
    timestamp: datetime
    extrinsic_id: str
    delegate_ss58: Optional[str]
    delegate_name: Optional[str]

    @classmethod
    def from_api(cls, row: Dict) -> "WalletEvent":
        nominator = row.get("nominator") or {}
        delegate = row.get("delegate") or {}
        return cls(
            wallet_ss58=str(nominator.get("ss58", "")),
            netuid=int(row.get("netuid", -1)),
            action=str(row.get("action", "")).upper(),
            amount_tao=rao_to_tao(row.get("amount")),
            timestamp=iso_to_dt(str(row.get("timestamp"))),
            extrinsic_id=str(row.get("extrinsic_id", "")),
            delegate_ss58=delegate.get("ss58"),
            delegate_name=row.get("delegate_name"),
        )


def fetch_wallet_positions(wallet_ss58: str, timeout: float) -> List[Dict]:
    payload = fetch_taostats(
        "/api/dtao/stake_balance/latest/v1",
        [f"coldkey={wallet_ss58}"],
        timeout,
    )
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    positions = []
    for row in rows or []:
        positions.append(
            {
                "netuid": int(row.get("netuid", -1)),
                "balance_tao": rao_to_tao(row.get("balance_as_tao")),
                "alpha_balance": rao_to_tao(row.get("balance")),
                "subnet_rank": int(row.get("subnet_rank", 0) or 0),
                "hotkey_name": row.get("hotkey_name"),
                "hotkey_ss58": (row.get("hotkey") or {}).get("ss58"),
                "timestamp": row.get("timestamp"),
            }
        )
    positions.sort(key=lambda row: row.get("balance_tao", 0.0), reverse=True)
    return positions


def fetch_wallet_events(wallet_ss58: str, limit: int, timeout: float) -> List[WalletEvent]:
    payload = fetch_taostats(
        "/api/delegation/v1",
        [f"nominator={wallet_ss58}", "action=all", f"limit={limit}"],
        timeout,
    )
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    return [WalletEvent.from_api(row) for row in rows or []]


def fetch_subnet_events(netuid: int, limit: int, timeout: float) -> List[WalletEvent]:
    payload = fetch_taostats(
        "/api/delegation/v1",
        [f"netuid={netuid}", "action=all", f"limit={limit}"],
        timeout,
    )
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    return [WalletEvent.from_api(row) for row in rows or []]


def _wallet_event_window(
    subnet_events: Iterable[WalletEvent],
    wallet_ss58: str,
    netuid: int,
    center: datetime,
    minutes: int,
) -> List[WalletEvent]:
    span_seconds = minutes * 60
    selected = []
    for event in subnet_events:
        if event.wallet_ss58 != wallet_ss58 or event.netuid != netuid:
            continue
        delta = abs((event.timestamp - center).total_seconds())
        if delta <= span_seconds:
            selected.append(event)
    return selected


def is_probable_mev_wallet(
    subnet_events: Iterable[WalletEvent],
    candidate_event: WalletEvent,
    mev_cfg: Dict,
) -> bool:
    nearby = _wallet_event_window(
        subnet_events,
        candidate_event.wallet_ss58,
        candidate_event.netuid,
        candidate_event.timestamp,
        int(mev_cfg.get("burst_minutes", 60) or 60),
    )
    actions = {event.action for event in nearby}
    max_notional = max((event.amount_tao for event in nearby), default=0.0)

    roundtrip_events = _wallet_event_window(
        subnet_events,
        candidate_event.wallet_ss58,
        candidate_event.netuid,
        candidate_event.timestamp,
        int(mev_cfg.get("roundtrip_minutes", 20) or 20),
    )
    roundtrip_actions = {event.action for event in roundtrip_events}
    roundtrip_notional = max((event.amount_tao for event in roundtrip_events), default=0.0)

    if (
        {"DELEGATE", "UNDELEGATE"}.issubset(roundtrip_actions)
        and roundtrip_notional <= float(mev_cfg.get("max_notional_tao", 2.0) or 2.0)
    ):
        return True

    if (
        len(nearby) >= int(mev_cfg.get("burst_count", 4) or 4)
        and {"DELEGATE", "UNDELEGATE"}.issubset(actions)
        and max_notional <= float(mev_cfg.get("burst_max_notional_tao", 5.0) or 5.0)
    ):
        return True

    return False


def find_precursor_events(
    tracked_event: WalletEvent,
    subnet_events: Iterable[WalletEvent],
    cfg: Dict,
) -> List[Dict]:
    min_lead_seconds = int(cfg.get("precursor_min_lead_minutes", 5) or 5) * 60
    max_lead_seconds = int(cfg.get("precursor_max_lead_hours", 24) or 24) * 3600
    min_amount_tao = float(cfg.get("candidate_min_amount_tao", 1.0) or 1.0)
    mev_cfg = cfg.get("mev_filters", {})

    by_wallet: Dict[str, WalletEvent] = {}
    for event in subnet_events:
        if event.action != "DELEGATE":
            continue
        if event.netuid != tracked_event.netuid:
            continue
        if event.wallet_ss58 == tracked_event.wallet_ss58:
            continue
        if event.amount_tao < min_amount_tao:
            continue
        lead_seconds = (tracked_event.timestamp - event.timestamp).total_seconds()
        if lead_seconds < min_lead_seconds or lead_seconds > max_lead_seconds:
            continue
        existing = by_wallet.get(event.wallet_ss58)
        if existing is None or event.timestamp > existing.timestamp:
            by_wallet[event.wallet_ss58] = event

    precursors = []
    for candidate in by_wallet.values():
        lead_hours = (tracked_event.timestamp - candidate.timestamp).total_seconds() / 3600.0
        mev_like = is_probable_mev_wallet(subnet_events, candidate, mev_cfg)
        lead_score = 1.5 if lead_hours <= 6 else 1.0
        amount_score = min(candidate.amount_tao / 25.0, 3.0)
        score = 1.0 + lead_score + amount_score
        if mev_like:
            score *= 0.2
        precursors.append(
            {
                "wallet_ss58": candidate.wallet_ss58,
                "netuid": candidate.netuid,
                "tracked_wallet_ss58": tracked_event.wallet_ss58,
                "tracked_extrinsic_id": tracked_event.extrinsic_id,
                "candidate_extrinsic_id": candidate.extrinsic_id,
                "lead_hours": round(lead_hours, 3),
                "amount_tao": round(candidate.amount_tao, 6),
                "score": round(score, 4),
                "delegate_name": candidate.delegate_name,
                "timestamp": dt_to_iso(candidate.timestamp),
                "mev_like": mev_like,
            }
        )
    precursors.sort(key=lambda row: (-row["score"], row["lead_hours"]))
    return precursors


def merge_candidate_scores(existing: Dict, precursor_hits: Iterable[Dict]) -> Dict:
    next_candidates = json.loads(json.dumps(existing or {}))
    for hit in precursor_hits:
        wallet_ss58 = hit["wallet_ss58"]
        candidate = next_candidates.setdefault(
            wallet_ss58,
            {
                "wallet_ss58": wallet_ss58,
                "label": None,
                "tier": "derived",
                "intel_score": 0.0,
                "lead_count": 0,
                "seed_hits": {},
                "subnet_hits": {},
                "last_seen_at": None,
                "first_seen_at": hit["timestamp"],
                "mev_flags": 0,
                "sample_delegate_name": hit.get("delegate_name"),
                "seen_hit_ids": [],
            },
        )
        hit_id = _precursor_hit_id(hit)
        seen_hit_ids = candidate.setdefault("seen_hit_ids", [])
        if hit_id in seen_hit_ids:
            continue
        seen_hit_ids.append(hit_id)
        candidate["intel_score"] = round(float(candidate.get("intel_score", 0.0)) + float(hit["score"]), 4)
        candidate["lead_count"] = int(candidate.get("lead_count", 0)) + 1
        seed_hits = candidate.setdefault("seed_hits", {})
        seed_hits[hit["tracked_wallet_ss58"]] = int(seed_hits.get(hit["tracked_wallet_ss58"], 0)) + 1
        subnet_hits = candidate.setdefault("subnet_hits", {})
        subnet_key = str(hit["netuid"])
        subnet_hits[subnet_key] = int(subnet_hits.get(subnet_key, 0)) + 1
        candidate["last_seen_at"] = hit["timestamp"]
        if hit.get("mev_like"):
            candidate["mev_flags"] = int(candidate.get("mev_flags", 0)) + 1
    return next_candidates


def select_promoted_wallets(candidates: Dict, cfg: Dict) -> List[Dict]:
    promotion_cfg = cfg.get("promotion", {})
    min_score = float(promotion_cfg.get("min_score", 4.0) or 4.0)
    min_lead_count = int(promotion_cfg.get("min_lead_count", 2) or 2)
    max_dynamic = int(cfg.get("max_dynamic_wallets", 8) or 8)

    promoted = []
    for candidate in candidates.values():
        lead_count = int(candidate.get("lead_count", 0))
        mev_flags = int(candidate.get("mev_flags", 0))
        score = float(candidate.get("intel_score", 0.0))
        mev_ratio = (mev_flags / lead_count) if lead_count else 0.0
        if lead_count < min_lead_count or score < min_score or mev_ratio >= 0.5:
            continue
        promoted.append(candidate)

    promoted.sort(
        key=lambda row: (
            -float(row.get("intel_score", 0.0)),
            -int(row.get("lead_count", 0)),
            row.get("last_seen_at") or "",
        )
    )
    return promoted[:max_dynamic]


def recent_delegate_events(events: Iterable[WalletEvent], hours: float) -> List[WalletEvent]:
    cutoff = now_utc().timestamp() - (hours * 3600.0)
    selected = []
    for event in events:
        if event.action != "DELEGATE":
            continue
        if event.timestamp.timestamp() < cutoff:
            continue
        selected.append(event)
    return selected


def _precursor_hit_id(hit: Dict) -> str:
    return "|".join(
        [
            str(hit.get("wallet_ss58", "")),
            str(hit.get("tracked_wallet_ss58", "")),
            str(hit.get("netuid", "")),
            str(hit.get("tracked_extrinsic_id", "")),
            str(hit.get("candidate_extrinsic_id", "")),
        ]
    )


def render_report(
    cfg: Dict,
    wallet_snapshots: Dict[str, Dict],
    candidates: Dict,
    promoted: List[Dict],
    filtered_mev: List[Dict],
    generated_at: datetime,
) -> str:
    seeds = {wallet["ss58"]: wallet for wallet in cfg.get("wallets", [])}
    lines = [
        "# Wallet Trackers",
        "",
        f"Generated: `{dt_to_iso(generated_at)}`",
        "",
        "This is a live, read-only wallet-intelligence report for Falcon and Arbos.",
        "Use it as supplemental evidence only. Do not copy-trade blindly.",
        "",
        "## Active Watchlist",
        "",
    ]

    active = []
    for wallet in cfg.get("wallets", []):
        active.append(
            {
                "wallet_ss58": wallet["ss58"],
                "label": wallet.get("label"),
                "handle": wallet.get("handle"),
                "tier": wallet.get("tier", "seed"),
                "intel_score": float(wallet.get("intel_score", 0.0)),
            }
        )
    active.extend(promoted)
    active.sort(key=lambda row: (-float(row.get("intel_score", 0.0)), row.get("label") or row["wallet_ss58"]))

    for index, wallet in enumerate(active, start=1):
        ss58 = wallet["wallet_ss58"]
        snapshot = wallet_snapshots.get(ss58, {})
        positions = snapshot.get("positions", [])
        top_positions = ", ".join(
            f"sn{row['netuid']}:{row['balance_tao']:.2f}t" for row in positions[:4]
        ) or "no current positions captured"
        recent = snapshot.get("recent_events", [])
        latest_event = recent[0] if recent else None
        latest_text = (
            f"{latest_event['action']} sn{latest_event['netuid']} {latest_event['amount_tao']:.2f} TAO at {latest_event['timestamp']}"
            if latest_event
            else "no recent delegation events captured"
        )
        label = wallet.get("label") or seeds.get(ss58, {}).get("label") or ss58
        handle = wallet.get("handle") or seeds.get(ss58, {}).get("handle")
        tier = wallet.get("tier", "derived")
        lines.append(
            f"{index}. **{label}**"
            + (f" `{handle}`" if handle else "")
            + f" | tier=`{tier}` | intel_score=`{float(wallet.get('intel_score', 0.0)):.2f}`"
        )
        lines.append(f"   - ss58: `{ss58}`")
        lines.append(f"   - latest: {latest_text}")
        lines.append(f"   - top positions: {top_positions}")

    lines.extend(
        [
            "",
            "## Ranked Precursor Candidates",
            "",
        ]
    )
    ranked_candidates = sorted(
        candidates.values(),
        key=lambda row: (
            -float(row.get("intel_score", 0.0)),
            -int(row.get("lead_count", 0)),
            row.get("last_seen_at") or "",
        ),
    )
    if not ranked_candidates:
        lines.append("- No precursor wallets ranked yet.")
    else:
        for index, candidate in enumerate(ranked_candidates[:15], start=1):
            mev_ratio = int(candidate.get("mev_flags", 0)) / max(int(candidate.get("lead_count", 1)), 1)
            seeds_hit = ", ".join(
                f"{(seeds.get(ss58) or {}).get('label', ss58)}:{count}"
                for ss58, count in sorted(candidate.get("seed_hits", {}).items())
            ) or "none"
            subnets = ", ".join(
                f"sn{netuid}:{count}" for netuid, count in sorted(candidate.get("subnet_hits", {}).items())
            ) or "none"
            lines.append(
                f"{index}. `{candidate['wallet_ss58']}` | score=`{float(candidate.get('intel_score', 0.0)):.2f}` | "
                f"lead_count=`{int(candidate.get('lead_count', 0))}` | mev_ratio=`{mev_ratio:.2f}`"
            )
            lines.append(f"   - seed hits: {seeds_hit}")
            lines.append(f"   - subnet hits: {subnets}")
            lines.append(f"   - first seen: `{candidate.get('first_seen_at')}` | last seen: `{candidate.get('last_seen_at')}`")

    lines.extend(
        [
            "",
            "## Filtered Likely MEV / Noise",
            "",
        ]
    )
    if not filtered_mev:
        lines.append("- None currently filtered.")
    else:
        for wallet in filtered_mev[:10]:
            lines.append(
                f"- `{wallet['wallet_ss58']}` | score=`{float(wallet.get('intel_score', 0.0)):.2f}` | "
                f"lead_count=`{int(wallet.get('lead_count', 0))}` | mev_flags=`{int(wallet.get('mev_flags', 0))}`"
            )

    lines.extend(
        [
            "",
            "## Rules",
            "",
            "- Never transfer funds to another wallet.",
            "- Never auto-copy a watched wallet blindly.",
            "- Promote a precursor wallet only if it repeatedly leads meaningful moves and is not behaving like MEV noise.",
            "- Confirm wallet intel against TAO flow, liquidity, slippage, and Falcon's current book before acting.",
        ]
    )
    return "\n".join(lines) + "\n"


def refresh_tracker(
    config_path: Path,
    state_path: Path,
    report_output: Path,
    desktop_output: Optional[Path],
    timeout: float,
    force: bool,
) -> Dict:
    cfg = load_json(config_path, {})
    state = load_json(state_path, {"meta": {}, "wallet_snapshots": {}, "candidates": {}})
    meta = state.get("meta", {})
    if int(meta.get("state_schema_version", 0) or 0) < STATE_SCHEMA_VERSION:
        state = {
            "meta": {
                "state_schema_version": STATE_SCHEMA_VERSION,
                "wallet_cursor": int(meta.get("wallet_cursor", 0) or 0),
            },
            "wallet_snapshots": {},
            "candidates": {},
        }

    generated_at = now_utc()
    refresh_minutes = float(cfg.get("refresh_minutes", 60) or 60)
    last_refresh = state.get("meta", {}).get("last_refresh_at")
    if last_refresh and not force:
        elapsed_seconds = (generated_at - iso_to_dt(last_refresh)).total_seconds()
        if elapsed_seconds < (refresh_minutes * 60.0):
            report = state.get("report_markdown")
            if report:
                report_output.write_text(report, encoding="utf-8")
                if desktop_output:
                    desktop_output.parent.mkdir(parents=True, exist_ok=True)
                    desktop_output.write_text(report, encoding="utf-8")
            return state

    wallet_snapshots: Dict[str, Dict] = {}
    candidates = state.get("candidates", {})
    subnet_cache: Dict[int, List[WalletEvent]] = {}
    tracked_events_for_analysis: List[WalletEvent] = []

    os.environ["WALLET_TRACKER_FETCH_RETRY_ATTEMPTS"] = str(
        int(cfg.get("fetch_retry_attempts", 4) or 4)
    )
    os.environ["WALLET_TRACKER_FETCH_RETRY_BASE_SECONDS"] = str(
        float(cfg.get("fetch_retry_base_seconds", 15) or 15)
    )

    seeds = cfg.get("wallets", [])
    promoted = select_promoted_wallets(candidates, cfg)
    active_wallets, next_wallet_cursor = select_active_wallets(seeds, promoted, state, cfg)

    for wallet in active_wallets:
        wallet_ss58 = wallet["ss58"] if "ss58" in wallet else wallet["wallet_ss58"]
        positions = fetch_wallet_positions(wallet_ss58, timeout)
        events = fetch_wallet_events(wallet_ss58, int(cfg.get("tracked_event_limit", 24) or 24), timeout)
        wallet_snapshots[wallet_ss58] = {
            "label": wallet.get("label"),
            "handle": wallet.get("handle"),
            "tier": wallet.get("tier", "derived"),
            "positions": positions,
            "recent_events": [
                {
                    "action": event.action,
                    "netuid": event.netuid,
                    "amount_tao": round(event.amount_tao, 6),
                    "timestamp": dt_to_iso(event.timestamp),
                    "extrinsic_id": event.extrinsic_id,
                }
                for event in events[:10]
            ],
        }
        tracked_events_for_analysis.extend(
            recent_delegate_events(events, float(cfg.get("tracked_event_lookback_hours", 24) or 24))
        )

    precursor_hits = []
    for tracked_event in tracked_events_for_analysis:
        if tracked_event.wallet_ss58 in set(cfg.get("ignored_wallets", [])):
            continue
        subnet_events = subnet_cache.get(tracked_event.netuid)
        if subnet_events is None:
            subnet_events = fetch_subnet_events(
                tracked_event.netuid,
                int(cfg.get("subnet_event_limit", 120) or 120),
                timeout,
            )
            subnet_cache[tracked_event.netuid] = subnet_events
        precursor_hits.extend(find_precursor_events(tracked_event, subnet_events, cfg))

    candidates = merge_candidate_scores(candidates, precursor_hits)
    promoted = select_promoted_wallets(candidates, cfg)

    filtered_mev = []
    for candidate in candidates.values():
        lead_count = int(candidate.get("lead_count", 0))
        mev_flags = int(candidate.get("mev_flags", 0))
        if lead_count and (mev_flags / lead_count) >= 0.5:
            filtered_mev.append(candidate)
    filtered_mev.sort(key=lambda row: (-int(row.get("mev_flags", 0)), -float(row.get("intel_score", 0.0))))

    report = render_report(cfg, wallet_snapshots, candidates, promoted, filtered_mev, generated_at)
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(report, encoding="utf-8")
    if desktop_output:
        desktop_output.parent.mkdir(parents=True, exist_ok=True)
        desktop_output.write_text(report, encoding="utf-8")

    state = {
        "meta": {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "last_refresh_at": dt_to_iso(generated_at),
            "tracked_wallet_count": len(active_wallets),
            "candidate_count": len(candidates),
            "promoted_count": len(promoted),
            "wallet_cursor": next_wallet_cursor,
        },
        "wallet_snapshots": wallet_snapshots,
        "candidates": candidates,
        "report_markdown": report,
    }
    write_json(state_path, state)
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the Arbos wallet tracker report.")
    parser.add_argument("command", nargs="?", default="refresh", choices=["refresh"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--report-output", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--desktop-output", default=str(DEFAULT_DESKTOP_REPORT_PATH))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-desktop-output", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    desktop_output = None if args.no_desktop_output else Path(args.desktop_output)
    refresh_tracker(
        config_path=Path(args.config),
        state_path=Path(args.state),
        report_output=Path(args.report_output),
        desktop_output=desktop_output,
        timeout=args.timeout,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
