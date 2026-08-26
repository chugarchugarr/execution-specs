"""Tests that EIP-8198 duration changes are schedule-driven."""

from ethereum_types.numeric import U64, Uint

from ethereum.forks.amsterdam.slot_timing import (
    BASE_SLOT_DURATION_MS,
    BlobScheduleParameters,
    SlotDurationEntry,
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

    # Equal response per millisecond: 1200/12s == 1000/10s == 800/8s.
    assert response_12s * Uint(10000) == response_10s * Uint(12000)
    assert response_10s * Uint(8000) == response_8s * Uint(10000)


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
