"""Prove VM blob helpers remain slot-duration agnostic."""

import inspect

from ethereum_types.numeric import U64, Uint

from ethereum.forks.amsterdam.slot_timing import (
    SlotDurationEntry,
    calculate_blob_gas_price_for_slot,
)
from ethereum.forks.amsterdam.vm import gas as vm_gas

FUTURE_TEST_EPOCH = U64(20)
SCHEDULE_10_8 = (
    SlotDurationEntry(U64(0), Uint(10000)),
    SlotDurationEntry(FUTURE_TEST_EPOCH, Uint(8000)),
)


def test_vm_blob_price_uses_slot_schedule_for_future_era() -> None:
    """The VM compatibility helper follows the same 10 -> 8 schedule."""
    last_10s_slot = U64(FUTURE_TEST_EPOCH * U64(32) - U64(1))
    first_8s_slot = U64(FUTURE_TEST_EPOCH * U64(32))
    excess_blob_gas = U64(20_000_000)

    vm_10s = vm_gas.calculate_blob_gas_price(
        excess_blob_gas,
        last_10s_slot,
        SCHEDULE_10_8,
    )
    vm_8s = vm_gas.calculate_blob_gas_price(
        excess_blob_gas,
        first_8s_slot,
        SCHEDULE_10_8,
    )

    assert vm_10s == calculate_blob_gas_price_for_slot(
        excess_blob_gas,
        last_10s_slot,
        SCHEDULE_10_8,
    )
    assert vm_8s == calculate_blob_gas_price_for_slot(
        excess_blob_gas,
        first_8s_slot,
        SCHEDULE_10_8,
    )
    assert vm_8s != vm_10s


def test_vm_blob_protocol_calculators_do_not_use_initial_snapshot() -> None:
    """Initial-era compatibility constants cannot govern later eras."""
    excess_source = inspect.getsource(vm_gas.calculate_excess_blob_gas)
    price_source = inspect.getsource(vm_gas.calculate_blob_gas_price)

    assert "get_blob_schedule" in excess_source
    assert "BLOB_SCHEDULE_TARGET" not in excess_source
    assert "BLOB_SCHEDULE_MAX" not in excess_source
    assert "calculate_blob_gas_price_for_slot" in price_source
    assert "BLOB_BASE_FEE_UPDATE_FRACTION" not in price_source
