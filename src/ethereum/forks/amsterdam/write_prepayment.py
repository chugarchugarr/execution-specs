"""Experimental transaction-boundary storage-write prepayments."""

from typing import Iterable

from ethereum_types.bytes import Bytes32
from ethereum_types.numeric import Uint

from ethereum.crypto.hash import keccak256
from ethereum.state import Address

from .fork_types import ExecutionGas

WRITE_PREPAYMENT_DOMAIN = b"glamsterdam-write-prepayment-v1"
WRITE_PREPAYMENT_MARKER_COUNT = 3

# EIP-8038 raises the existing-slot write component from 2,800 to 10,000.
# A voucher moves only that 7,200 repricing delta to transaction scope.
STORAGE_WRITE_REPRICING_DELTA = ExecutionGas(Uint(7_200))


def write_prepayment_markers(
    address: Address, key: Bytes32
) -> tuple[Bytes32, ...]:
    """Return the provisional access-list markers for one storage write."""
    prefix = WRITE_PREPAYMENT_DOMAIN + bytes(address) + bytes(key)
    return tuple(
        keccak256(prefix + bytes([index]))
        for index in range(WRITE_PREPAYMENT_MARKER_COUNT)
    )


def find_write_prepayments(
    entries: Iterable[tuple[Address, Bytes32]],
) -> set[tuple[Address, Bytes32]]:
    """Decode complete provisional vouchers from access-list entries."""
    declared = set(entries)
    prepaid: set[tuple[Address, Bytes32]] = set()
    for address, key in declared:
        markers = write_prepayment_markers(address, key)
        if all((address, marker) in declared for marker in markers):
            prepaid.add((address, key))
    return prepaid
