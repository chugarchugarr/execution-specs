#!/usr/bin/env python3
"""Adapt the EIP-8198 schedule-driven proof onto current Amsterdam."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FORK = Path("src/ethereum/forks/amsterdam/fork.py")
GAS = Path("src/ethereum/forks/amsterdam/vm/gas.py")
ENV = Path("src/ethereum/forks/amsterdam/vm/instructions/environment.py")


def resolve_reference_conflict() -> None:
    s = FORK.read_text()
    start = s.index("<<<<<<< ")
    end_marker = s.index(">>>>>>> ", start)
    end = s.index("\n", end_marker) + 1
    block = s[start:end]
    if "SYSTEM_TRANSACTION_GAS" not in block or "beacon roots ring buffer" not in block:
        raise RuntimeError("unexpected EIP-8198 reference conflict")
    new = "\n".join(
        [
            '"""',
            "Address of the beacon roots ring buffer contract. Its 8191-entry buffer",
            "holds one root per slot, so its wall-clock coverage shrinks from ~27.3",
            "hours to ~22.8 hours under 10-second slots. The buffer length is part of",
            "the deployed contract and is deliberately not changed by this fork.",
            '"""',
            "SYSTEM_TRANSACTION_GAS = ExecutionGas(Uint(30000000))",
        ]
    ) + "\n"
    FORK.write_text(s[:start] + new + s[end:])


def git_show(ref: str, path: str) -> str:
    return subprocess.check_output(["git", "show", f"{ref}:{path}"], text=True)


def restore_final_added_files(old_head: str) -> None:
    files = [
        "src/ethereum/forks/amsterdam/slot_timing.py",
        "tests/evm_tools/eip8198_quick_slots/__init__.py",
        "tests/evm_tools/eip8198_quick_slots/test_slot_timing.py",
        "tests/evm_tools/eip8198_quick_slots/test_vm_blob_schedule.py",
        "tests/evm_tools/eip8198_quick_slots/test_additional_duration_era.py",
    ]
    for name in files:
        p = Path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(git_show(old_head, name))


def replace_one(pattern: str, replacement: str, source: str, label: str) -> str:
    out, n = re.subn(pattern, replacement, source, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{label}: expected one replacement, got {n}")
    return out


def adapt_fork() -> None:
    s = FORK.read_text()
    anchor = "from .state_tracker import (\n"
    slot_import = """from .slot_timing import (
    BASE_SLOT_DURATION_MS,
    BLOB_GAS_PER_BLOB,
    SLOT_DURATION_SCHEDULE,
    SlotDurationSchedule,
    get_blob_schedule,
    get_slot_duration_ms,
    get_transition_durations,
    scale_transition_limit,
)
"""
    if slot_import not in s:
        if anchor not in s:
            raise RuntimeError("fork import anchor missing")
        s = s.replace(anchor, slot_import + anchor, 1)

    s = replace_one(
        r'SLOT_DURATION_MS = Uint\(10000\)\n""".*?PREVIOUS_SLOT_DURATION_MS = Uint\(12000\)\n"""\nDuration of a consensus-layer slot before this fork, in milliseconds\.\n"""\n',
        "",
        s,
        "remove one-off duration constants",
    )

    new_base_fee = '''def get_max_blob_gas_per_block(
    slot_number: U64,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> U64:
    """Return blob capacity for the slot-duration era."""
    return (
        BLOB_GAS_PER_BLOB
        * get_blob_schedule(slot_number, slot_duration_schedule).maximum
    )


def calculate_base_fee_per_gas(
    block_gas_limit: Uint,
    parent_gas_limit: Uint,
    parent_gas_used: Uint,
    parent_base_fee_per_gas: Uint,
    gas_limit_reference: Optional[Uint] = None,
    slot_duration_ms: Optional[Uint] = None,
) -> Uint:
    """Calculate the base fee while preserving wall-clock response."""
    if gas_limit_reference is None:
        gas_limit_reference = parent_gas_limit
    if slot_duration_ms is None:
        slot_duration_ms = get_slot_duration_ms(U64(0))
    parent_gas_target = parent_gas_limit // ELASTICITY_MULTIPLIER
    if not check_gas_limit(block_gas_limit, gas_limit_reference):
        raise InvalidBlock

    if parent_gas_used == parent_gas_target:
        expected_base_fee_per_gas = parent_base_fee_per_gas
    elif parent_gas_used > parent_gas_target:
        gas_used_delta = parent_gas_used - parent_gas_target
        parent_fee_gas_delta = parent_base_fee_per_gas * gas_used_delta
        target_fee_gas_delta = parent_fee_gas_delta // parent_gas_target
        base_fee_per_gas_delta = max(
            target_fee_gas_delta
            * slot_duration_ms
            // (BASE_SLOT_DURATION_MS * BASE_FEE_MAX_CHANGE_DENOMINATOR),
            Uint(1),
        )
        expected_base_fee_per_gas = parent_base_fee_per_gas + base_fee_per_gas_delta
    else:
        gas_used_delta = parent_gas_target - parent_gas_used
        parent_fee_gas_delta = parent_base_fee_per_gas * gas_used_delta
        target_fee_gas_delta = parent_fee_gas_delta // parent_gas_target
        base_fee_per_gas_delta = (
            target_fee_gas_delta
            * slot_duration_ms
            // (BASE_SLOT_DURATION_MS * BASE_FEE_MAX_CHANGE_DENOMINATOR)
        )
        expected_base_fee_per_gas = parent_base_fee_per_gas - base_fee_per_gas_delta

    return Uint(expected_base_fee_per_gas)


'''
    s = replace_one(
        r"def calculate_base_fee_per_gas\(.*?(?=def validate_header\()",
        new_base_fee,
        s,
        "base fee",
    )

    new_validate = '''def validate_header(
    parent_header: Header | PreviousHeader,
    header: Header,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> None:
    """Verify a block header against its parent."""
    if header.number < Uint(1):
        raise InvalidBlock

    excess_blob_gas = calculate_excess_blob_gas(
        parent_header,
        header.slot_number,
        slot_duration_schedule,
    )
    if header.excess_blob_gas != excess_blob_gas:
        raise InvalidBlock

    if header.gas_used > header.gas_limit:
        raise InvalidBlock

    parent_slot_number: Optional[U64]
    if isinstance(parent_header, Header):
        parent_slot_number = parent_header.slot_number
    else:
        parent_slot_number = None

    old_duration_ms, new_duration_ms = get_transition_durations(
        parent_slot_number,
        header.slot_number,
        slot_duration_schedule,
    )
    gas_limit_reference = scale_transition_limit(
        parent_header.gas_limit,
        old_duration_ms,
        new_duration_ms,
    )
    expected_base_fee_per_gas = calculate_base_fee_per_gas(
        header.gas_limit,
        parent_header.gas_limit,
        parent_header.gas_used,
        parent_header.base_fee_per_gas,
        gas_limit_reference=gas_limit_reference,
        slot_duration_ms=new_duration_ms,
    )
    if expected_base_fee_per_gas != header.base_fee_per_gas:
        raise InvalidBlock
    if header.timestamp <= parent_header.timestamp:
        raise InvalidBlock
    if header.number != parent_header.number + Uint(1):
        raise InvalidBlock
    if len(header.extra_data) > 32:
        raise InvalidBlock
    if header.difficulty != 0:
        raise InvalidBlock
    if header.nonce != b"\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00":
        raise InvalidBlock
    if header.ommers_hash != EMPTY_OMMER_HASH:
        raise InvalidBlock

    block_parent_hash = keccak256(rlp.encode(parent_header))
    if header.parent_hash != block_parent_hash:
        raise InvalidBlock


'''
    s = replace_one(
        r"def validate_header\(.*?(?=def check_transaction\()",
        new_validate,
        s,
        "validate header",
    )

    old = '''        check_max_fee_per_blob_gas(
            tx.blob_versioned_hashes,
            tx.max_fee_per_blob_gas,
            block_env.excess_blob_gas,
        )'''
    new = '''        check_max_fee_per_blob_gas(
            tx.blob_versioned_hashes,
            tx.max_fee_per_blob_gas,
            block_env.excess_blob_gas,
            block_env.slot_number,
        )'''
    if old not in s:
        raise RuntimeError("current blob fee helper call missing")
    s = s.replace(old, new, 1)

    old = "        blob_gas_fee = calculate_data_fee(block_env.excess_blob_gas, tx)"
    new = '''        blob_gas_fee = calculate_data_fee(
            block_env.excess_blob_gas,
            tx,
            block_env.slot_number,
        )'''
    if old not in s:
        raise RuntimeError("current data fee call missing")
    s = s.replace(old, new, 1)
    FORK.write_text(s)


def adapt_gas() -> None:
    s = GAS.read_text()
    s = s.replace(
        "from ethereum.utils.numeric import ceil32, taylor_exponential\n",
        "from ethereum.utils.numeric import ceil32\n",
        1,
    )
    anchor = "from ..transactions import (\n"
    slot_import = """from ..slot_timing import (
    SLOT_DURATION_SCHEDULE,
    SlotDurationSchedule,
    calculate_blob_gas_price_for_slot,
    get_blob_schedule,
)
"""
    if slot_import not in s:
        if anchor not in s:
            raise RuntimeError("gas import anchor missing")
        s = s.replace(anchor, slot_import + anchor, 1)

    marker = "if TYPE_CHECKING:\n    from . import BlockEnvironment, BlockOutput, Evm\n\n\n"
    if marker not in s:
        raise RuntimeError("TYPE_CHECKING marker missing")
    s = s.replace(marker, marker + "_INITIAL_SLOT = U64(0)\n\n\n", 1)

    new_excess = '''def calculate_excess_blob_gas(
    parent_header: Header | PreviousHeader,
    current_slot_number: U64 = _INITIAL_SLOT,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> U64:
    """Calculate excess blob gas using the current slot-duration era."""
    excess_blob_gas = U64(0)
    blob_gas_used = U64(0)
    base_fee_per_gas = Uint(0)

    if isinstance(parent_header, (Header, PreviousHeader)):
        excess_blob_gas = parent_header.excess_blob_gas
        blob_gas_used = parent_header.blob_gas_used
        base_fee_per_gas = parent_header.base_fee_per_gas

    blob_schedule = get_blob_schedule(current_slot_number, slot_duration_schedule)
    target_blob_gas_per_block = GasCosts.PER_BLOB * blob_schedule.target
    parent_blob_gas = excess_blob_gas + blob_gas_used
    if parent_blob_gas < target_blob_gas_per_block:
        return U64(0)

    target_blob_gas_price = Uint(GasCosts.PER_BLOB)
    target_blob_gas_price *= calculate_blob_gas_price(
        excess_blob_gas,
        current_slot_number,
        slot_duration_schedule,
    )

    base_blob_tx_price = GasCosts.BLOB_BASE_COST * base_fee_per_gas
    if base_blob_tx_price > target_blob_gas_price:
        blob_schedule_delta = blob_schedule.maximum - blob_schedule.target
        return U64(
            excess_blob_gas
            + blob_gas_used * blob_schedule_delta // blob_schedule.maximum
        )

    return U64(parent_blob_gas - target_blob_gas_per_block)


'''
    s = replace_one(
        r"def calculate_excess_blob_gas\(.*?(?=def calculate_total_blob_gas\()",
        new_excess,
        s,
        "excess blob gas",
    )

    new_price = '''def calculate_blob_gas_price(
    excess_blob_gas: U64,
    slot_number: U64 = _INITIAL_SLOT,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> Uint:
    """Calculate the blob gas price for the supplied duration era."""
    return calculate_blob_gas_price_for_slot(
        excess_blob_gas,
        slot_number,
        slot_duration_schedule,
    )


'''
    s = replace_one(
        r"def calculate_blob_gas_price\(.*?(?=def calculate_data_fee\()",
        new_price,
        s,
        "blob gas price",
    )

    new_data = '''def calculate_data_fee(
    excess_blob_gas: U64,
    tx: Transaction,
    slot_number: U64 = _INITIAL_SLOT,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> Uint:
    """Calculate the blob data fee for the supplied duration era."""
    return Uint(calculate_total_blob_gas(tx)) * calculate_blob_gas_price(
        excess_blob_gas,
        slot_number,
        slot_duration_schedule,
    )


'''
    s = replace_one(
        r"def calculate_data_fee\(.*?(?=def check_max_fee_per_blob_gas\()",
        new_data,
        s,
        "data fee",
    )

    new_check_fee = '''def check_max_fee_per_blob_gas(
    blob_versioned_hashes: Tuple[VersionedHash, ...],
    max_fee_per_blob_gas: U256,
    excess_blob_gas: U64,
    slot_number: U64 = _INITIAL_SLOT,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> None:
    """Check that a blob transaction covers the active-era blob price."""
    if not blob_versioned_hashes:
        return

    blob_gas_price = calculate_blob_gas_price(
        excess_blob_gas,
        slot_number,
        slot_duration_schedule,
    )
    if Uint(max_fee_per_blob_gas) < blob_gas_price:
        raise InsufficientMaxFeePerBlobGasError(
            "insufficient max fee per blob gas"
        )


'''
    s = replace_one(
        r"def check_max_fee_per_blob_gas\(.*?(?=def check_block_gas_capacity\()",
        new_check_fee,
        s,
        "max blob fee",
    )

    old = "    blob_gas_available = MAX_BLOB_GAS_PER_BLOCK - block_output.blob_gas_used"
    new = '''    blob_gas_available = (
        GasCosts.PER_BLOB * get_blob_schedule(block_env.slot_number).maximum
        - block_output.blob_gas_used
    )'''
    if old not in s:
        raise RuntimeError("current blob-capacity line missing")
    s = s.replace(old, new, 1)
    GAS.write_text(s)


def adapt_environment() -> None:
    s = ENV.read_text()
    old = "    blob_base_fee = calculate_blob_gas_price(evm.block_env.excess_blob_gas)"
    new = '''    blob_base_fee = calculate_blob_gas_price(
        evm.block_env.excess_blob_gas,
        evm.block_env.slot_number,
    )'''
    if old not in s:
        raise RuntimeError("current BLOBBASEFEE call missing")
    ENV.write_text(s.replace(old, new, 1))


def adapt(old_head: str) -> None:
    restore_final_added_files(old_head)
    adapt_fork()
    adapt_gas()
    adapt_environment()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--resolve-reference-conflict":
        resolve_reference_conflict()
    elif len(sys.argv) == 2:
        adapt(sys.argv[1])
    else:
        raise SystemExit("usage: eip8198_upstream_adapt.py OLD_HEAD | --resolve-reference-conflict")
