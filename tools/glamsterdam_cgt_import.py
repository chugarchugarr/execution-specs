#!/usr/bin/env python3
"""Convert the pinned CGT corpus into Glamsterdam scanner NDJSON.

CGT's ``consolidated.csv`` contains one row per assessment, so the same
on-chain contract may appear many times. This importer deduplicates by chain
address, requires a consistent runtime fingerprint for repeated rows, and
reads runtime code from ``runtime/<fp_runtime>.rt.hex``.

The output contains no valuation claim. It is only an address/runtime corpus
for static fixed-gas analysis.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

from tools.glamsterdam_exposure_scan import normalize_address


def _pick(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name, "").strip()
        if value:
            return value
    return ""


def _is_mainnet(chain: str) -> bool:
    normalized = chain.strip().lower()
    return normalized in {"", "1", "eth", "ethereum", "main", "mainnet"}


def load_cgt_records(
    consolidated_csv: Path,
    runtime_dir: Path,
    *,
    mainnet_only: bool = True,
) -> list[dict[str, str]]:
    """Return deduplicated CGT address/runtime records."""
    by_address: dict[str, dict[str, str]] = {}

    with consolidated_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        if not reader.fieldnames:
            raise ValueError("CGT consolidated.csv has no header")

        for row_no, row in enumerate(reader, 2):
            chain = _pick(row, "chain")
            if mainnet_only and not _is_mainnet(chain):
                continue

            address_raw = _pick(row, "addr", "address")
            runtime_hash = _pick(row, "fp_runtime", "runtime_hash")
            if not address_raw or not runtime_hash:
                continue

            try:
                address = normalize_address(address_raw)
            except ValueError as exc:
                raise ValueError(f"invalid CGT address on row {row_no}: {exc}") from exc

            runtime_path = runtime_dir / f"{runtime_hash}.rt.hex"
            if not runtime_path.is_file():
                # CGT permits missing artefacts for some source rows. Missing
                # runtime cannot support a bytecode claim, so omit it.
                continue

            bytecode = runtime_path.read_text(encoding="utf-8").strip()
            if not bytecode:
                continue
            if not bytecode.startswith("0x"):
                bytecode = "0x" + bytecode
            try:
                bytes.fromhex(bytecode[2:])
            except ValueError as exc:
                raise ValueError(
                    f"invalid runtime hex for {runtime_hash} on row {row_no}"
                ) from exc

            record = {
                "address": address,
                "bytecode": bytecode,
                "runtime_hash": runtime_hash,
                "chain": chain or "main",
                "source": "CGT",
            }

            previous = by_address.get(address)
            if previous is None:
                by_address[address] = record
            elif previous["runtime_hash"] != runtime_hash:
                raise ValueError(
                    "CGT maps one address to conflicting runtime fingerprints: "
                    f"{address} -> {previous['runtime_hash']} / {runtime_hash}"
                )

    return [by_address[address] for address in sorted(by_address)]


def write_ndjson(records: Iterable[dict[str, str]], output: Path) -> None:
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--consolidated", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--all-chains",
        action="store_true",
        help="include rows whose CGT chain field is not Ethereum mainnet",
    )
    args = parser.parse_args()

    records = load_cgt_records(
        args.consolidated,
        args.runtime_dir,
        mainnet_only=not args.all_chains,
    )
    write_ndjson(records, args.out)
    print(json.dumps({"records": len(records), "output": str(args.out)}))


if __name__ == "__main__":
    main()
