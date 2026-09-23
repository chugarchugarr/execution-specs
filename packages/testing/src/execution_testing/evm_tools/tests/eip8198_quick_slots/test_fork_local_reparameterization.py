"""Try to falsify fork-local EIP-8198 re-parameterisation.

The schedule-driven implementation is the behavioral oracle. The competitor
contains no runtime duration schedule: each synthetic fork has only fixed EL
constants. If fixed constants cannot reproduce an invariant, this suite should
expose the first counterexample.
"""

from fractions import Fraction
import inspect

import pytest
from ethereum_types.numeric import U64, Uint

from ethereum.forks.amsterdam.fork import calculate_base_fee_per_gas
from ethereum.forks.amsterdam.slot_timing import (
    BASE_BLOB_SCHEDULE,
    BASE_SLOT_DURATION_MS,
    BlobScheduleParameters,
    SlotDurationEntry,
    calculate_blob_gas_price_for_slot,
    get_blob_schedule,
    scale_blob_schedule,
    scale_transition_limit,
)

from . import fork_local_competitor as local

SLOTS_PER_EPOCH = U64(32)
EPOCH_10S = U64(10)
EPOCH_8S = U64(15)
EPOCH_6S = U64(20)

ORACLE_SCHEDULE = (
    SlotDurationEntry(EPOCH_10S, Uint(10_000)),
    SlotDurationEntry(EPOCH_8S, Uint(8_000)),
    SlotDurationEntry(EPOCH_6S, Uint(6_000)),
)

ERA_CASES = (
    (12_000, U64(0), local.BASELINE_12S),
    (10_000, U64(EPOCH_10S * SLOTS_PER_EPOCH), local.FORK_10S),
    (8_000, U64(EPOCH_8S * SLOTS_PER_EPOCH), local.FORK_8S),
    (6_000, U64(EPOCH_6S * SLOTS_PER_EPOCH), local.FORK_6S),
)


def _blob_parameters(
    parameters: local.ForkLocalELParameters,
) -> BlobScheduleParameters:
    return BlobScheduleParameters(
        maximum=parameters.blob_maximum,
        target=parameters.blob_target,
        update_fraction=parameters.blob_update_fraction,
    )


def _rate_error(value: int, period_ms: int, baseline: Fraction) -> Fraction:
    return abs(Fraction(value, period_ms) - baseline)


def test_competitor_has_no_runtime_schedule_dependency() -> None:
    """The competing runtime model contains no schedule/slot/epoch machinery."""
    fields = tuple(local.ForkLocalELParameters.__dataclass_fields__)
    assert fields == (
        "gas_limit_target",
        "base_fee_numerator",
        "base_fee_denominator",
        "blob_maximum",
        "blob_target",
        "blob_update_fraction",
    )

    source = inspect.getsource(local)
    for forbidden in (
        "SLOT_DURATION_SCHEDULE",
        "SlotDurationEntry",
        "get_slot_duration_ms",
        "slot_number",
        "current_epoch",
    ):
        assert forbidden not in source


@pytest.mark.parametrize("duration_ms,_slot,parameters", ERA_CASES)
@pytest.mark.parametrize("parent_base_fee", [1, 7, 960, 1_000_000_000])
@pytest.mark.parametrize("gas_fraction", [0, 1, 2, 3, 4])
def test_fork_local_base_fee_matches_schedule_oracle(
    duration_ms: int,
    _slot: U64,
    parameters: local.ForkLocalELParameters,
    parent_base_fee: int,
    gas_fraction: int,
) -> None:
    """Fixed fork constants reproduce the oracle base-fee response exactly."""
    gas_limit = parameters.gas_limit_target
    parent_gas_target = gas_limit // Uint(2)
    parent_gas_used = Uint(parent_gas_target * Uint(gas_fraction) // Uint(2))

    oracle = calculate_base_fee_per_gas(
        block_gas_limit=gas_limit,
        parent_gas_limit=gas_limit,
        parent_gas_used=parent_gas_used,
        parent_base_fee_per_gas=Uint(parent_base_fee),
        gas_limit_reference=gas_limit,
        slot_duration_ms=Uint(duration_ms),
    )
    competitor = local.calculate_fork_local_base_fee(
        parent_gas_limit=gas_limit,
        parent_gas_used=parent_gas_used,
        parent_base_fee_per_gas=Uint(parent_base_fee),
        parameters=parameters,
    )
    assert competitor == oracle


def test_fork_local_gas_targets_match_transition_oracle() -> None:
    """Pre-coordinated fork targets compose without runtime duration state."""
    gas_12 = local.BASELINE_12S.gas_limit_target
    gas_10 = scale_transition_limit(gas_12, Uint(12_000), Uint(10_000))
    gas_8 = scale_transition_limit(gas_10, Uint(10_000), Uint(8_000))
    gas_6 = scale_transition_limit(gas_8, Uint(8_000), Uint(6_000))

    assert gas_10 == local.FORK_10S.gas_limit_target
    assert gas_8 == local.FORK_8S.gas_limit_target
    assert gas_6 == local.FORK_6S.gas_limit_target


@pytest.mark.parametrize("_duration_ms,slot,parameters", ERA_CASES)
def test_fork_local_blob_constants_can_match_schedule_oracle(
    _duration_ms: int,
    slot: U64,
    parameters: local.ForkLocalELParameters,
) -> None:
    """A fork can carry the same blob constants without runtime schedule lookup."""
    oracle = get_blob_schedule(slot, ORACLE_SCHEDULE)
    assert _blob_parameters(parameters) == oracle


@pytest.mark.parametrize("_duration_ms,slot,parameters", ERA_CASES)
@pytest.mark.parametrize(
    "excess_blob_gas",
    [0, 131_072, 1_000_000, 20_000_000, 50_000_000],
)
def test_fork_local_blob_price_matches_schedule_oracle(
    _duration_ms: int,
    slot: U64,
    parameters: local.ForkLocalELParameters,
    excess_blob_gas: int,
) -> None:
    """Fixed fork-local blob pricing reproduces the schedule oracle exactly."""
    oracle = calculate_blob_gas_price_for_slot(
        U64(excess_blob_gas),
        slot,
        ORACLE_SCHEDULE,
    )
    competitor = local.calculate_fork_local_blob_price(
        U64(excess_blob_gas),
        parameters,
    )
    assert competitor == oracle


@pytest.mark.parametrize(
    "duration_ms,sequential",
    [
        (8_000, local.FORK_8S),
        (6_000, local.FORK_6S),
    ],
)
def test_direct_baseline_blob_constants_are_not_blocked_by_rounding(
    duration_ms: int,
    sequential: local.ForkLocalELParameters,
) -> None:
    """Discrete blob rounding does not force the EL to own duration history.

    A fork may choose explicit constants against the original wall-clock
    objective rather than mechanically carrying forward sequential rounding.
    For the synthetic 8s and 6s eras, direct-baseline constants are at least as
    close to the original target/max blob throughput as the sequential oracle.
    """
    direct = scale_blob_schedule(
        BASE_BLOB_SCHEDULE,
        BASE_SLOT_DURATION_MS,
        Uint(duration_ms),
    )
    sequential_blob = _blob_parameters(sequential)

    assert direct != sequential_blob

    baseline_target_rate = Fraction(
        int(BASE_BLOB_SCHEDULE.target),
        int(BASE_SLOT_DURATION_MS),
    )
    baseline_max_rate = Fraction(
        int(BASE_BLOB_SCHEDULE.maximum),
        int(BASE_SLOT_DURATION_MS),
    )

    assert _rate_error(
        int(direct.target),
        duration_ms,
        baseline_target_rate,
    ) <= _rate_error(
        int(sequential_blob.target),
        duration_ms,
        baseline_target_rate,
    )
    assert _rate_error(
        int(direct.maximum),
        duration_ms,
        baseline_max_rate,
    ) <= _rate_error(
        int(sequential_blob.maximum),
        duration_ms,
        baseline_max_rate,
    )


def test_schedule_free_competitor_covers_every_oracle_era() -> None:
    """The experiment has a fixed EL snapshot for every oracle duration era."""
    assert tuple(case[0] for case in ERA_CASES) == (12_000, 10_000, 8_000, 6_000)
    assert tuple(case[2].gas_limit_target for case in ERA_CASES) == (
        Uint(72_000_000),
        Uint(60_000_000),
        Uint(48_000_000),
        Uint(36_000_000),
    )
