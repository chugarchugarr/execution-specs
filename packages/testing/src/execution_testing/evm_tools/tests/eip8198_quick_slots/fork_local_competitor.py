"""Schedule-free EIP-8198 execution-layer competitor.

Each synthetic fork carries only the execution constants needed by that fork.
There is no runtime slot-duration schedule, epoch lookup, or slot lookup here.
"""

from dataclasses import dataclass
from typing import Final, final

from ethereum_types.numeric import U64, Uint

from ethereum.utils.numeric import taylor_exponential

ELASTICITY_MULTIPLIER: Final[Uint] = Uint(2)
BLOB_MIN_GASPRICE: Final[Uint] = Uint(1)


@final
@dataclass(frozen=True)
class ForkLocalELParameters:
    """Execution constants compiled into one fork."""

    gas_limit_target: Uint
    base_fee_numerator: Uint
    base_fee_denominator: Uint
    blob_maximum: U64
    blob_target: U64
    blob_update_fraction: Uint


BASELINE_12S: Final[ForkLocalELParameters] = ForkLocalELParameters(
    gas_limit_target=Uint(72_000_000),
    base_fee_numerator=Uint(1),
    base_fee_denominator=Uint(8),
    blob_maximum=U64(21),
    blob_target=U64(14),
    blob_update_fraction=Uint(11_684_671),
)

FORK_10S: Final[ForkLocalELParameters] = ForkLocalELParameters(
    gas_limit_target=Uint(60_000_000),
    base_fee_numerator=Uint(5),
    base_fee_denominator=Uint(48),
    blob_maximum=U64(17),
    blob_target=U64(12),
    blob_update_fraction=Uint(10_015_432),
)

FORK_8S: Final[ForkLocalELParameters] = ForkLocalELParameters(
    gas_limit_target=Uint(48_000_000),
    base_fee_numerator=Uint(1),
    base_fee_denominator=Uint(12),
    blob_maximum=U64(13),
    blob_target=U64(10),
    blob_update_fraction=Uint(7_511_574),
)

FORK_6S: Final[ForkLocalELParameters] = ForkLocalELParameters(
    gas_limit_target=Uint(36_000_000),
    base_fee_numerator=Uint(1),
    base_fee_denominator=Uint(16),
    blob_maximum=U64(9),
    blob_target=U64(8),
    blob_update_fraction=Uint(3_338_477),
)


def calculate_fork_local_base_fee(
    parent_gas_limit: Uint,
    parent_gas_used: Uint,
    parent_base_fee_per_gas: Uint,
    parameters: ForkLocalELParameters,
) -> Uint:
    """Calculate base fee using only constants compiled into the active fork."""
    parent_gas_target = parent_gas_limit // ELASTICITY_MULTIPLIER

    if parent_gas_used == parent_gas_target:
        return parent_base_fee_per_gas

    if parent_gas_used > parent_gas_target:
        gas_used_delta = parent_gas_used - parent_gas_target
        parent_fee_gas_delta = parent_base_fee_per_gas * gas_used_delta
        target_fee_gas_delta = parent_fee_gas_delta // parent_gas_target
        base_fee_delta = max(
            target_fee_gas_delta
            * parameters.base_fee_numerator
            // parameters.base_fee_denominator,
            Uint(1),
        )
        return Uint(parent_base_fee_per_gas + base_fee_delta)

    gas_used_delta = parent_gas_target - parent_gas_used
    parent_fee_gas_delta = parent_base_fee_per_gas * gas_used_delta
    target_fee_gas_delta = parent_fee_gas_delta // parent_gas_target
    base_fee_delta = (
        target_fee_gas_delta
        * parameters.base_fee_numerator
        // parameters.base_fee_denominator
    )
    return Uint(parent_base_fee_per_gas - base_fee_delta)


def calculate_fork_local_blob_price(
    excess_blob_gas: U64,
    parameters: ForkLocalELParameters,
) -> Uint:
    """Calculate blob price using only constants compiled into the active fork."""
    return taylor_exponential(
        BLOB_MIN_GASPRICE,
        Uint(excess_blob_gas),
        parameters.blob_update_fraction,
    )
