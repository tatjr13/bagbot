#!/usr/bin/env python3
"""Mirror the current public NOVA validator checks for preflight screening."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from rdkit import Chem
from rdkit.Chem import Descriptors
import yaml

from nova_archive_mirror import DEFAULT_CACHE_DIR, load_cached_inchikeys


DEFAULT_CONFIG_PATH = Path("/root/work/nova/config/config.yaml")
DEFAULT_ALLOWED_REACTIONS = ("rxn:1", "rxn:2", "rxn:3", "rxn:4", "rxn:5")


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class CandidateParity:
    name: str
    ok: bool
    reason: str
    detail: str
    smiles: str | None
    inchikey: str | None
    heavy_atoms: int | None
    rotatable_bonds: int | None
    boltz_safe: bool | None
    boltz_warning: str | None


@dataclass
class SubmissionParity:
    ok: bool
    reason: str
    detail: str
    checked_at_utc: str
    allowed_reactions: list[str]
    accepted_names: list[str]
    results: list[CandidateParity]


def load_public_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Unexpected config format in {config_path}")
    merged: dict[str, object] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            merged.update(value)
        else:
            merged[key] = value
    return merged


def resolve_smiles(name: str) -> str | None:
    if not name:
        return None
    cleaned = name.replace("'", "").replace('"', "")
    if not cleaned.startswith("rxn:"):
        return None
    from combinatorial_db.reactions import get_smiles_from_reaction

    return get_smiles_from_reaction(cleaned)


def is_reaction_allowed(name: str, allowed_reactions: Sequence[str]) -> bool:
    return any(name.startswith(prefix + ":") or name == prefix for prefix in allowed_reactions)


def is_boltz_safe(smiles: str) -> tuple[bool, str | None]:
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False, "RDKit failed to parse SMILES"
        mol = Chem.AddHs(mol)
        canonical_order = Chem.CanonicalRankAtoms(mol)
        for atom, can_idx in zip(mol.GetAtoms(), canonical_order):
            atom_name = atom.GetSymbol().upper() + str(can_idx + 1)
            if len(atom_name) > 4:
                return False, f"Atom name would exceed 4 chars: {atom_name}"
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, f"Boltz safety check failed: {exc}"


def check_candidate(
    name: str,
    *,
    config: dict,
    archive_inchikeys: set[str] | None = None,
    allowed_reactions: Sequence[str] = DEFAULT_ALLOWED_REACTIONS,
) -> CandidateParity:
    if not is_reaction_allowed(name, allowed_reactions):
        return CandidateParity(name, False, "reaction_window", f"Candidate is outside the allowed reactions: {', '.join(allowed_reactions)}.", None, None, None, None, None, None)

    smiles = resolve_smiles(name)
    if not smiles:
        return CandidateParity(name, False, "smiles_resolution", "Could not derive SMILES from the reaction tuple.", None, None, None, None, None, None)

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return CandidateParity(name, False, "rdkit_parse", "RDKit could not parse the derived SMILES.", smiles, None, None, None, None, None)

    heavy_atoms = int(mol.GetNumHeavyAtoms())
    if heavy_atoms < int(config["min_heavy_atoms"]):
        return CandidateParity(name, False, "heavy_atoms", f"Heavy atom count {heavy_atoms} is below {config['min_heavy_atoms']}.", smiles, None, heavy_atoms, None, None, None)

    present_banned = [atom.GetSymbol() for atom in mol.GetAtoms() if atom.GetSymbol() in set(config["banned_atom_types"])]
    if present_banned:
        return CandidateParity(name, False, "banned_atom", f"Contains banned atom types: {sorted(set(present_banned))}.", smiles, None, heavy_atoms, None, None, None)

    rotatable_bonds = int(Descriptors.NumRotatableBonds(mol))
    if rotatable_bonds < int(config["min_rotatable_bonds"]) or rotatable_bonds > int(config["max_rotatable_bonds"]):
        return CandidateParity(
            name,
            False,
            "rotatable_bonds",
            f"Rotatable bonds {rotatable_bonds} outside [{config['min_rotatable_bonds']}, {config['max_rotatable_bonds']}].",
            smiles,
            None,
            heavy_atoms,
            rotatable_bonds,
            None,
            None,
        )

    inchikey = Chem.MolToInchiKey(mol)
    if archive_inchikeys and inchikey in archive_inchikeys:
        return CandidateParity(name, False, "archive_duplicate", "InChIKey already exists in the mirrored submission archive.", smiles, inchikey, heavy_atoms, rotatable_bonds, None, None)

    boltz_safe, boltz_warning = is_boltz_safe(smiles)
    return CandidateParity(name, True, "ok", "Candidate passes the mirrored public validity checks.", smiles, inchikey, heavy_atoms, rotatable_bonds, boltz_safe, boltz_warning)


def check_submission(
    names: Sequence[str],
    *,
    config: dict,
    archive_inchikeys: set[str] | None = None,
    allowed_reactions: Sequence[str] = DEFAULT_ALLOWED_REACTIONS,
) -> SubmissionParity:
    if len(names) != len(set(names)):
        results = [
            CandidateParity(name, False, "duplicate_name", "Submission contains duplicate names; validator would clear the whole submission.", None, None, None, None, None, None)
            for name in names
        ]
        return SubmissionParity(False, "duplicate_name", "Duplicate names invalidate the full submission.", iso_now(), list(allowed_reactions), [], results)

    results: list[CandidateParity] = []
    accepted_names: list[str] = []
    seen_inchikeys: dict[str, str] = {}

    for name in names:
        result = check_candidate(name, config=config, archive_inchikeys=archive_inchikeys, allowed_reactions=allowed_reactions)
        results.append(result)
        if not result.ok:
            return SubmissionParity(False, result.reason, result.detail, iso_now(), list(allowed_reactions), [], results)
        if result.inchikey:
            prior_name = seen_inchikeys.get(result.inchikey)
            if prior_name is not None:
                detail = f"Chemically identical molecules detected: {prior_name} and {name} share {result.inchikey}."
                duplicate_results = [
                    CandidateParity(item.name, False, "chemical_duplicate", detail, item.smiles, item.inchikey, item.heavy_atoms, item.rotatable_bonds, item.boltz_safe, item.boltz_warning)
                    for item in results
                ]
                return SubmissionParity(False, "chemical_duplicate", detail, iso_now(), list(allowed_reactions), [], duplicate_results)
            seen_inchikeys[result.inchikey] = name
        accepted_names.append(name)

    return SubmissionParity(True, "ok", "Submission passes mirrored public validity checks.", iso_now(), list(allowed_reactions), accepted_names, results)


def latest_valid_submission_block(epoch_end_block: int, no_submission_blocks: int) -> int:
    return int(epoch_end_block) - int(no_submission_blocks) - 1


def render_markdown(summary: SubmissionParity) -> str:
    lines = [
        "# Nova Validator Parity",
        "",
        f"- Checked at: `{summary.checked_at_utc}`",
        f"- Overall: `{'pass' if summary.ok else 'fail'}`",
        f"- Reason: `{summary.reason}`",
        f"- Detail: `{summary.detail}`",
        f"- Allowed reactions: `{', '.join(summary.allowed_reactions)}`",
        "",
        "## Candidate Results",
    ]
    for result in summary.results:
        lines.extend(
            [
                f"### {result.name}",
                f"- ok: `{result.ok}`",
                f"- reason: `{result.reason}`",
                f"- detail: `{result.detail}`",
                f"- smiles: `{result.smiles}`",
                f"- inchikey: `{result.inchikey}`",
                f"- heavy_atoms: `{result.heavy_atoms}`",
                f"- rotatable_bonds: `{result.rotatable_bonds}`",
                f"- boltz_safe: `{result.boltz_safe}`",
                f"- boltz_warning: `{result.boltz_warning}`",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror NOVA validator parity checks for candidate preflight")
    parser.add_argument("--candidate", action="append", dest="candidates", required=True)
    parser.add_argument("--protein", default="Q01959")
    parser.add_argument("--config-path", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--json-out")
    parser.add_argument("--md-out")
    args = parser.parse_args()

    config = load_public_config(Path(args.config_path))
    archive_inchikeys = load_cached_inchikeys(Path(args.cache_dir), args.protein)
    summary = check_submission(args.candidates, config=config, archive_inchikeys=archive_inchikeys)
    payload = asdict(summary)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.md_out:
        Path(args.md_out).write_text(render_markdown(summary), encoding="utf-8")

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
