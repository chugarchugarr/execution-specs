"""
Executable proof for preserving fixed-gas call liveness across EIP-8038.

The primary fixture proves three legs with identical child bytecode:

    parent schedule PASS -> Amsterdam FAIL -> Amsterdam + voucher PASS

The experimental voucher is encoded with three domain-separated EIP-2930
storage-key markers. Their existing intrinsic charges move a bounded portion of
the Amsterdam STORAGE_WRITE price outside the child frame. The remaining
write charge stays inside the child. The core storage-access/write charge is
conserved exactly; marker bytes add ordinary transaction-data overhead on top.

Additional fixtures contain the proof mechanism: partial marker sets do not
activate it, an OOG attempt consumes the prepaid credit instead of making it
replayable, and ordinary Amsterdam writes retain their normal semantics.

This marker encoding is a proof vehicle, not a proposed final wire format.
"""

import pytest
from ethereum_types.bytes import Bytes32

from ethereum.crypto.hash import keccak256
from execution_testing import (
    AccessList,
    Account,
    Address,
    Alloc,
    Block,
    BlockchainTestFiller,
    Bytecode,
    Fork,
    Op,
    Transaction,
)

from .spec import ref_spec_8038

REFERENCE_SPEC_GIT_PATH = ref_spec_8038.git_path
REFERENCE_SPEC_VERSION = ref_spec_8038.version

BEFORE_TS = 14_999
AFTER_TS = 15_000
VOUCHER_TS = 15_001
WRITE_PREPAYMENT_DOMAIN = b"glamsterdam-write-prepayment-v1"
WRITE_PREPAYMENT_MARKER_COUNT = 3

pytestmark = pytest.mark.valid_at_transition_to("Amsterdam")


def _warm_existing_slot_write(fork: Fork) -> Bytecode:
    """Return one warm nonzero-to-nonzero SSTORE with explicit metadata."""
    return Op.SSTORE.with_metadata(
        key_warm=True,
        original_value=1,
        current_value=1,
        new_value=2,
    )(0, 2)


def _write_prepayment_markers(address: Address, key: int) -> list[Bytes32]:
    """Mirror the experimental Amsterdam marker derivation."""
    key_bytes = Bytes32(key.to_bytes(32, "big"))
    prefix = WRITE_PREPAYMENT_DOMAIN + bytes(address) + bytes(key_bytes)
    return [
        keccak256(prefix + bytes([index]))
        for index in range(WRITE_PREPAYMENT_MARKER_COUNT)
    ]


def _fixed_gas_window(fork: Fork) -> tuple[int, int, int, int]:
    """Return parent cost, Amsterdam cost, voucher cost, and fixed budget."""
    before = fork.fork_at(timestamp=BEFORE_TS)
    after = fork.fork_at(timestamp=AFTER_TS)
    write = _warm_existing_slot_write(fork)
    cost_before = write.execution_cost(before)
    cost_after = write.execution_cost(after)
    storage_key_prepayment = Op.SLOAD(key_warm=False).gas_cost(
        after
    ) - Op.SLOAD(key_warm=True).gas_cost(after)
    marker_prepayment = storage_key_prepayment * WRITE_PREPAYMENT_MARKER_COUNT
    voucher_frame_charge = cost_after - marker_prepayment
    fixed_child_gas = (cost_before + cost_after) // 2
    return cost_before, cost_after, voucher_frame_charge, fixed_child_gas


def test_fixed_gas_sstore_liveness_and_write_prepayment(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """
    Prove parent PASS, Amsterdam FAIL, and Amsterdam+prepayment PASS.

    All three children begin with slot 0 == 1 and execute identical bytecode
    that changes the existing slot to 2. The target slot is access-listed in
    every leg, removing cold-access and EIP-8037 state-creation confounders.
    The CALL gas budget is strictly between the parent and Amsterdam execution
    costs, so merely increasing outer transaction gas cannot fix the middle
    leg.

    In the voucher leg, three additional access-list storage keys pay their
    existing intrinsic storage-key charges. Amsterdam consumes that prepayment
    once and reduces only the frame-local STORAGE_WRITE charge by the same
    amount. Thus the child fits the immutable CALL budget without reducing the
    total core storage-access/write price.
    """
    before = fork.fork_at(timestamp=BEFORE_TS)
    after = fork.fork_at(timestamp=AFTER_TS)

    write = _warm_existing_slot_write(fork)
    cost_before = write.execution_cost(before)
    cost_after = write.execution_cost(after)

    assert cost_after > cost_before
    fixed_child_gas = (cost_before + cost_after) // 2
    assert cost_before <= fixed_child_gas < cost_after

    # One EIP-2930 storage-key prepayment equals cold minus warm SLOAD.
    storage_key_prepayment = Op.SLOAD(key_warm=False).gas_cost(
        after
    ) - Op.SLOAD(key_warm=True).gas_cost(after)
    marker_prepayment = storage_key_prepayment * WRITE_PREPAYMENT_MARKER_COUNT
    voucher_frame_charge = cost_after - marker_prepayment

    assert marker_prepayment > 0
    assert voucher_frame_charge <= fixed_child_gas
    # Core conservation: the amount removed from the child is exactly the
    # amount already paid by the three marker-key intrinsic charges.
    assert voucher_frame_charge + marker_prepayment == cost_after

    child_before = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    child_after = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    child_voucher = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})

    parent_before = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child_before))
    )
    parent_after = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child_after))
    )
    parent_voucher = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child_voucher))
    )

    voucher_keys = [0, *_write_prepayment_markers(child_voucher, 0)]

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
        Block(
            timestamp=VOUCHER_TS,
            txs=[
                Transaction(
                    to=parent_voucher,
                    sender=pre.fund_eoa(),
                    access_list=[
                        AccessList(
                            address=child_voucher,
                            storage_keys=voucher_keys,
                        )
                    ],
                )
            ],
        ),
    ]

    post = {
        child_before: Account(storage={0: 2}),
        child_after: Account(storage={0: 1}),
        child_voucher: Account(storage={0: 2}),
    }
    blockchain_test(pre=pre, blocks=blocks, post=post)


def test_partial_write_prepayment_does_not_discount(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """Two of the three markers are insufficient to rescue the fixed call."""
    _, cost_after, _, fixed_child_gas = _fixed_gas_window(fork)
    assert fixed_child_gas < cost_after

    child = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    parent = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child))
    )
    markers = _write_prepayment_markers(child, 0)

    blocks = [
        Block(
            timestamp=AFTER_TS,
            txs=[
                Transaction(
                    to=parent,
                    sender=pre.fund_eoa(),
                    access_list=[
                        AccessList(
                            address=child,
                            storage_keys=[0, *markers[:2]],
                        )
                    ],
                )
            ],
        )
    ]

    post = {child: Account(storage={0: 1})}
    blockchain_test(pre=pre, blocks=blocks, post=post)


def test_write_prepayment_is_not_replayable_after_oog(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """
    An OOG attempt burns its prepaid write credit transaction-wide.

    The first call has one gas less than the discounted frame requires. The
    SSTORE recognizes and consumes the marker set, then fails its gas charge.
    A second call in the same transaction has enough gas for the discounted
    write but not the ordinary Amsterdam write. It must still fail; otherwise
    one intrinsic prepayment could subsidize arbitrarily many retrying frames.
    """
    _, cost_after, voucher_frame_charge, fixed_child_gas = _fixed_gas_window(
        fork
    )
    assert voucher_frame_charge > 2_301
    first_attempt_gas = voucher_frame_charge - 1
    assert first_attempt_gas < voucher_frame_charge <= fixed_child_gas
    assert fixed_child_gas < cost_after

    child = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    parent = pre.deploy_contract(
        code=(
            Op.POP(Op.CALL(gas=first_attempt_gas, address=child))
            + Op.POP(Op.CALL(gas=fixed_child_gas, address=child))
        )
    )
    voucher_keys = [0, *_write_prepayment_markers(child, 0)]

    blocks = [
        Block(
            timestamp=AFTER_TS,
            txs=[
                Transaction(
                    to=parent,
                    sender=pre.fund_eoa(),
                    access_list=[
                        AccessList(address=child, storage_keys=voucher_keys)
                    ],
                )
            ],
        )
    ]

    post = {child: Account(storage={0: 1})}
    blockchain_test(pre=pre, blocks=blocks, post=post)


def test_ordinary_amsterdam_sstore_remains_unchanged(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """
    Verify an adequately funded Amsterdam write still works without markers.
    """
    _, cost_after, _, _ = _fixed_gas_window(fork)

    child = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    parent = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=cost_after + 10_000, address=child))
    )

    blocks = [
        Block(
            timestamp=AFTER_TS,
            txs=[
                Transaction(
                    to=parent,
                    sender=pre.fund_eoa(),
                    access_list=[AccessList(address=child, storage_keys=[0])],
                )
            ],
        )
    ]

    post = {child: Account(storage={0: 2})}
    blockchain_test(pre=pre, blocks=blocks, post=post)
