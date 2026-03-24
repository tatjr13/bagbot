#!/usr/bin/env python3
"""Thin Nova miner entrypoint with bounded Arbos-side runtime fixes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run

from dotenv import load_dotenv


NOVA_ROOT = Path(__file__).resolve().parent.parent

if str(NOVA_ROOT) not in sys.path:
    sys.path.insert(0, str(NOVA_ROOT))

from neurons import miner as nova_miner  # noqa: E402


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def runtime_contract(config) -> dict[str, object]:
    git_branch = "unknown"
    git_commit = "unknown"
    try:
        git_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=NOVA_ROOT, capture_output=True, text=True, check=False).stdout.strip() or git_branch
        git_commit = run(["git", "rev-parse", "--short", "HEAD"], cwd=NOVA_ROOT, capture_output=True, text=True, check=False).stdout.strip() or git_commit
    except Exception:  # noqa: BLE001
        pass
    submit_mode = os.getenv("NOVA_SUBMIT_MODE", "shadow").strip().lower() or "shadow"
    contract = {
        "timestamp_utc": iso_now(),
        "git_branch": git_branch,
        "git_commit": git_commit,
        "wallet_name": getattr(config.wallet, "name", None),
        "wallet_hotkey": getattr(config.wallet, "hotkey", None),
        "network": getattr(config.subtensor, "network", None),
        "netuid": getattr(config, "netuid", None),
        "submit_mode": submit_mode,
        "wrapper_patch": "entropy_weight seed + optional submit kill-switch",
    }
    log_dir = Path(getattr(config, "full_path", NOVA_ROOT / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "nova-runtime-contract.json").write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return contract


async def main() -> None:
    load_dotenv(override=True)

    config = nova_miner.parse_arguments()

    # Upstream miner reads config.entropy_weight even though config only
    # exposes entropy_start_weight. Keep the runtime moving by seeding the
    # missing field from the configured starting weight.
    if getattr(config, "entropy_weight", None) is None:
        config.entropy_weight = float(getattr(config, "entropy_start_weight", 0.0))

    nova_miner.setup_logging(config)
    contract = runtime_contract(config)
    submit_mode = str(contract["submit_mode"])
    nova_miner.bt.logging.info(f"Stable runtime contract: {contract}")
    if submit_mode != "live":
        async def shadow_submit(state) -> None:
            candidate_product = state.get("candidate_product")
            nova_miner.bt.logging.warning(
                f"Shadow submit mode active; skipping live chain/GitHub submission for candidate={candidate_product}"
            )
        nova_miner.submit_response = shadow_submit
    # --- Patch data source: surrogate-guided combinatorial DB ---
    from surrogate_guided_source import generate_surrogate_guided_chunks

    db_path = str(NOVA_ROOT / "combinatorial_db" / "molecules.sqlite")
    # Only pass weekly_target if explicitly set; otherwise let the function
    # resolve from config.yaml → env var (avoids hardcoded Q01959 default)
    _weekly_target = os.environ.get("NOVA_WEEKLY_TARGET")  # None if unset
    combo_gen = generate_surrogate_guided_chunks(
        db_path, chunk_size=128, weekly_target=_weekly_target,
        sample_size=50_000, top_k_surrogate=500, surrogate_enabled=True,
    )

    def patched_stream(*args, **kwargs):
        return combo_gen

    nova_miner.stream_random_chunk_from_dataset = patched_stream
    nova_miner.bt.logging.warning(f"Molecule source patched: surrogate-guided (target={_weekly_target or 'from-config'})")

    # --- Patch submission ordering: smallest molecule first for Boltz2 ---
    # Boltz2 score = (affinity_prob - affinity_pred) / heavy_atom_count
    # Only the FIRST molecule gets Boltz2-scored (sample_selection: "first")
    # So we reorder top-10 to put the smallest (fewest heavy atoms) first.
    from nova_validator_parity import resolve_smiles as _resolve_smiles
    from rdkit import Chem as _Chem

    _original_submit = nova_miner.submit_response

    async def boltz_reordered_submit(state) -> None:
        candidate = state.get("candidate_product", "")
        if candidate:
            names = candidate.split(",")
            if len(names) > 1:
                def _heavy_count(name: str) -> int:
                    smi = _resolve_smiles(name.strip())
                    if not smi:
                        return 999
                    mol = _Chem.MolFromSmiles(smi)
                    return mol.GetNumHeavyAtoms() if mol else 999
                names.sort(key=_heavy_count)
                state["candidate_product"] = ",".join(names)
                nova_miner.bt.logging.warning(
                    f"Boltz2 reorder: first={names[0]} ({_heavy_count(names[0])} HA)"
                )
        return await _original_submit(state)

    nova_miner.submit_response = boltz_reordered_submit
    nova_miner.bt.logging.warning("Submission reorder patched: smallest molecule first for Boltz2")

    await nova_miner.run_miner(config)


if __name__ == "__main__":
    asyncio.run(main())
