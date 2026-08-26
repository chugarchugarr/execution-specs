"""
Prototype test for preserving fixed-gas call liveness across EIP-8038.

The test first proves the liveness regression independently of any proposed
protocol change, then pins the conservation equation a write-prepayment remedy
must satisfy:

    child-frame charge + prepaid repricing delta
        == Amsterdam child-frame charge

A later commit replaces the algebraic voucher leg with executable Amsterdam
semantics only after the regression itself survives the filler.
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

    The same fixture also proves the accounting invariant required by a later
    voucher implementation: moving exactly the repricing delta outside the
    child leaves the total Amsterdam execution charge unchanged.
    """
    before = fork.fork_at(timestamp=BEFORE_TS)
    after = fork.fork_at(timestamp=AFTER_TS)

    write = _warm_existing_slot_write(fork)
    cost_before = write.execution_cost(before)
    cost_after = write.execution_cost(after)

    assert cost_after > cost_before
    fixed_child_gas = (cost_before + cost_after) // 2
    assert cost_before <= fixed_child_gas < cost_after

    # Conservation gate for the future voucher implementation.
    prepaid_repricing_delta = cost_after - cost_before
    voucher_frame_charge = cost_after - prepaid_repricing_delta
    assert prepaid_repricing_delta > 0
    assert voucher_frame_charge == cost_before
    assert voucher_frame_charge <= fixed_child_gas
    assert voucher_frame_charge + prepaid_repricing_delta == cost_after

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
