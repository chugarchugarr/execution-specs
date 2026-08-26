"""
Ethereum Virtual Machine (EVM) Environmental Instructions.

.. contents:: Table of Contents
    :backlinks: none
    :local:

Introduction
------------

Implementations of the EVM environment related instructions.
"""

from ethereum_types.bytes import Bytes32
from ethereum_types.numeric import U256, Uint, ulen

from ethereum.state import EMPTY_ACCOUNT
from ethereum.utils.numeric import ceil32

from ...slot_timing import calculate_blob_gas_price_for_slot
from ...state_tracker import get_account, get_code
from ...utils.address import to_address_masked
from ...vm.memory import buffer_read, memory_write
from .. import Evm
from ..exceptions import OutOfBoundsRead
from ..gas import GasCosts, calculate_gas_extend_memory, charge_gas
from ..stack import pop, push


def address(evm: Evm) -> None:
    """Push the address of the current executing account to the stack."""
    charge_gas(evm, GasCosts.OPCODE_ADDRESS)
    push(evm.stack, U256.from_be_bytes(evm.message.current_target))
    evm.pc += Uint(1)


def balance(evm: Evm) -> None:
    """Push the balance of the given account onto the stack."""
    address = to_address_masked(pop(evm.stack))
    if address in evm.accessed_addresses:
        charge_gas(evm, GasCosts.WARM_ACCESS)
    else:
        evm.accessed_addresses.add(address)
        charge_gas(evm, GasCosts.COLD_ACCOUNT_ACCESS)
    tx_state = evm.message.tx_env.state
    balance = get_account(tx_state, address).balance
    push(evm.stack, balance)
    evm.pc += Uint(1)


def origin(evm: Evm) -> None:
    """Push the original transaction sender to the stack."""
    charge_gas(evm, GasCosts.OPCODE_ORIGIN)
    push(evm.stack, U256.from_be_bytes(evm.message.tx_env.origin))
    evm.pc += Uint(1)


def caller(evm: Evm) -> None:
    """Push the address of the caller onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_CALLER)
    push(evm.stack, U256.from_be_bytes(evm.message.caller))
    evm.pc += Uint(1)


def callvalue(evm: Evm) -> None:
    """Push the value sent with the call onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_CALLVALUE)
    push(evm.stack, evm.message.value)
    evm.pc += Uint(1)


def calldataload(evm: Evm) -> None:
    """Push a word of the current call data onto the stack."""
    start_index = pop(evm.stack)
    charge_gas(evm, GasCosts.OPCODE_CALLDATALOAD)
    value = buffer_read(evm.message.data, start_index, U256(32))
    push(evm.stack, U256.from_be_bytes(value))
    evm.pc += Uint(1)


def calldatasize(evm: Evm) -> None:
    """Push the size of the current call data onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_CALLDATASIZE)
    push(evm.stack, U256(len(evm.message.data)))
    evm.pc += Uint(1)


def calldatacopy(evm: Evm) -> None:
    """Copy a portion of call data to memory."""
    memory_start_index = pop(evm.stack)
    data_start_index = pop(evm.stack)
    size = pop(evm.stack)
    words = ceil32(Uint(size)) // Uint(32)
    copy_gas_cost = GasCosts.OPCODE_COPY_PER_WORD * words
    extend_memory = calculate_gas_extend_memory(
        evm.memory, [(memory_start_index, size)]
    )
    charge_gas(
        evm,
        GasCosts.OPCODE_CALLDATACOPY_BASE + copy_gas_cost + extend_memory.cost,
    )
    evm.memory += b"\x00" * extend_memory.expand_by
    value = buffer_read(evm.message.data, data_start_index, size)
    memory_write(evm.memory, memory_start_index, value)
    evm.pc += Uint(1)


def codesize(evm: Evm) -> None:
    """Push the size of the current code onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_CODESIZE)
    push(evm.stack, U256(len(evm.code)))
    evm.pc += Uint(1)


def codecopy(evm: Evm) -> None:
    """Copy a portion of current code to memory."""
    memory_start_index = pop(evm.stack)
    code_start_index = pop(evm.stack)
    size = pop(evm.stack)
    words = ceil32(Uint(size)) // Uint(32)
    copy_gas_cost = GasCosts.OPCODE_COPY_PER_WORD * words
    extend_memory = calculate_gas_extend_memory(
        evm.memory, [(memory_start_index, size)]
    )
    charge_gas(
        evm,
        GasCosts.OPCODE_CODECOPY_BASE + copy_gas_cost + extend_memory.cost,
    )
    evm.memory += b"\x00" * extend_memory.expand_by
    value = buffer_read(evm.code, code_start_index, size)
    memory_write(evm.memory, memory_start_index, value)
    evm.pc += Uint(1)


def gasprice(evm: Evm) -> None:
    """Push the transaction gas price onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_GASPRICE)
    push(evm.stack, U256(evm.message.tx_env.gas_price))
    evm.pc += Uint(1)


def extcodesize(evm: Evm) -> None:
    """Push the code size of a given account onto the stack."""
    address = to_address_masked(pop(evm.stack))
    if address in evm.accessed_addresses:
        access_gas_cost = GasCosts.WARM_ACCESS
    else:
        evm.accessed_addresses.add(address)
        access_gas_cost = GasCosts.COLD_ACCOUNT_ACCESS
    access_gas_cost += GasCosts.WARM_ACCESS
    charge_gas(evm, access_gas_cost)
    tx_state = evm.message.tx_env.state
    code_hash = get_account(tx_state, address).code_hash
    code = get_code(tx_state, code_hash)
    push(evm.stack, U256(len(code)))
    evm.pc += Uint(1)


def extcodecopy(evm: Evm) -> None:
    """Copy a portion of an account's code to memory."""
    address = to_address_masked(pop(evm.stack))
    memory_start_index = pop(evm.stack)
    code_start_index = pop(evm.stack)
    size = pop(evm.stack)
    words = ceil32(Uint(size)) // Uint(32)
    copy_gas_cost = GasCosts.OPCODE_COPY_PER_WORD * words
    extend_memory = calculate_gas_extend_memory(
        evm.memory, [(memory_start_index, size)]
    )
    if address in evm.accessed_addresses:
        access_gas_cost = GasCosts.WARM_ACCESS
    else:
        evm.accessed_addresses.add(address)
        access_gas_cost = GasCosts.COLD_ACCOUNT_ACCESS
    access_gas_cost += GasCosts.WARM_ACCESS
    charge_gas(evm, access_gas_cost + copy_gas_cost + extend_memory.cost)
    evm.memory += b"\x00" * extend_memory.expand_by
    tx_state = evm.message.tx_env.state
    code_hash = get_account(tx_state, address).code_hash
    code = get_code(tx_state, code_hash)
    value = buffer_read(code, code_start_index, size)
    memory_write(evm.memory, memory_start_index, value)
    evm.pc += Uint(1)


def returndatasize(evm: Evm) -> None:
    """Push the size of the return data buffer onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_RETURNDATASIZE)
    push(evm.stack, U256(len(evm.return_data)))
    evm.pc += Uint(1)


def returndatacopy(evm: Evm) -> None:
    """Copy data from the return-data buffer to memory."""
    memory_start_index = pop(evm.stack)
    return_data_start_position = pop(evm.stack)
    size = pop(evm.stack)
    words = ceil32(Uint(size)) // Uint(32)
    copy_gas_cost = GasCosts.OPCODE_RETURNDATACOPY_PER_WORD * words
    extend_memory = calculate_gas_extend_memory(
        evm.memory, [(memory_start_index, size)]
    )
    charge_gas(
        evm,
        GasCosts.OPCODE_RETURNDATACOPY_BASE
        + copy_gas_cost
        + extend_memory.cost,
    )
    if Uint(return_data_start_position) + Uint(size) > ulen(evm.return_data):
        raise OutOfBoundsRead
    evm.memory += b"\x00" * extend_memory.expand_by
    value = evm.return_data[
        return_data_start_position : return_data_start_position + size
    ]
    memory_write(evm.memory, memory_start_index, value)
    evm.pc += Uint(1)


def extcodehash(evm: Evm) -> None:
    """Push the keccak256 hash of an account's bytecode."""
    address = to_address_masked(pop(evm.stack))
    if address in evm.accessed_addresses:
        access_gas_cost = GasCosts.WARM_ACCESS
    else:
        evm.accessed_addresses.add(address)
        access_gas_cost = GasCosts.COLD_ACCOUNT_ACCESS
    charge_gas(evm, access_gas_cost)
    tx_state = evm.message.tx_env.state
    account = get_account(tx_state, address)
    if account == EMPTY_ACCOUNT:
        codehash = U256(0)
    else:
        codehash = U256.from_be_bytes(account.code_hash)
    push(evm.stack, codehash)
    evm.pc += Uint(1)


def self_balance(evm: Evm) -> None:
    """Push the current account balance onto the stack."""
    charge_gas(evm, GasCosts.FAST_STEP)
    balance = get_account(
        evm.message.tx_env.state, evm.message.current_target
    ).balance
    push(evm.stack, balance)
    evm.pc += Uint(1)


def base_fee(evm: Evm) -> None:
    """Push the current block base fee onto the stack."""
    charge_gas(evm, GasCosts.OPCODE_BASEFEE)
    push(evm.stack, U256(evm.message.block_env.base_fee_per_gas))
    evm.pc += Uint(1)


def blob_hash(evm: Evm) -> None:
    """Push the versioned blob hash at an index onto the stack."""
    index = pop(evm.stack)
    charge_gas(evm, GasCosts.OPCODE_BLOBHASH)
    if int(index) < len(evm.message.tx_env.blob_versioned_hashes):
        blob_hash = evm.message.tx_env.blob_versioned_hashes[index]
    else:
        blob_hash = Bytes32(b"\x00" * 32)
    push(evm.stack, U256.from_be_bytes(blob_hash))
    evm.pc += Uint(1)


def blob_base_fee(evm: Evm) -> None:
    """Push the blob base fee for the current slot-duration era."""
    charge_gas(evm, GasCosts.OPCODE_BLOBBASEFEE)
    block_env = evm.message.block_env
    blob_base_fee = calculate_blob_gas_price_for_slot(
        block_env.excess_blob_gas,
        block_env.slot_number,
    )
    push(evm.stack, U256(blob_base_fee))
    evm.pc += Uint(1)
