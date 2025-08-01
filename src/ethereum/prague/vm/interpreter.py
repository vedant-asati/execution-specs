"""
Ethereum Virtual Machine (EVM) Interpreter
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. contents:: Table of Contents
    :backlinks: none
    :local:

Introduction
------------

A straightforward interpreter that executes EVM code.
"""

from dataclasses import dataclass
from typing import Optional, Set, Tuple

from ethereum_types.bytes import Bytes, Bytes0
from ethereum_types.numeric import U256, Uint, ulen

from ethereum.exceptions import EthereumException
from ethereum.trace import (
    EvmStop,
    OpEnd,
    OpException,
    OpStart,
    PrecompileEnd,
    PrecompileStart,
    TransactionEnd,
    evm_trace,
)

from ..blocks import Log
from ..fork_types import Address
from ..state import (
    account_has_code_or_nonce,
    account_has_storage,
    begin_transaction,
    commit_transaction,
    destroy_storage,
    get_account,
    increment_nonce,
    mark_account_created,
    move_ether,
    rollback_transaction,
    set_code,
)
from ..vm import Message
from ..vm.eoa_delegation import get_delegated_code_address, set_delegation
from ..vm.gas import GAS_CODE_DEPOSIT, charge_gas
from ..vm.precompiled_contracts.mapping import PRE_COMPILED_CONTRACTS
from . import Evm
from .exceptions import (
    AddressCollision,
    ExceptionalHalt,
    InvalidContractPrefix,
    InvalidOpcode,
    OutOfGasError,
    Revert,
    StackDepthLimitError,
)
from .instructions import Ops, op_implementation
from .runtime import get_valid_jump_destinations

STACK_DEPTH_LIMIT = Uint(1024)
MAX_CODE_SIZE = 0x6000


@dataclass
class MessageCallOutput:
    """
    Output of a particular message call

    Contains the following:

          1. `gas_left`: remaining gas after execution.
          2. `refund_counter`: gas to refund after execution.
          3. `logs`: list of `Log` generated during execution.
          4. `accounts_to_delete`: Contracts which have self-destructed.
          5. `error`: The error from the execution if any.
          6. `return_data`: The output of the execution.
    """

    gas_left: Uint
    refund_counter: U256
    logs: Tuple[Log, ...]
    accounts_to_delete: Set[Address]
    error: Optional[EthereumException]
    return_data: Bytes


def process_message_call(message: Message) -> MessageCallOutput:
    """
    If `message.current` is empty then it creates a smart contract
    else it executes a call from the `message.caller` to the `message.target`.

    Parameters
    ----------
    message :
        Transaction specific items.

    Returns
    -------
    output : `MessageCallOutput`
        Output of the message call
    """
    block_env = message.block_env
    refund_counter = U256(0)
    if message.target == Bytes0(b""):
        is_collision = account_has_code_or_nonce(
            block_env.state, message.current_target
        ) or account_has_storage(block_env.state, message.current_target)
        if is_collision:
            return MessageCallOutput(
                Uint(0),
                U256(0),
                tuple(),
                set(),
                AddressCollision(),
                Bytes(b""),
            )
        else:
            evm = process_create_message(message)
    else:
        if message.tx_env.authorizations != ():
            refund_counter += set_delegation(message)

        delegated_address = get_delegated_code_address(message.code)
        if delegated_address is not None:
            message.disable_precompiles = True
            message.accessed_addresses.add(delegated_address)
            message.code = get_account(block_env.state, delegated_address).code
            message.code_address = delegated_address

        evm = process_message(message)

    if evm.error:
        logs: Tuple[Log, ...] = ()
        accounts_to_delete = set()
    else:
        logs = evm.logs
        accounts_to_delete = evm.accounts_to_delete
        refund_counter += U256(evm.refund_counter)

    tx_end = TransactionEnd(
        int(message.gas) - int(evm.gas_left), evm.output, evm.error
    )
    evm_trace(evm, tx_end)

    return MessageCallOutput(
        gas_left=evm.gas_left,
        refund_counter=refund_counter,
        logs=logs,
        accounts_to_delete=accounts_to_delete,
        error=evm.error,
        return_data=evm.output,
    )


def process_create_message(message: Message) -> Evm:
    """
    Executes a call to create a smart contract.

    Parameters
    ----------
    message :
        Transaction specific items.

    Returns
    -------
    evm: :py:class:`~ethereum.prague.vm.Evm`
        Items containing execution specific objects.
    """
    state = message.block_env.state
    transient_storage = message.tx_env.transient_storage
    # take snapshot of state before processing the message
    begin_transaction(state, transient_storage)

    # If the address where the account is being created has storage, it is
    # destroyed. This can only happen in the following highly unlikely
    # circumstances:
    # * The address created by a `CREATE` call collides with a subsequent
    #   `CREATE` or `CREATE2` call.
    # * The first `CREATE` happened before Spurious Dragon and left empty
    #   code.
    destroy_storage(state, message.current_target)

    # In the previously mentioned edge case the preexisting storage is ignored
    # for gas refund purposes. In order to do this we must track created
    # accounts. This tracking is also needed to respect the constraints
    # added to SELFDESTRUCT by EIP-6780.
    mark_account_created(state, message.current_target)

    increment_nonce(state, message.current_target)
    evm = process_message(message)
    if not evm.error:
        contract_code = evm.output
        contract_code_gas = Uint(len(contract_code)) * GAS_CODE_DEPOSIT
        try:
            if len(contract_code) > 0:
                if contract_code[0] == 0xEF:
                    raise InvalidContractPrefix
            charge_gas(evm, contract_code_gas)
            if len(contract_code) > MAX_CODE_SIZE:
                raise OutOfGasError
        except ExceptionalHalt as error:
            rollback_transaction(state, transient_storage)
            evm.gas_left = Uint(0)
            evm.output = b""
            evm.error = error
        else:
            set_code(state, message.current_target, contract_code)
            commit_transaction(state, transient_storage)
    else:
        rollback_transaction(state, transient_storage)
    return evm


def process_message(message: Message) -> Evm:
    """
    Move ether and execute the relevant code.

    Parameters
    ----------
    message :
        Transaction specific items.

    Returns
    -------
    evm: :py:class:`~ethereum.prague.vm.Evm`
        Items containing execution specific objects
    """
    state = message.block_env.state
    transient_storage = message.tx_env.transient_storage
    if message.depth > STACK_DEPTH_LIMIT:
        raise StackDepthLimitError("Stack depth limit reached")

    # take snapshot of state before processing the message
    begin_transaction(state, transient_storage)

    if message.should_transfer_value and message.value != 0:
        move_ether(
            state, message.caller, message.current_target, message.value
        )

    evm = execute_code(message)
    if evm.error:
        # revert state to the last saved checkpoint
        # since the message call resulted in an error
        rollback_transaction(state, transient_storage)
    else:
        commit_transaction(state, transient_storage)
    return evm


def execute_code(message: Message) -> Evm:
    """
    Executes bytecode present in the `message`.

    Parameters
    ----------
    message :
        Transaction specific items.

    Returns
    -------
    evm: `ethereum.vm.EVM`
        Items containing execution specific objects
    """
    code = message.code
    valid_jump_destinations = get_valid_jump_destinations(code)

    evm = Evm(
        pc=Uint(0),
        stack=[],
        memory=bytearray(),
        code=code,
        gas_left=message.gas,
        valid_jump_destinations=valid_jump_destinations,
        logs=(),
        refund_counter=0,
        running=True,
        message=message,
        output=b"",
        accounts_to_delete=set(),
        return_data=b"",
        error=None,
        accessed_addresses=message.accessed_addresses,
        accessed_storage_keys=message.accessed_storage_keys,
    )
    try:
        if evm.message.code_address in PRE_COMPILED_CONTRACTS:
            if message.disable_precompiles:
                return evm
            evm_trace(evm, PrecompileStart(evm.message.code_address))
            PRE_COMPILED_CONTRACTS[evm.message.code_address](evm)
            evm_trace(evm, PrecompileEnd())
            return evm

        while evm.running and evm.pc < ulen(evm.code):
            try:
                op = Ops(evm.code[evm.pc])
            except ValueError:
                raise InvalidOpcode(evm.code[evm.pc])

            evm_trace(evm, OpStart(op))
            op_implementation[op](evm)
            evm_trace(evm, OpEnd())

        evm_trace(evm, EvmStop(Ops.STOP))

    except ExceptionalHalt as error:
        evm_trace(evm, OpException(error))
        evm.gas_left = Uint(0)
        evm.output = b""
        evm.error = error
    except Revert as error:
        evm_trace(evm, OpException(error))
        evm.error = error
    return evm
