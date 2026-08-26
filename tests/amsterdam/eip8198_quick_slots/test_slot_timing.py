"""Tests that EIP-8198 duration changes are schedule-driven."""

import inspect

from ethereum_types.numeric import U64, Uint

from ethereum.forks.amsterdam import fork as amsterdam_fork
from ethereum.forks.amsterdam.fork import (
    calculate_base_fee_per_gas,
    get_max_blob_gas_per_block,
)
from ethereum.forks.amsterdam.slot_timing import (
    BASE_SLOT_DURATION_MS,
    BLOB_GAS_PER_BLOB,
    BlobScheduleParameters,
    SlotDurationEntry,
    calculate_blob_gas_price_for_slot,
    get_blob_schedule,
    get_slot_duration_ms,
    get_transition_durations,
    scale_blob_schedule,
    scale_transition_limit,
    scale_wall_clock_response,
)

HEGOTA_EPOCH = U64(10)
FUTURE_TEST_EPOCH = U64(20)
SCHEDULE_12_10_8 = (
    SlotDurationEntry(HEGOTA_EPOCH, Uint(10000)),
    SlotDurationEntry(FUTURE_TEST_EPOCH, Uint(8000)),
)


def test_repeated_duration_changes_are_schedule_only() -> None:
    """A synthetic 10 -> 8 era uses the same lookup as 12 -> 10."""
    last_12s_slot = U64(HEGOTA_EPOCH * U64(32) - U64(1))
    first_10s_slot = U64(HEGOTA_EPOCH * U64(32))
    last_10s_slot = U64(FUTURE_TEST_EPOCH * U64(32) - U64(1))
    first_8s_slot = U64(FUTURE_TEST_EPOCH * U64(32))

    assert get_slot_duration_ms(last_12s_slot, SCHEDULE_12_10_8) == Uint(12000)
    assert get_slot_duration_ms(first_10s_slot, SCHEDULE_12_10_8) == Uint(10000)
    assert get_slot_duration_ms(last_10s_slot, SCHEDULE_12_10_8) == Uint(10000)
    assert get_slot_duration_ms(first_8s_slot, SCHEDULE_12_10_8) == Uint(8000)


def test_gas_limit_scales_once_at_each_duration_boundary() -> None:
    """Gas/sec is preserved at both 12 -> 10 and 10 -> 8 transitions."""
    first_10s_slot = U64(HEGOTA_EPOCH * U64(32))
    first_8s_slot = U64(FUTURE_TEST_EPOCH * U64(32))
    last_10s_payload_slot = U64(first_8s_slot - U64(7))

    old_ms, new_ms = get_transition_durations(
        None, first_10s_slot, SCHEDULE_12_10_8
    )
    gas_limit_10s = scale_transition_limit(Uint(72_000_000), old_ms, new_ms)
    assert (old_ms, new_ms) == (Uint(12000), Uint(10000))
    assert gas_limit_10s == Uint(60_000_000)

    # The current payload is already seven slots into the 8-second era.
    # Comparing execution-payload slots still detects the one-time change.
    old_ms, new_ms = get_transition_durations(
        last_10s_payload_slot,
        U64(first_8s_slot + U64(7)),
        SCHEDULE_12_10_8,
    )
    gas_limit_8s = scale_transition_limit(gas_limit_10s, old_ms, new_ms)
    assert (old_ms, new_ms) == (Uint(10000), Uint(8000))
    assert gas_limit_8s == Uint(48_000_000)

    # Once both execution payloads are in the 8-second era, no second scale
    # is applied even when there were missed beacon slots between payloads.
    old_ms, new_ms = get_transition_durations(
        U64(first_8s_slot + U64(7)),
        U64(first_8s_slot + U64(19)),
        SCHEDULE_12_10_8,
    )
    assert scale_transition_limit(gas_limit_8s, old_ms, new_ms) == gas_limit_8s


def test_base_fee_response_uses_current_over_base_ratio() -> None:
    """Ongoing response/sec remains constant across more than one era."""
    unscaled_delta = Uint(1200)

    response_12s = scale_wall_clock_response(
        unscaled_delta, BASE_SLOT_DURATION_MS
    )
    response_10s = scale_wall_clock_response(unscaled_delta, Uint(10000))
    response_8s = scale_wall_clock_response(unscaled_delta, Uint(8000))

    assert response_12s == Uint(1200)
    assert response_10s == Uint(1000)
    assert response_8s == Uint(800)
    assert response_12s * Uint(10000) == response_10s * Uint(12000)
    assert response_10s * Uint(8000) == response_8s * Uint(10000)


def test_production_base_fee_path_supports_second_era() -> None:
    """Amsterdam's real base-fee calculator remains wall-clock invariant."""
    common = dict(
        block_gas_limit=Uint(60_000_000),
        parent_gas_limit=Uint(60_000_000),
        parent_gas_used=Uint(60_000_000),
        parent_base_fee_per_gas=Uint(960),
        gas_limit_reference=Uint(60_000_000),
    )

    fee_10s = calculate_base_fee_per_gas(
        **common,
        slot_duration_ms=Uint(10000),
    )
    fee_8s = calculate_base_fee_per_gas(
        **common,
        slot_duration_ms=Uint(8000),
    )

    assert fee_10s == Uint(1060)
    assert fee_8s == Uint(1040)


def test_blob_schedule_derives_repeated_eras_from_same_transition() -> None:
    """Blob throughput and fee response derive through 12 -> 10 -> 8."""
    blob_12s = BlobScheduleParameters(
        maximum=U64(21),
        target=U64(14),
        update_fraction=Uint(11_684_671),
    )

    blob_10s = scale_blob_schedule(blob_12s, Uint(12000), Uint(10000))
    assert blob_10s == BlobScheduleParameters(
        maximum=U64(17),
        target=U64(12),
        update_fraction=Uint(10_015_432),
    )

    blob_8s = scale_blob_schedule(blob_10s, Uint(10000), Uint(8000))
    assert blob_8s == BlobScheduleParameters(
        maximum=U64(13),
        target=U64(10),
        update_fraction=Uint(7_511_574),
    )


def test_production_blob_paths_follow_same_schedule() -> None:
    """Capacity and BLOBBASEFEE inputs both follow the synthetic 8s era."""
    first_10s_slot = U64(HEGOTA_EPOCH * U64(32))
    first_8s_slot = U64(FUTURE_TEST_EPOCH * U64(32))

    blob_10s = get_blob_schedule(first_10s_slot, SCHEDULE_12_10_8)
    blob_8s = get_blob_schedule(first_8s_slot, SCHEDULE_12_10_8)
    assert blob_10s.maximum == U64(17)
    assert blob_8s.maximum == U64(13)

    assert get_max_blob_gas_per_block(
        first_10s_slot, SCHEDULE_12_10_8
    ) == BLOB_GAS_PER_BLOB * U64(17)
    assert get_max_blob_gas_per_block(
        first_8s_slot, SCHEDULE_12_10_8
    ) == BLOB_GAS_PER_BLOB * U64(13)

    excess = U64(20_000_000)
    fee_10s = calculate_blob_gas_price_for_slot(
        excess, first_10s_slot, SCHEDULE_12_10_8
    )
    fee_8s = calculate_blob_gas_price_for_slot(
        excess, first_8s_slot, SCHEDULE_12_10_8
    )
    assert fee_8s != fee_10s


def test_amsterdam_has_no_12_to_10_transition_special_case() -> None:
    """The production header path contains no previous-duration constant."""
    source = inspect.getsource(amsterdam_fork)
    assert "PREVIOUS_SLOT_DURATION_MS" not in source
    assert "SLOT_DURATION_MS = Uint(10000)" not in source
    assert "get_transition_durations" in source
    assert "scale_transition_limit" in source
