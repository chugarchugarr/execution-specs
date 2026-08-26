"""
Executable phase-1 proof for Glamsterdam storage-write prepayment.

This test is intentionally smaller than a consensus change. It pins the
current EELS Osaka and Amsterdam gas constants and proves the two invariants
that must survive before a transaction encoding or EVM implementation is
worth adding:

1. identical fixed child gas: Osaka PASS -> Amsterdam FAIL -> prepaid PASS;
2. the prepaid path preserves the full Amsterdam execution-resource charge.

If either invariant fails as the Amsterdam branch changes, this prototype
must stop rather than silently adapt its claim.
"""

from enum import Enum

from ethereum.forks.amsterdam.vm import gas as amsterdam_gas
from ethereum.forks.osaka.vm import gas as osaka_gas


# Osaka's warm first-change write component. Osaka charges a cold existing
# non-zero -> non-zero first change as COLD_STORAGE_WRITE=5_000 total:
# 2_100 cold access + 2_900 write. Removing the 100 warm-access component
# leaves the historical write component that a prewarmed child frame paid.
LEGACY_STORAGE_WRITE = 2_800

# PUSH(value) + PUSH(key) around SSTORE. Both forks price these two PUSHes at
# VERY_LOW=3 each on the branch pinned by this test.
SSTORE_STACK_SETUP = 6

# Exactly enough for the minimal cold Osaka existing-slot overwrite.
FIXED_CHILD_GAS = 5_006


class Outcome(Enum):
    """Bounded result for the fixed-gas child execution."""

    PASS = "PASS"
    FAIL = "FAIL"


def _run_fixed_child(
    *, access_cost: int, write_cost: int
) -> tuple[Outcome, int]:
    """Evaluate the minimal SSTORE path against the immutable child budget."""
    required = SSTORE_STACK_SETUP + access_cost + write_cost
    outcome = Outcome.PASS if required <= FIXED_CHILD_GAS else Outcome.FAIL
    return outcome, required


def test_osaka_pass_amsterdam_fail_prepaid_pass() -> None:
    """Pin the liveness flip and proposed rescue under one fixed budget."""
    osaka_write_component = int(osaka_gas.GasCosts.COLD_STORAGE_WRITE) - int(
        osaka_gas.GasCosts.COLD_STORAGE_ACCESS
    )

    osaka, osaka_required = _run_fixed_child(
        access_cost=int(osaka_gas.GasCosts.COLD_STORAGE_ACCESS),
        write_cost=osaka_write_component,
    )
    amsterdam, amsterdam_required = _run_fixed_child(
        access_cost=int(amsterdam_gas.GasCosts.COLD_STORAGE_ACCESS),
        write_cost=int(amsterdam_gas.GasCosts.STORAGE_WRITE),
    )
    prepaid, prepaid_required = _run_fixed_child(
        access_cost=int(amsterdam_gas.GasCosts.WARM_ACCESS),
        write_cost=LEGACY_STORAGE_WRITE,
    )

    assert osaka_write_component == 2_900
    assert osaka_required == 5_006
    assert amsterdam_required == 12_106
    assert prepaid_required == 2_906
    assert (osaka, amsterdam, prepaid) == (
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.PASS,
    )


def test_prepayment_preserves_glamsterdam_execution_resource_charge() -> None:
    """Move repricing out of the child without discounting the work."""
    write_repricing_delta = (
        int(amsterdam_gas.GasCosts.STORAGE_WRITE) - LEGACY_STORAGE_WRITE
    )

    # Ordinary Amsterdam path: the child first touches a cold account and a
    # cold existing storage slot, then performs the first non-zero overwrite.
    baseline_call_access = int(amsterdam_gas.GasCosts.COLD_ACCOUNT_ACCESS)
    baseline_storage = int(
        amsterdam_gas.GasCosts.COLD_STORAGE_ACCESS
    ) + int(amsterdam_gas.GasCosts.STORAGE_WRITE)
    baseline_core_charge = baseline_call_access + baseline_storage

    # Proposed accounting split:
    # - EIP-2930-style prepayment makes account and slot accesses warm;
    # - the 7_200 write-repricing delta is paid outside the immutable child;
    # - the child pays only warm access + the 2_800 historical write component.
    prepaid_call_access = int(
        amsterdam_gas.GasCosts.TX_ACCESS_LIST_ADDRESS
    ) + int(amsterdam_gas.GasCosts.WARM_ACCESS)
    prepaid_storage = (
        int(amsterdam_gas.GasCosts.TX_ACCESS_LIST_STORAGE_KEY)
        + int(amsterdam_gas.GasCosts.WARM_ACCESS)
        + write_repricing_delta
        + LEGACY_STORAGE_WRITE
    )
    prepaid_core_charge = prepaid_call_access + prepaid_storage

    assert write_repricing_delta == 7_200
    assert baseline_call_access == 3_000
    assert prepaid_call_access == baseline_call_access
    assert baseline_storage == 12_100
    assert prepaid_storage == baseline_storage
    assert prepaid_core_charge == baseline_core_charge == 15_100


def test_prepayment_is_not_a_subsidy() -> None:
    """Prove lower local cost does not make global work cheaper."""
    local_prepaid_sstore = (
        int(amsterdam_gas.GasCosts.WARM_ACCESS) + LEGACY_STORAGE_WRITE
    )
    globally_paid_sstore = (
        int(amsterdam_gas.GasCosts.TX_ACCESS_LIST_STORAGE_KEY)
        + local_prepaid_sstore
        + int(amsterdam_gas.GasCosts.STORAGE_WRITE)
        - LEGACY_STORAGE_WRITE
    )

    assert local_prepaid_sstore == 2_900
    assert globally_paid_sstore == (
        int(amsterdam_gas.GasCosts.COLD_STORAGE_ACCESS)
        + int(amsterdam_gas.GasCosts.STORAGE_WRITE)
    )
    assert local_prepaid_sstore < FIXED_CHILD_GAS
