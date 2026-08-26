"""Ethereum specification entry point for Amsterdam."""

from dataclasses import dataclass
from typing import List, Optional, Tuple, final

from ethereum_rlp import rlp
from ethereum_types.bytes import Bytes
from ethereum_types.frozen import slotted_freezable
from ethereum_types.numeric import U64, U256, Uint, ulen

from ethereum.crypto.hash import Hash32, keccak256
from ethereum.exceptions import (
    EthereumException,
    GasUsedExceedsLimitError,
    InsufficientBalanceError,
    InvalidBlock,
    InvalidSenderError,
    NonceMismatchError,
)
from ethereum.forks.bpo5.blocks import Header as PreviousHeader
from ethereum.merkle_patricia_trie import root, trie_set
from ethereum.state import EMPTY_CODE_HASH, Address, BlockDiff
from ethereum.state_mpt import State, apply_changes_to_state

from . import vm
from .block_access_lists import (
    BlockAccessListBuilder,
    build_block_access_list,
    hash_block_access_list,
    validate_block_access_list_gas_limit,
)
from .blocks import Block, Header, Log, Receipt, Withdrawal, encode_receipt
from .bloom import logs_bloom
from .exceptions import (
    BlobCountExceededError,
    BlobGasLimitExceededError,
    EmptyAuthorizationListError,
    InsufficientMaxFeePerBlobGasError,
    InsufficientMaxFeePerGasError,
    InvalidBlobVersionedHashError,
    NoBlobDataError,
    PriorityFeeGreaterThanMaxFeeError,
    TransactionTypeContractCreationError,
    WrongChainIdError,
)
from .fork_types import Authorization, BlockAccessIndex, VersionedHash
from .requests import (
    BUILDER_DEPOSIT_REQUEST_TYPE,
    BUILDER_EXIT_REQUEST_TYPE,
    CONSOLIDATION_REQUEST_TYPE,
    DEPOSIT_REQUEST_TYPE,
    WITHDRAWAL_REQUEST_TYPE,
    compute_requests_hash,
    parse_deposit_requests,
)
from .slot_timing import (
    BASE_SLOT_DURATION_MS,
    BLOB_BASE_COST,
    BLOB_GAS_PER_BLOB,
    SLOT_DURATION_SCHEDULE,
    SlotDurationSchedule,
    calculate_blob_gas_price_for_slot,
    get_blob_schedule,
    get_slot_duration_ms,
    get_transition_durations,
    scale_transition_limit,
)
from .state_tracker import (
    BlockState,
    TransactionState,
    clear_account_preserving_balance,
    create_ether,
    extract_block_diff,
    get_account,
    get_code,
    incorporate_tx_into_block,
    increment_nonce,
    set_account_balance,
)
from .transactions import (
    TX_MAX_GAS_LIMIT,
    BlobTransaction,
    FeeMarketCapableTransaction,
    LegacyTransaction,
    SetCodeTransaction,
    Transaction,
    chain_id,
    decode_transaction,
    encode_transaction,
    get_transaction_hash,
    has_access_list,
    recover_sender,
    validate_transaction,
)
from .utils.hexadecimal import hex_to_address
from .utils.message import prepare_message
from .vm import Message
from .vm.eoa_delegation import is_valid_delegation
from .vm.gas import (
    GasCosts,
    StateGasCosts,
    allocate_execution_gas,
    calculate_total_blob_gas,
    settle_transaction_gas,
)
from .vm.interpreter import MessageCallOutput, process_message_call

BASE_FEE_MAX_CHANGE_DENOMINATOR = Uint(8)
ELASTICITY_MULTIPLIER = Uint(2)
EMPTY_OMMER_HASH = keccak256(rlp.encode([]))
SYSTEM_ADDRESS = hex_to_address("0xfffffffffffffffffffffffffffffffffffffffe")
BEACON_ROOTS_ADDRESS = hex_to_address(
    "0x000F3df6D732807Ef1319fB7B8bB8522d0Beac02"
)
SYSTEM_TRANSACTION_GAS = Uint(30000000)
SYSTEM_MAX_SSTORES_PER_CALL = Uint(16)
VERSIONED_HASH_VERSION_KZG = b"\x01"
GWEI_TO_WEI = U256(10**9)

WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS = hex_to_address(
    "0x00000961Ef480Eb55e80D19ad83579A64c007002"
)
CONSOLIDATION_REQUEST_PREDEPLOY_ADDRESS = hex_to_address(
    "0x0000BBdDc7CE488642fb579F8B00f3a590007251"
)
BUILDER_DEPOSIT_CONTRACT_ADDRESS = hex_to_address(
    "0x0000BFF46984E3725691FA540A8C7589300D8282"
)
BUILDER_EXIT_CONTRACT_ADDRESS = hex_to_address(
    "0x000064D678505AD48F8CCB093BC65613800E8282"
)
HISTORY_STORAGE_ADDRESS = hex_to_address(
    "0x0000F90827F1C53a10cb7A02335B175320002935"
)

MAX_BLOCK_SIZE = 10_485_760
SAFETY_MARGIN = 2_097_152
MAX_RLP_BLOCK_SIZE = MAX_BLOCK_SIZE - SAFETY_MARGIN
BLOB_COUNT_LIMIT = 6


@final
@slotted_freezable
@dataclass
class ChainContext:
    """Chain context needed for block execution."""

    chain_id: U64
    block_hashes: List[Hash32]
    parent_header: Header | PreviousHeader


@final
@dataclass
class BlockChain:
    """History and current state of the block chain."""

    blocks: List[Block]
    state: State
    chain_id: U64


def apply_fork(old: BlockChain) -> BlockChain:
    """Return the chain after the Amsterdam fork transition."""
    return old


def get_last_256_block_hashes(chain: BlockChain) -> List[Hash32]:
    """Return hashes of the most recent 256 blocks in increasing order."""
    recent_blocks = chain.blocks[-255:]
    if len(recent_blocks) == 0:
        return []

    recent_block_hashes = []
    for block in recent_blocks:
        recent_block_hashes.append(block.header.parent_hash)

    recent_block_hashes.append(keccak256(rlp.encode(recent_blocks[-1].header)))
    return recent_block_hashes


def state_transition(chain: BlockChain, block: Block) -> None:
    """Apply a block to the chain after validating and executing it."""
    chain_context = ChainContext(
        chain_id=chain.chain_id,
        block_hashes=get_last_256_block_hashes(chain),
        parent_header=chain.blocks[-1].header,
    )
    block_diff = execute_block(block, chain.state, chain_context)
    apply_changes_to_state(chain.state, block_diff)
    chain.blocks.append(block)
    if len(chain.blocks) > 255:
        chain.blocks = chain.blocks[-255:]


def execute_block(
    block: Block,
    pre_state: State,
    chain_context: ChainContext,
) -> BlockDiff:
    """Execute a block and validate the resulting roots against the header."""
    if len(rlp.encode(block)) > MAX_RLP_BLOCK_SIZE:
        raise InvalidBlock("Block rlp size exceeds MAX_RLP_BLOCK_SIZE")

    parent_header = chain_context.parent_header
    validate_header(parent_header, block.header)

    if block.ommers != ():
        raise InvalidBlock

    block_state = BlockState(pre_state=pre_state)
    block_env = vm.BlockEnvironment(
        chain_id=chain_context.chain_id,
        state=block_state,
        block_gas_limit=block.header.gas_limit,
        block_hashes=chain_context.block_hashes,
        coinbase=block.header.coinbase,
        number=block.header.number,
        base_fee_per_gas=block.header.base_fee_per_gas,
        time=block.header.timestamp,
        prev_randao=block.header.prev_randao,
        excess_blob_gas=block.header.excess_blob_gas,
        parent_beacon_block_root=block.header.parent_beacon_block_root,
        block_access_list_builder=BlockAccessListBuilder(),
        slot_number=block.header.slot_number,
    )

    block_output = apply_body(
        block_env=block_env,
        transactions=block.transactions,
        withdrawals=block.withdrawals,
    )
    block_diff = extract_block_diff(block_state)
    block_state_root = pre_state.compute_state_root(block_diff)
    transactions_root = root(block_output.transactions_trie)
    receipt_root = root(block_output.receipts_trie)
    block_logs_bloom = logs_bloom(block_output.block_logs)
    withdrawals_root = root(block_output.withdrawals_trie)
    requests_hash = compute_requests_hash(block_output.requests)
    computed_block_access_list_hash = hash_block_access_list(
        block_output.block_access_list
    )

    block_gas_used = max(
        block_output.block_gas_used,
        block_output.block_state_gas_used,
    )
    if block_gas_used != block.header.gas_used:
        raise InvalidBlock(f"{block_gas_used} != {block.header.gas_used}")
    if transactions_root != block.header.transactions_root:
        raise InvalidBlock
    if block_state_root != block.header.state_root:
        raise InvalidBlock
    if receipt_root != block.header.receipt_root:
        raise InvalidBlock
    if block_logs_bloom != block.header.bloom:
        raise InvalidBlock
    if withdrawals_root != block.header.withdrawals_root:
        raise InvalidBlock
    if block_output.blob_gas_used != block.header.blob_gas_used:
        raise InvalidBlock
    if requests_hash != block.header.requests_hash:
        raise InvalidBlock
    if computed_block_access_list_hash != block.header.block_access_list_hash:
        raise InvalidBlock("Invalid block access list hash")

    return block_diff


def get_max_blob_gas_per_block(
    slot_number: U64,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> U64:
    """
    Return the current per-block blob capacity from the duration schedule.
    """
    return (
        BLOB_GAS_PER_BLOB
        * get_blob_schedule(slot_number, slot_duration_schedule).maximum
    )


def calculate_excess_blob_gas_for_slot(
    parent_header: Header | PreviousHeader,
    current_slot_number: U64,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> U64:
    """Calculate excess blob gas with parameters for the current slot era."""
    excess_blob_gas = U64(0)
    blob_gas_used = U64(0)
    base_fee_per_gas = Uint(0)

    if isinstance(parent_header, Header):
        excess_blob_gas = parent_header.excess_blob_gas
        blob_gas_used = parent_header.blob_gas_used
        base_fee_per_gas = parent_header.base_fee_per_gas

    blob_schedule = get_blob_schedule(
        current_slot_number, slot_duration_schedule
    )
    target_blob_gas_per_block = BLOB_GAS_PER_BLOB * blob_schedule.target
    parent_blob_gas = excess_blob_gas + blob_gas_used
    if parent_blob_gas < target_blob_gas_per_block:
        return U64(0)

    target_blob_gas_price = Uint(BLOB_GAS_PER_BLOB)
    target_blob_gas_price *= calculate_blob_gas_price_for_slot(
        excess_blob_gas,
        current_slot_number,
        slot_duration_schedule,
    )

    base_blob_tx_price = BLOB_BASE_COST * base_fee_per_gas
    if base_blob_tx_price > target_blob_gas_price:
        blob_schedule_delta = blob_schedule.maximum - blob_schedule.target
        return U64(
            excess_blob_gas
            + blob_gas_used * blob_schedule_delta // blob_schedule.maximum
        )

    return U64(parent_blob_gas - target_blob_gas_per_block)


def calculate_data_fee_for_slot(
    excess_blob_gas: U64,
    tx: Transaction,
    slot_number: U64,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> Uint:
    """Calculate the blob data fee using the current slot-duration era."""
    return Uint(calculate_total_blob_gas(tx)) * (
        calculate_blob_gas_price_for_slot(
            excess_blob_gas,
            slot_number,
            slot_duration_schedule,
        )
    )


def calculate_base_fee_per_gas(
    block_gas_limit: Uint,
    parent_gas_limit: Uint,
    parent_gas_used: Uint,
    parent_base_fee_per_gas: Uint,
    gas_limit_reference: Optional[Uint] = None,
    slot_duration_ms: Optional[Uint] = None,
) -> Uint:
    """
    Calculate base fee while preserving responsiveness per wall-clock time.

    ``gas_limit_reference`` is transition-scoped and may use the current/parent
    duration ratio. ``slot_duration_ms`` is era-scoped and is always compared
    with the pre-schedule base duration, so a later 10 -> 8 transition keeps
    using 8/12 on every 8-second block rather than reverting to 8/8.
    """
    if gas_limit_reference is None:
        gas_limit_reference = parent_gas_limit
    if slot_duration_ms is None:
        slot_duration_ms = get_slot_duration_ms(U64(0))

    parent_gas_target = parent_gas_limit // ELASTICITY_MULTIPLIER
    if not check_gas_limit(block_gas_limit, gas_limit_reference):
        raise InvalidBlock

    if parent_gas_used == parent_gas_target:
        expected_base_fee_per_gas = parent_base_fee_per_gas
    elif parent_gas_used > parent_gas_target:
        gas_used_delta = parent_gas_used - parent_gas_target
        parent_fee_gas_delta = parent_base_fee_per_gas * gas_used_delta
        target_fee_gas_delta = parent_fee_gas_delta // parent_gas_target
        base_fee_per_gas_delta = max(
            target_fee_gas_delta
            * slot_duration_ms
            // (BASE_SLOT_DURATION_MS * BASE_FEE_MAX_CHANGE_DENOMINATOR),
            Uint(1),
        )
        expected_base_fee_per_gas = (
            parent_base_fee_per_gas + base_fee_per_gas_delta
        )
    else:
        gas_used_delta = parent_gas_target - parent_gas_used
        parent_fee_gas_delta = parent_base_fee_per_gas * gas_used_delta
        target_fee_gas_delta = parent_fee_gas_delta // parent_gas_target
        base_fee_per_gas_delta = (
            target_fee_gas_delta
            * slot_duration_ms
            // (BASE_SLOT_DURATION_MS * BASE_FEE_MAX_CHANGE_DENOMINATOR)
        )
        expected_base_fee_per_gas = (
            parent_base_fee_per_gas - base_fee_per_gas_delta
        )

    return Uint(expected_base_fee_per_gas)


def validate_header(
    parent_header: Header | PreviousHeader,
    header: Header,
    slot_duration_schedule: SlotDurationSchedule = SLOT_DURATION_SCHEDULE,
) -> None:
    """Verify a block header against its parent and duration era."""
    if header.number < Uint(1):
        raise InvalidBlock

    excess_blob_gas = calculate_excess_blob_gas_for_slot(
        parent_header,
        header.slot_number,
        slot_duration_schedule,
    )
    if header.excess_blob_gas != excess_blob_gas:
        raise InvalidBlock

    if header.gas_used > header.gas_limit:
        raise InvalidBlock

    parent_slot_number: Optional[U64]
    if isinstance(parent_header, Header):
        parent_slot_number = parent_header.slot_number
    else:
        # The legacy parent predates SLOTNUM. This is only an adapter for the
        # missing slot value; it is not the transition condition.
        parent_slot_number = None

    old_duration_ms, new_duration_ms = get_transition_durations(
        parent_slot_number,
        header.slot_number,
        slot_duration_schedule,
    )
    gas_limit_reference = scale_transition_limit(
        parent_header.gas_limit,
        old_duration_ms,
        new_duration_ms,
    )

    expected_base_fee_per_gas = calculate_base_fee_per_gas(
        header.gas_limit,
        parent_header.gas_limit,
        parent_header.gas_used,
        parent_header.base_fee_per_gas,
        gas_limit_reference=gas_limit_reference,
        slot_duration_ms=new_duration_ms,
    )
    if expected_base_fee_per_gas != header.base_fee_per_gas:
        raise InvalidBlock
    if header.timestamp <= parent_header.timestamp:
        raise InvalidBlock
    if header.number != parent_header.number + Uint(1):
        raise InvalidBlock
    if len(header.extra_data) > 32:
        raise InvalidBlock
    if header.difficulty != 0:
        raise InvalidBlock
    if header.nonce != b"\x00\x00\x00\x00\x00\x00\x00\x00":
        raise InvalidBlock
    if header.ommers_hash != EMPTY_OMMER_HASH:
        raise InvalidBlock

    block_parent_hash = keccak256(rlp.encode(parent_header))
    if header.parent_hash != block_parent_hash:
        raise InvalidBlock


def check_transaction(
    block_env: vm.BlockEnvironment,
    block_output: vm.BlockOutput,
    tx: Transaction,
    sender: Address,
    tx_state: TransactionState,
) -> Tuple[Uint, Tuple[VersionedHash, ...], U64]:
    """Check whether a transaction is includable in the current block."""
    regular_gas_available = (
        block_env.block_gas_limit - block_output.block_gas_used
    )
    state_gas_available = (
        block_env.block_gas_limit - block_output.block_state_gas_used
    )
    blob_gas_available = (
        get_max_blob_gas_per_block(block_env.slot_number)
        - block_output.blob_gas_used
    )

    if min(TX_MAX_GAS_LIMIT, tx.gas) > regular_gas_available:
        raise GasUsedExceedsLimitError("regular gas used exceeds limit")
    if tx.gas > state_gas_available:
        raise GasUsedExceedsLimitError("state gas used exceeds limit")

    tx_blob_gas_used = calculate_total_blob_gas(tx)
    if tx_blob_gas_used > blob_gas_available:
        raise BlobGasLimitExceededError("blob gas limit exceeded")

    sender_account = get_account(tx_state, sender)

    if isinstance(tx, FeeMarketCapableTransaction):
        if tx.max_fee_per_gas < tx.max_priority_fee_per_gas:
            raise PriorityFeeGreaterThanMaxFeeError(
                "priority fee greater than max fee"
            )
        if tx.max_fee_per_gas < block_env.base_fee_per_gas:
            raise InsufficientMaxFeePerGasError(
                tx.max_fee_per_gas, block_env.base_fee_per_gas
            )

        priority_fee_per_gas = min(
            tx.max_priority_fee_per_gas,
            tx.max_fee_per_gas - block_env.base_fee_per_gas,
        )
        effective_gas_price = priority_fee_per_gas + block_env.base_fee_per_gas
        max_gas_fee = tx.gas * tx.max_fee_per_gas
    else:
        if tx.gas_price < block_env.base_fee_per_gas:
            raise InvalidBlock
        effective_gas_price = tx.gas_price
        max_gas_fee = tx.gas * tx.gas_price

    if isinstance(tx, BlobTransaction):
        blob_count = len(tx.blob_versioned_hashes)
        if blob_count == 0:
            raise NoBlobDataError("no blob data in transaction")
        if blob_count > BLOB_COUNT_LIMIT:
            raise BlobCountExceededError(
                f"Tx has {blob_count} blobs. Max allowed: {BLOB_COUNT_LIMIT}"
            )
        for blob_versioned_hash in tx.blob_versioned_hashes:
            if blob_versioned_hash[0:1] != VERSIONED_HASH_VERSION_KZG:
                raise InvalidBlobVersionedHashError(
                    "invalid blob versioned hash"
                )

        blob_gas_price = calculate_blob_gas_price_for_slot(
            block_env.excess_blob_gas,
            block_env.slot_number,
        )
        if Uint(tx.max_fee_per_blob_gas) < blob_gas_price:
            raise InsufficientMaxFeePerBlobGasError(
                "insufficient max fee per blob gas"
            )

        max_gas_fee += Uint(calculate_total_blob_gas(tx)) * Uint(
            tx.max_fee_per_blob_gas
        )
        blob_versioned_hashes = tx.blob_versioned_hashes
    else:
        blob_versioned_hashes = ()

    if isinstance(tx, (BlobTransaction, SetCodeTransaction)):
        if not isinstance(tx.to, Address):
            raise TransactionTypeContractCreationError(tx)

    if isinstance(tx, SetCodeTransaction):
        if not any(tx.authorizations):
            raise EmptyAuthorizationListError("empty authorization list")

    if sender_account.nonce > Uint(tx.nonce):
        raise NonceMismatchError("nonce too low")
    elif sender_account.nonce < Uint(tx.nonce):
        raise NonceMismatchError("nonce too high")

    if Uint(sender_account.balance) < max_gas_fee + Uint(tx.value):
        raise InsufficientBalanceError("insufficient sender balance")
    sender_code = get_code(tx_state, sender_account.code_hash)
    if sender_account.code_hash != EMPTY_CODE_HASH and not is_valid_delegation(
        sender_code
    ):
        raise InvalidSenderError("not EOA")

    return (
        effective_gas_price,
        blob_versioned_hashes,
        tx_blob_gas_used,
    )


def make_receipt(
    tx: Transaction,
    error: Optional[EthereumException],
    cumulative_gas_used: Uint,
    logs: Tuple[Log, ...],
) -> Bytes | Receipt:
    """Make a receipt for an executed transaction."""
    receipt = Receipt(
        succeeded=error is None,
        cumulative_gas_used=cumulative_gas_used,
        bloom=logs_bloom(logs),
        logs=logs,
    )
    return encode_receipt(tx, receipt)


def process_checked_system_transaction(
    block_env: vm.BlockEnvironment,
    target_address: Address,
    data: Bytes,
) -> MessageCallOutput:
    """Process a system transaction and require executable target code."""
    untracked_state = TransactionState(parent=block_env.state)
    system_contract_code = get_code(
        untracked_state,
        get_account(untracked_state, target_address).code_hash,
    )

    if len(system_contract_code) == 0:
        raise InvalidBlock(
            f"System contract address {target_address.hex()} "
            "does not contain code"
        )

    system_tx_output = process_unchecked_system_transaction(
        block_env,
        target_address,
        data,
    )
    if system_tx_output.error:
        raise InvalidBlock(
            f"System contract ({target_address.hex()}) call failed: "
            f"{system_tx_output.error}"
        )
    return system_tx_output


def process_unchecked_system_transaction(
    block_env: vm.BlockEnvironment,
    target_address: Address,
    data: Bytes,
) -> MessageCallOutput:
    """Process a system transaction without pre- or post-execution checks."""
    system_tx_state = TransactionState(parent=block_env.state)
    system_contract_code = get_code(
        system_tx_state,
        get_account(system_tx_state, target_address).code_hash,
    )

    tx_env = vm.TransactionEnvironment(
        origin=SYSTEM_ADDRESS,
        recipient=target_address,
        value=U256(0),
        gas_price=block_env.base_fee_per_gas,
        gas=SYSTEM_TRANSACTION_GAS,
        state_gas_reservoir=(
            StateGasCosts.STORAGE_SET * SYSTEM_MAX_SSTORES_PER_CALL
        ),
        access_list_addresses=set(),
        access_list_storage_keys=set(),
        state=system_tx_state,
        blob_versioned_hashes=(),
        authorizations=(),
        index_in_block=None,
        tx_hash=None,
    )

    system_tx_message = Message(
        block_env=block_env,
        tx_env=tx_env,
        caller=SYSTEM_ADDRESS,
        target=target_address,
        gas=SYSTEM_TRANSACTION_GAS,
        state_gas_reservoir=(
            StateGasCosts.STORAGE_SET * SYSTEM_MAX_SSTORES_PER_CALL
        ),
        value=U256(0),
        data=data,
        code=system_contract_code,
        depth=Uint(0),
        current_target=target_address,
        code_address=target_address,
        should_transfer_value=False,
        is_static=False,
        accessed_addresses=set(),
        accessed_storage_keys=set(),
        disable_precompiles=False,
        parent_evm=None,
    )

    system_tx_output = process_message_call(system_tx_message)
    incorporate_tx_into_block(
        system_tx_state, block_env.block_access_list_builder
    )
    return system_tx_output


def apply_body(
    block_env: vm.BlockEnvironment,
    transactions: Tuple[LegacyTransaction | Bytes, ...],
    withdrawals: Tuple[Withdrawal, ...],
) -> vm.BlockOutput:
    """Execute a block body."""
    block_output = vm.BlockOutput()

    process_unchecked_system_transaction(
        block_env=block_env,
        target_address=BEACON_ROOTS_ADDRESS,
        data=block_env.parent_beacon_block_root,
    )
    process_unchecked_system_transaction(
        block_env=block_env,
        target_address=HISTORY_STORAGE_ADDRESS,
        data=block_env.block_hashes[-1],
    )

    for i, tx in enumerate(map(decode_transaction, transactions)):
        process_transaction(block_env, block_output, tx, Uint(i))

    block_env.block_access_list_builder.block_access_index = BlockAccessIndex(
        ulen(transactions) + Uint(1)
    )
    process_withdrawals(block_env, block_output, withdrawals)
    process_general_purpose_requests(
        block_env=block_env,
        block_output=block_output,
    )

    block_output.block_access_list = build_block_access_list(
        block_env.block_access_list_builder, block_env.state
    )
    validate_block_access_list_gas_limit(
        block_access_list=block_output.block_access_list,
        block_gas_limit=block_env.block_gas_limit,
    )
    return block_output


def process_general_purpose_requests(
    block_env: vm.BlockEnvironment,
    block_output: vm.BlockOutput,
) -> None:
    """Process general-purpose execution requests in ascending type order."""
    deposit_requests = parse_deposit_requests(block_output)
    requests_from_execution = block_output.requests
    if len(deposit_requests) > 0:
        requests_from_execution.append(DEPOSIT_REQUEST_TYPE + deposit_requests)

    system_withdrawal_tx_output = process_checked_system_transaction(
        block_env=block_env,
        target_address=WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS,
        data=b"",
    )
    if len(system_withdrawal_tx_output.return_data) > 0:
        requests_from_execution.append(
            WITHDRAWAL_REQUEST_TYPE + system_withdrawal_tx_output.return_data
        )

    system_consolidation_tx_output = process_checked_system_transaction(
        block_env=block_env,
        target_address=CONSOLIDATION_REQUEST_PREDEPLOY_ADDRESS,
        data=b"",
    )
    if len(system_consolidation_tx_output.return_data) > 0:
        requests_from_execution.append(
            CONSOLIDATION_REQUEST_TYPE
            + system_consolidation_tx_output.return_data
        )

    system_builder_deposit_tx_output = process_checked_system_transaction(
        block_env=block_env,
        target_address=BUILDER_DEPOSIT_CONTRACT_ADDRESS,
        data=b"",
    )
    if len(system_builder_deposit_tx_output.return_data) > 0:
        requests_from_execution.append(
            BUILDER_DEPOSIT_REQUEST_TYPE
            + system_builder_deposit_tx_output.return_data
        )

    system_builder_exit_tx_output = process_checked_system_transaction(
        block_env=block_env,
        target_address=BUILDER_EXIT_CONTRACT_ADDRESS,
        data=b"",
    )
    if len(system_builder_exit_tx_output.return_data) > 0:
        requests_from_execution.append(
            BUILDER_EXIT_REQUEST_TYPE
            + system_builder_exit_tx_output.return_data
        )


def process_transaction(
    block_env: vm.BlockEnvironment,
    block_output: vm.BlockOutput,
    tx: Transaction,
    index: Uint,
) -> None:
    """Execute a transaction against the provided block environment."""
    block_env.block_access_list_builder.block_access_index = BlockAccessIndex(
        index + Uint(1)
    )
    tx_state = TransactionState(parent=block_env.state)

    trie_set(
        block_output.transactions_trie,
        rlp.encode(index),
        encode_transaction(tx),
    )

    tx_chain_id = chain_id(tx)
    if tx_chain_id is not None and tx_chain_id != block_env.chain_id:
        raise WrongChainIdError(
            expected=block_env.chain_id,
            actual=tx_chain_id,
        )

    sender = recover_sender(tx)
    intrinsic = validate_transaction(tx, sender)
    (
        effective_gas_price,
        blob_versioned_hashes,
        tx_blob_gas_used,
    ) = check_transaction(
        block_env=block_env,
        block_output=block_output,
        tx=tx,
        sender=sender,
        tx_state=tx_state,
    )

    sender_account = get_account(tx_state, sender)
    if isinstance(tx, BlobTransaction):
        blob_gas_fee = calculate_data_fee_for_slot(
            block_env.excess_blob_gas,
            tx,
            block_env.slot_number,
        )
    else:
        blob_gas_fee = Uint(0)

    effective_gas_fee = tx.gas * effective_gas_price
    allocation = allocate_execution_gas(tx.gas, intrinsic)

    increment_nonce(tx_state, sender)
    sender_balance_after_gas_fee = (
        Uint(sender_account.balance) - effective_gas_fee - blob_gas_fee
    )
    set_account_balance(tx_state, sender, U256(sender_balance_after_gas_fee))

    access_list_addresses = {block_env.coinbase}
    access_list_storage_keys = set()
    if has_access_list(tx):
        for access in tx.access_list:
            access_list_addresses.add(access.account)
            for slot in access.slots:
                access_list_storage_keys.add((access.account, slot))

    authorizations: Tuple[Authorization, ...] = ()
    if isinstance(tx, SetCodeTransaction):
        authorizations = tx.authorizations

    tx_env = vm.TransactionEnvironment(
        origin=sender,
        recipient=tx.to,
        value=tx.value,
        gas_price=effective_gas_price,
        gas=allocation.regular_gas,
        state_gas_reservoir=allocation.state_gas_reservoir,
        access_list_addresses=access_list_addresses,
        access_list_storage_keys=access_list_storage_keys,
        state=tx_state,
        blob_versioned_hashes=blob_versioned_hashes,
        authorizations=authorizations,
        index_in_block=index,
        tx_hash=get_transaction_hash(encode_transaction(tx)),
    )

    message = prepare_message(block_env, tx_env, tx)
    tx_output = process_message_call(message)
    settlement = settle_transaction_gas(
        tx.gas,
        intrinsic,
        tx_output.gas_left,
        tx_output.state_gas_left,
        tx_output.refund_counter,
        tx_output.state_gas_used,
    )

    gas_refund_amount = settlement.gas_left * effective_gas_price
    priority_fee_per_gas = effective_gas_price - block_env.base_fee_per_gas
    transaction_fee = settlement.gas_used * priority_fee_per_gas

    create_ether(tx_state, sender, U256(gas_refund_amount))
    create_ether(tx_state, block_env.coinbase, U256(transaction_fee))

    block_output.block_gas_used += settlement.regular_gas_used
    block_output.block_state_gas_used += settlement.state_gas_used
    block_output.blob_gas_used += tx_blob_gas_used
    block_output.cumulative_gas_used += settlement.gas_used

    receipt = make_receipt(
        tx, tx_output.error, block_output.cumulative_gas_used, tx_output.logs
    )
    receipt_key = rlp.encode(Uint(index))
    block_output.receipt_keys += (receipt_key,)
    trie_set(
        block_output.receipts_trie,
        receipt_key,
        receipt,
    )
    block_output.block_logs += tx_output.logs

    for address in tx_output.accounts_to_delete:
        clear_account_preserving_balance(tx_state, address)

    incorporate_tx_into_block(tx_state, block_env.block_access_list_builder)


def process_withdrawals(
    block_env: vm.BlockEnvironment,
    block_output: vm.BlockOutput,
    withdrawals: Tuple[Withdrawal, ...],
) -> None:
    """Increase balances for withdrawals and record their trie entries."""
    wd_state = TransactionState(parent=block_env.state)
    for i, wd in enumerate(withdrawals):
        trie_set(
            block_output.withdrawals_trie,
            rlp.encode(Uint(i)),
            rlp.encode(wd),
        )
        create_ether(wd_state, wd.address, wd.amount * GWEI_TO_WEI)
    incorporate_tx_into_block(wd_state, block_env.block_access_list_builder)


def check_gas_limit(gas_limit: Uint, parent_gas_limit: Uint) -> bool:
    """Validate a block gas limit against its reference value."""
    max_adjustment_delta = parent_gas_limit // GasCosts.LIMIT_ADJUSTMENT_FACTOR
    if gas_limit >= parent_gas_limit + max_adjustment_delta:
        return False
    if gas_limit <= parent_gas_limit - max_adjustment_delta:
        return False
    if gas_limit < GasCosts.LIMIT_MINIMUM:
        return False
    return True
