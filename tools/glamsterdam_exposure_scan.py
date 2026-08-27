#!/usr/bin/env python3
"""Conservative static scanner for EIP-8038 fixed-gas call exposure.

Input is newline-delimited JSON. Each record must contain:

    {"address": "0x...", "bytecode": "0x..."}

Optional fields are copied through when useful. ``value_usd`` may be supplied by
an external valuation source; this tool never invents or fetches valuation data.

The scanner deliberately separates three claims:

1. a caller contains a statically recoverable fixed-gas CALL-family site;
2. that gas constant lies in the isolated warm existing-slot SSTORE repricing
   window (Osaka 2,900 <= gas < Amsterdam 10,100);
3. a statically resolved callee is present in the same dataset and contains an
   SSTORE opcode outside PUSH data.

Only (3) is labelled ``static_high``. Even that is a candidate, not proof that
an on-chain execution reaches the SSTORE with the measured remaining gas.
Trace replay is the next verification boundary.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

OSAKA_WARM_EXISTING_WRITE = 2_900
AMSTERDAM_WARM_EXISTING_WRITE = 10_100
UINT256_MASK = (1 << 256) - 1
UNKNOWN = object()


@dataclass(frozen=True)
class Instruction:
    pc: int
    opcode: int
    immediate: bytes = b""


@dataclass(frozen=True)
class CallSite:
    caller: str
    pc: int
    opcode: str
    gas: int
    target: str | None
    value: int | None
    window: str
    callee_has_sstore: bool | None
    classification: str
    callee_value_usd: float | None


CALLS = {
    0xF1: ("CALL", 7),
    0xF2: ("CALLCODE", 7),
    0xF4: ("DELEGATECALL", 6),
}

# STATICCALL cannot execute SSTORE and is intentionally excluded.


def normalize_address(value: str) -> str:
    raw = value.lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) > 40:
        raise ValueError(f"address longer than 20 bytes: {value}")
    int(raw or "0", 16)
    return "0x" + raw.rjust(40, "0")


def decode_hex(value: str) -> bytes:
    raw = value[2:] if value.startswith("0x") else value
    if len(raw) % 2:
        raw = "0" + raw
    return bytes.fromhex(raw)


def disassemble(code: bytes) -> list[Instruction]:
    out: list[Instruction] = []
    pc = 0
    while pc < len(code):
        opcode = code[pc]
        start = pc
        pc += 1
        if 0x60 <= opcode <= 0x7F:
            size = opcode - 0x5F
            immediate = code[pc : pc + size]
            pc += size
            out.append(Instruction(start, opcode, immediate))
        else:
            out.append(Instruction(start, opcode))
    return out


def contains_sstore(code: bytes) -> bool:
    return any(ins.opcode == 0x55 for ins in disassemble(code))


def pop(stack: list[Any]) -> Any:
    return stack.pop() if stack else UNKNOWN


def push_unknown(stack: list[Any], count: int = 1) -> None:
    stack.extend([UNKNOWN] * count)


def const_binary(stack: list[Any], fn: Any) -> None:
    a = pop(stack)
    b = pop(stack)
    if isinstance(a, int) and isinstance(b, int):
        try:
            stack.append(fn(a, b) & UINT256_MASK)
        except (ZeroDivisionError, ValueError, OverflowError, MemoryError):
            stack.append(UNKNOWN)
    else:
        stack.append(UNKNOWN)


def evm_shl(shift: int, value: int) -> int:
    """EIP-145 SHL semantics without allocating enormous Python integers."""
    if shift >= 256:
        return 0
    return (value << shift) & UINT256_MASK


def evm_shr(shift: int, value: int) -> int:
    """EIP-145 SHR semantics."""
    if shift >= 256:
        return 0
    return value >> shift


def stack_effect(opcode: int) -> tuple[int, int] | None:
    # Stack effects for opcodes commonly encountered between a PUSH constant
    # and CALL. Unknown opcodes invalidate only their outputs rather than being
    # misread as constants.
    table: dict[int, tuple[int, int]] = {
        0x00: (0, 0), 0x01: (2, 1), 0x02: (2, 1), 0x03: (2, 1),
        0x04: (2, 1), 0x05: (2, 1), 0x06: (2, 1), 0x07: (2, 1),
        0x08: (3, 1), 0x09: (3, 1), 0x0A: (2, 1), 0x0B: (2, 1),
        0x10: (2, 1), 0x11: (2, 1), 0x12: (2, 1), 0x13: (2, 1),
        0x14: (2, 1), 0x15: (1, 1), 0x16: (2, 1), 0x17: (2, 1),
        0x18: (2, 1), 0x19: (1, 1), 0x1A: (2, 1), 0x1B: (2, 1),
        0x1C: (2, 1), 0x1D: (2, 1), 0x20: (2, 1),
        0x30: (0, 1), 0x31: (1, 1), 0x32: (0, 1), 0x33: (0, 1),
        0x34: (0, 1), 0x35: (1, 1), 0x36: (0, 1), 0x37: (3, 0),
        0x38: (0, 1), 0x39: (3, 0), 0x3A: (0, 1), 0x3B: (1, 1),
        0x3C: (4, 0), 0x3D: (0, 1), 0x3E: (3, 0), 0x3F: (1, 1),
        0x40: (1, 1), 0x41: (0, 1), 0x42: (0, 1), 0x43: (0, 1),
        0x44: (0, 1), 0x45: (0, 1), 0x46: (0, 1), 0x47: (0, 1),
        0x48: (0, 1), 0x49: (1, 1), 0x4A: (0, 1),
        0x50: (1, 0), 0x51: (1, 1), 0x52: (2, 0), 0x53: (2, 0),
        0x54: (1, 1), 0x55: (2, 0), 0x56: (1, 0), 0x57: (2, 0),
        0x58: (0, 1), 0x59: (0, 1), 0x5A: (0, 1), 0x5B: (0, 0),
        0x5C: (1, 1), 0x5D: (2, 0), 0x5E: (3, 0), 0x5F: (0, 1),
        0xF0: (3, 1), 0xF3: (2, 0), 0xF5: (4, 1), 0xFA: (6, 1),
        0xFD: (2, 0), 0xFE: (0, 0), 0xFF: (1, 0),
    }
    return table.get(opcode)


def _constant_address(value: Any) -> str | None:
    if not isinstance(value, int) or value < 0 or value >= 1 << 160:
        return None
    return normalize_address(hex(value))


def _window(gas: int) -> str:
    if gas < OSAKA_WARM_EXISTING_WRITE:
        return "below_osaka_floor"
    if gas < AMSTERDAM_WARM_EXISTING_WRITE:
        return "repricing_window"
    return "above_direct_window"


def recover_fixed_calls(address: str, code: bytes) -> list[dict[str, Any]]:
    """Recover fixed-gas CALL-family sites within conservative basic blocks."""
    stack: list[Any] = []
    calls: list[dict[str, Any]] = []

    for ins in disassemble(code):
        opcode = ins.opcode

        # A JUMPDEST may have multiple predecessors. Clearing the abstract
        # stack avoids inventing constants across unknown control-flow joins.
        if opcode == 0x5B:
            stack.clear()
            continue

        if opcode == 0x5F:  # PUSH0
            stack.append(0)
            continue
        if 0x60 <= opcode <= 0x7F:
            stack.append(int.from_bytes(ins.immediate, "big"))
            continue
        if 0x80 <= opcode <= 0x8F:  # DUP1..DUP16
            depth = opcode - 0x7F
            stack.append(stack[-depth] if len(stack) >= depth else UNKNOWN)
            continue
        if 0x90 <= opcode <= 0x9F:  # SWAP1..SWAP16
            depth = opcode - 0x8F
            if len(stack) > depth:
                stack[-1], stack[-1 - depth] = stack[-1 - depth], stack[-1]
            else:
                stack.clear()
            continue

        if opcode in CALLS:
            name, pops = CALLS[opcode]
            gas = stack[-1] if stack else UNKNOWN
            target_value = stack[-2] if len(stack) >= 2 else UNKNOWN
            call_value = (
                stack[-3]
                if opcode in (0xF1, 0xF2) and len(stack) >= 3
                else None
            )
            if isinstance(gas, int):
                calls.append(
                    {
                        "caller": address,
                        "pc": ins.pc,
                        "opcode": name,
                        "gas": gas,
                        "target": _constant_address(target_value),
                        "value": call_value if isinstance(call_value, int) else None,
                    }
                )
            for _ in range(pops):
                pop(stack)
            stack.append(UNKNOWN)
            continue

        if opcode == 0x01:
            const_binary(stack, lambda a, b: b + a)
            continue
        if opcode == 0x02:
            const_binary(stack, lambda a, b: b * a)
            continue
        if opcode == 0x03:
            const_binary(stack, lambda a, b: b - a)
            continue
        if opcode == 0x04:
            const_binary(stack, lambda a, b: 0 if a == 0 else b // a)
            continue
        if opcode == 0x16:
            const_binary(stack, lambda a, b: b & a)
            continue
        if opcode == 0x17:
            const_binary(stack, lambda a, b: b | a)
            continue
        if opcode == 0x18:
            const_binary(stack, lambda a, b: b ^ a)
            continue
        if opcode == 0x1B:
            const_binary(stack, evm_shl)
            continue
        if opcode == 0x1C:
            const_binary(stack, evm_shr)
            continue

        effect = stack_effect(opcode)
        if effect is None:
            # Unknown semantics make the current abstract stack unsafe.
            stack.clear()
            continue
        pops, pushes = effect
        for _ in range(pops):
            pop(stack)
        push_unknown(stack, pushes)

        if opcode in (0x00, 0x56, 0xF3, 0xFD, 0xFE, 0xFF):
            stack.clear()

    return calls


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                record["address"] = normalize_address(record["address"])
                record["_code"] = decode_hex(record["bytecode"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid record on line {line_no}: {exc}") from exc
            records.append(record)
    return records


def classify(records: Iterable[dict[str, Any]]) -> list[CallSite]:
    rows = list(records)
    by_address = {row["address"]: row for row in rows}
    sites: list[CallSite] = []

    for row in rows:
        for raw in recover_fixed_calls(row["address"], row["_code"]):
            window = _window(raw["gas"])
            target = raw["target"]
            target_record = by_address.get(target) if target else None
            has_sstore = (
                contains_sstore(target_record["_code"])
                if target_record is not None
                else None
            )

            if window == "repricing_window" and has_sstore is True:
                classification = "static_high"
            elif window == "repricing_window" and target_record is None:
                classification = "direct_window_unresolved"
            elif window == "repricing_window":
                classification = "direct_window_no_sstore_seen"
            else:
                classification = "fixed_gas_candidate"

            value_usd: float | None = None
            if target_record is not None and target_record.get("value_usd") is not None:
                value_usd = float(target_record["value_usd"])

            sites.append(
                CallSite(
                    caller=raw["caller"],
                    pc=raw["pc"],
                    opcode=raw["opcode"],
                    gas=raw["gas"],
                    target=target,
                    value=raw["value"],
                    window=window,
                    callee_has_sstore=has_sstore,
                    classification=classification,
                    callee_value_usd=value_usd,
                )
            )
    return sites


def summarize(records: list[dict[str, Any]], sites: list[CallSite]) -> dict[str, Any]:
    high_targets: dict[str, float] = {}
    for site in sites:
        if (
            site.classification == "static_high"
            and site.target is not None
            and site.callee_value_usd is not None
        ):
            # Count each resolved callee at most once. ``value_usd`` is supplied
            # by the dataset and is reported, not independently verified here.
            high_targets[site.target] = site.callee_value_usd

    return {
        "contracts_scanned": len(records),
        "fixed_call_sites": len(sites),
        "repricing_window_sites": sum(
            site.window == "repricing_window" for site in sites
        ),
        "static_high_sites": sum(
            site.classification == "static_high" for site in sites
        ),
        "unresolved_direct_window_sites": sum(
            site.classification == "direct_window_unresolved" for site in sites
        ),
        "valued_static_high_callees": len(high_targets),
        "reported_static_high_value_usd": sum(high_targets.values()),
        "claim_boundary": (
            "Static candidates only. Do not call reported value 'at risk' until "
            "the corresponding call path is replayed under Osaka and Amsterdam."
        ),
    }


def write_sites(path: Path, sites: list[CallSite]) -> None:
    fields = [field.name for field in CallSite.__dataclass_fields__.values()]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for site in sites:
            writer.writerow(asdict(site))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="NDJSON address/bytecode dataset")
    parser.add_argument("--sites", type=Path, help="optional CSV call-site output")
    args = parser.parse_args()

    records = load_records(args.input)
    sites = classify(records)
    if args.sites:
        write_sites(args.sites, sites)
    print(json.dumps(summarize(records, sites), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
