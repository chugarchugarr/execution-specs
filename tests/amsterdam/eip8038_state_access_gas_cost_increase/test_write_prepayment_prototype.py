"""
Executable four-assertion proof for fixed-gas call liveness across EIP-8038.

Four independently named tests prove Osaka PASS, Amsterdam FAIL, Amsterdam
with a write prepayment PASS, and preservation of the 12,100 Amsterdam storage
resource charge.

The experimental voucher is signaled by three domain-separated EIP-2930
storage-key markers. Their 6,000 core intrinsic charge plus a 1,200 prototype
intrinsic surcharge pay the exact 7,200 repricing delta at the transaction
boundary. The child retains the historical 2,800 write component.

Additional fixtures contain the proof mechanism: partial marker sets do not
activate it, an OOG attempt consumes the prepaid credit instead of making it
replayable, and ordinary Amsterdam writes retain their normal semantics.

This marker encoding is a proof vehicle, not a proposed final wire format.
"""

import pytest
from ethereum_types.bytes import Bytes, Bytes32
from ethereum_types.numeric import U64, U256, Uint
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

from ethereum.crypto.hash import keccak256
from ethereum.forks.amsterdam.transactions import (
    ACCESS_LIST_STORAGE_KEY_FLOOR_TOKENS,
    AccessListTransaction,
    calculate_intrinsic_cost,
)
from ethereum.forks.amsterdam.transactions import (
    Access as AmsterdamAccess,
)
from ethereum.forks.amsterdam.vm.gas import GasCosts
from ethereum.state import Address as StateAddress

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


def _write_prepayment_markers(
    address: Address | StateAddress, key: int
) -> list[Bytes32]:
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
    repricing_delta = cost_after - cost_before
    voucher_frame_charge = cost_after - repricing_delta
    fixed_child_gas = (cost_before + cost_after) // 2
    return cost_before, cost_after, voucher_frame_charge, fixed_child_gas


def _execute_fixed_gas_leg(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
    *,
    timestamp: int,
    voucher: bool,
    expected_value: int,
) -> None:
    """Execute one parent/child pair under the shared immutable budget."""
    cost_before, cost_after, voucher_cost, fixed_child_gas = _fixed_gas_window(
        fork
    )
    assert cost_before <= fixed_child_gas < cost_after
    assert voucher_cost == cost_before

    child = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    parent = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=fixed_child_gas, address=child))
    )

    storage_keys: list[int | Bytes32] = [0]
    if voucher:
        storage_keys.extend(_write_prepayment_markers(child, 0))

    blocks = [
        Block(
            timestamp=timestamp,
            txs=[
                Transaction(
                    to=parent,
                    sender=pre.fund_eoa(),
                    access_list=[
                        AccessList(address=child, storage_keys=storage_keys)
                    ],
                )
            ],
        )
    ]
    post = {child: Account(storage={0: expected_value})}
    blockchain_test(pre=pre, blocks=blocks, post=post)


def test_osaka_pass(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """OSAKA_PASS: the fixed-gas child changes the existing nonzero slot."""
    _execute_fixed_gas_leg(
        blockchain_test,
        pre,
        fork,
        timestamp=BEFORE_TS,
        voucher=False,
        expected_value=2,
    )


def test_amsterdam_fail(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """AMSTERDAM_FAIL: identical child execution runs out of gas."""
    _execute_fixed_gas_leg(
        blockchain_test,
        pre,
        fork,
        timestamp=AFTER_TS,
        voucher=False,
        expected_value=1,
    )


def test_amsterdam_voucher_pass(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """AMSTERDAM_VOUCHER_PASS: prepaid delta restores child liveness."""
    _execute_fixed_gas_leg(
        blockchain_test,
        pre,
        fork,
        timestamp=VOUCHER_TS,
        voucher=True,
        expected_value=2,
    )


def test_amsterdam_total_charge_preserved(
    blockchain_test: BlockchainTestFiller,
    pre: Alloc,
    fork: Fork,
) -> None:
    """AMSTERDAM_TOTAL_CHARGE_PRESERVED: both paths charge 12,100."""
    before = fork.fork_at(timestamp=BEFORE_TS)
    after = fork.fork_at(timestamp=AFTER_TS)
    bare_write = Op.SSTORE.with_metadata(
        key_warm=True,
        original_value=1,
        current_value=1,
        new_value=2,
    )

    warm_access = Op.SLOAD(key_warm=True).gas_cost(after)
    cold_access = Op.SLOAD(key_warm=False).gas_cost(after)
    legacy_write = bare_write.execution_cost(before) - warm_access
    amsterdam_write = bare_write.execution_cost(after) - warm_access
    access_list_prepayment = cold_access - warm_access
    repricing_delta = amsterdam_write - legacy_write
    marker_prepayment = access_list_prepayment * WRITE_PREPAYMENT_MARKER_COUNT
    prototype_remainder = repricing_delta - marker_prepayment

    ordinary_amsterdam = cold_access + amsterdam_write
    prepaid_amsterdam = (
        access_list_prepayment
        + marker_prepayment
        + prototype_remainder
        + warm_access
        + legacy_write
    )

    assert access_list_prepayment == 2_000
    assert repricing_delta == 7_200
    assert marker_prepayment == 6_000
    assert prototype_remainder == 1_200
    assert warm_access == 100
    assert legacy_write == 2_800
    assert ordinary_amsterdam == prepaid_amsterdam == 12_100

    # Bind the equation to the implemented transaction-boundary charge. The
    # provisional marker bytes have ordinary EIP-7981 encoding overhead; after
    # removing only that overhead, the actual intrinsic delta is exactly 7,200.
    target = StateAddress(b"\x11" * 20)
    sender = StateAddress(b"\x22" * 20)
    key = Bytes32(b"\x00" * 32)
    markers = tuple(_write_prepayment_markers(target, 0))

    def intrinsic_for(slots: tuple[Bytes32, ...]) -> int:
        tx = AccessListTransaction(
            chain_id=U64(1),
            nonce=U256(0),
            gas_price=Uint(1),
            gas=Uint(1_000_000),
            to=target,
            value=U256(0),
            data=Bytes(b""),
            access_list=(AmsterdamAccess(account=target, slots=slots),),
            y_parity=U256(0),
            r=U256(0),
            s=U256(0),
        )
        return int(calculate_intrinsic_cost(tx, sender).execution)

    ordinary_intrinsic = intrinsic_for((key,))
    voucher_intrinsic = intrinsic_for((key, *markers))
    marker_encoding_overhead = int(
        Uint(WRITE_PREPAYMENT_MARKER_COUNT)
        * ACCESS_LIST_STORAGE_KEY_FLOOR_TOKENS
        * GasCosts.TX_DATA_TOKEN_FLOOR
    )
    assert (
        voucher_intrinsic - ordinary_intrinsic - marker_encoding_overhead
        == repricing_delta
        == 7_200
    )

    _, cost_after, voucher_cost, _ = _fixed_gas_window(fork)
    ordinary_child = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    voucher_child = pre.deploy_contract(code=Op.SSTORE(0, 2), storage={0: 1})
    ordinary_parent = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=cost_after, address=ordinary_child))
    )
    voucher_parent = pre.deploy_contract(
        code=Op.POP(Op.CALL(gas=voucher_cost, address=voucher_child))
    )
    voucher_keys = [0, *_write_prepayment_markers(voucher_child, 0)]

    blockchain_test(
        pre=pre,
        blocks=[
            Block(
                timestamp=AFTER_TS,
                txs=[
                    Transaction(
                        to=ordinary_parent,
                        sender=pre.fund_eoa(),
                        access_list=[
                            AccessList(
                                address=ordinary_child,
                                storage_keys=[0],
                            )
                        ],
                    ),
                    Transaction(
                        to=voucher_parent,
                        sender=pre.fund_eoa(),
                        access_list=[
                            AccessList(
                                address=voucher_child,
                                storage_keys=voucher_keys,
                            )
                        ],
                    ),
                ],
            )
        ],
        post={
            ordinary_child: Account(storage={0: 2}),
            voucher_child: Account(storage={0: 2}),
        },
    )


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
