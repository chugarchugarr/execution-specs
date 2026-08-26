"""
Prototype tests for preserving fixed-gas call liveness across EIP-8038.

This file deliberately separates the problem proof from the protocol mechanism.
The first test executes the same fixed-gas child write on both sides of the
Amsterdam transition and demonstrates the liveness regression. The second test
proves the conservation equation required by a write-prepayment remedy:

    legacy child-frame charge + prepaid repricing delta
        == Amsterdam child-frame charge

The prototype does not assign a final wire encoding to the prepayment. That is
kept out of the first proof so the liveness finding can survive or fail on its
own.
"""

import pytest
from execution_testing import (
    AccessList,
    Account,
    Alloc,
    Block,
    BlockchainTestFiller,
    Fork,
    Op,
    Transaction,
)

BEFORE_TS = 14_999
AFTER_TS = 15_000

pytestmark = pytest.mark.valid_at_transition_to("Amsterdam")


def _warm_existing_slot_write(fork: Fork):
    """Return one warm nonzero-to-nonzero SSTORE with explicit metadata."""
    return Op.SSTORE.with_metadata(
        key_warm=True,
        original_value=1,
        current_value=1,
        new_value=2,
    )(0, 2)


def test_fixed_gas_sstore_liveness_regresses_at_amsterdam(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """
    A fixed child CALL budget can be live before Amsterdam and dead after it.

    Both children begin with slot 0 == 1 and execute identical bytecode that
    changes the already-existing slot to 2. The access list pre-warms the slot,
    removing cold-access and EIP-8037 state-creation confounders. The CALL gas
    budget is chosen strictly between the pre- and post-fork execution costs.

    The first block therefore commits the write. The second child runs out of
    gas and rolls back the write even though the outer transaction has ample
    gas. Increasing only outer transaction gas cannot alter the immutable CALL
    operand.
    """
    before = fork.fork_at(timestamp=BEFORE_TS)
    after = fork.fork_at(timestamp=AFTER_TS)

    write = _warm_existing_slot_write(fork)
    cost_before = write.execution_cost(before)
    cost_after = write.execution_cost(after)

    assert cost_after > cost_before
    fixed_child_gas = (cost_before + cost_after) // 2
    assert cost_before <= fixed_child_gas < cost_after

    child_before = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    child_after = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})

    parent_before = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child_before))
    )
    parent_after = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child_after))
    )

    blocks = [
        Block(
            timestamp=BEFORE_TS,
            txs=[
                Transaction(
                    to=parent_before,
                    sender=pre.fund_eoa(),
                    access_list=[
                        AccessList(address=child_before, storage_keys=[0])
                    ],
                )
            ],
        ),
        Block(
            timestamp=AFTER_TS,
            txs=[
                Transaction(
                    to=parent_after,
                    sender=pre.fund_eoa(),
                    access_list=[
                        AccessList(address=child_after, storage_keys=[0])
                    ],
                )
            ],
        ),
    ]

    post = {
        child_before: Account(storage={0: 2}),
        child_after: Account(storage={0: 1}),
    }
    blockchain_test(pre=pre, blocks=blocks, post=post)


def test_write_prepayment_conserves_amsterdam_execution_charge(
    fork: Fork,
) -> None:
    """
    Splitting only the repricing delta preserves the Amsterdam gas charge.

    A write voucher must not subsidize the operation. It moves exactly the
    Amsterdam-minus-parent execution-gas delta to transaction scope and leaves
    the child frame paying the parent-fork execution charge. The total remains
    identical to the unmodified Amsterdam execution charge while restoring a
    fixed budget that lies between the two costs.
    """
    before = fork.fork_at(timestamp=BEFORE_TS)
    after = fork.fork_at(timestamp=AFTER_TS)

    write = _warm_existing_slot_write(fork)
    legacy_frame_charge = write.execution_cost(before)
    amsterdam_frame_charge = write.execution_cost(after)
    prepaid_repricing_delta = amsterdam_frame_charge - legacy_frame_charge

    fixed_child_gas = (legacy_frame_charge + amsterdam_frame_charge) // 2

    # Osaka/current-parent execution remains live under the fixed budget.
    assert legacy_frame_charge <= fixed_child_gas
    # Unmodified Amsterdam execution is no longer live.
    assert amsterdam_frame_charge > fixed_child_gas
    # The voucher restores the old frame-local requirement.
    voucher_frame_charge = amsterdam_frame_charge - prepaid_repricing_delta
    assert voucher_frame_charge == legacy_frame_charge
    assert voucher_frame_charge <= fixed_child_gas
    # No resource discount: transaction prepayment + frame charge is exactly
    # the unmodified Amsterdam execution charge.
    assert prepaid_repricing_delta > 0
    assert voucher_frame_charge + prepaid_repricing_delta == amsterdam_frame_charge
