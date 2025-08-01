"""
Ethereum Virtual Machine (EVM)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. contents:: Table of Contents
    :backlinks: none
    :local:

Introduction
------------

The abstract computer which runs the code stored in an
`.fork_types.Account`.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from ethereum_types.bytes import Bytes, Bytes0, Bytes32
from ethereum_types.numeric import U64, U256, Uint

from ethereum.crypto.hash import Hash32
from ethereum.exceptions import EthereumException

from ..blocks import Log, Receipt, Withdrawal
from ..fork_types import Address, Authorization, VersionedHash
from ..state import State, TransientStorage
from ..transactions import LegacyTransaction
from ..trie import Trie

__all__ = ("Environment", "Evm", "Message")


@dataclass
class BlockEnvironment:
    """
    Items external to the virtual machine itself, provided by the environment.
    """

    chain_id: U64
    state: State
    block_gas_limit: Uint
    block_hashes: List[Hash32]
    coinbase: Address
    number: Uint
    base_fee_per_gas: Uint
    time: U256
    prev_randao: Bytes32
    excess_blob_gas: U64
    parent_beacon_block_root: Hash32


@dataclass
class BlockOutput:
    """
    Output from applying the block body to the present state.

    Contains the following:

    block_gas_used : `ethereum.base_types.Uint`
        Gas used for executing all transactions.
    transactions_trie : `ethereum.fork_types.Root`
        Trie of all the transactions in the block.
    receipts_trie : `ethereum.fork_types.Root`
        Trie root of all the receipts in the block.
    receipt_keys :
        Key of all the receipts in the block.
    block_logs : `Bloom`
        Logs bloom of all the logs included in all the transactions of the
        block.
    withdrawals_trie : `ethereum.fork_types.Root`
        Trie root of all the withdrawals in the block.
    blob_gas_used : `ethereum.base_types.U64`
        Total blob gas used in the block.
    requests : `Bytes`
        Hash of all the requests in the block.
    """

    block_gas_used: Uint = Uint(0)
    transactions_trie: Trie[
        Bytes, Optional[Bytes | LegacyTransaction]
    ] = field(default_factory=lambda: Trie(secured=False, default=None))
    receipts_trie: Trie[Bytes, Optional[Bytes | Receipt]] = field(
        default_factory=lambda: Trie(secured=False, default=None)
    )
    receipt_keys: Tuple[Bytes, ...] = field(default_factory=tuple)
    block_logs: Tuple[Log, ...] = field(default_factory=tuple)
    withdrawals_trie: Trie[Bytes, Optional[Bytes | Withdrawal]] = field(
        default_factory=lambda: Trie(secured=False, default=None)
    )
    blob_gas_used: U64 = U64(0)
    requests: List[Bytes] = field(default_factory=list)


@dataclass
class TransactionEnvironment:
    """
    Items that are used by contract creation or message call.
    """

    origin: Address
    gas_price: Uint
    gas: Uint
    access_list_addresses: Set[Address]
    access_list_storage_keys: Set[Tuple[Address, Bytes32]]
    transient_storage: TransientStorage
    blob_versioned_hashes: Tuple[VersionedHash, ...]
    authorizations: Tuple[Authorization, ...]
    index_in_block: Optional[Uint]
    tx_hash: Optional[Hash32]
    traces: List[dict]


@dataclass
class Message:
    """
    Items that are used by contract creation or message call.
    """

    block_env: BlockEnvironment
    tx_env: TransactionEnvironment
    caller: Address
    target: Bytes0 | Address
    current_target: Address
    gas: Uint
    value: U256
    data: Bytes
    code_address: Optional[Address]
    code: Bytes
    depth: Uint
    should_transfer_value: bool
    is_static: bool
    accessed_addresses: Set[Address]
    accessed_storage_keys: Set[Tuple[Address, Bytes32]]
    disable_precompiles: bool
    warm_code_addresses: Set[Address]
    parent_evm: Optional["Evm"]


@dataclass
class Evm:
    """The internal state of the virtual machine."""

    pc: Uint
    stack: List[U256]
    memory: bytearray
    code: Bytes
    gas_left: Uint
    valid_jump_destinations: Set[Uint]
    logs: Tuple[Log, ...]
    refund_counter: int
    running: bool
    message: Message
    output: Bytes
    accounts_to_delete: Set[Address]
    return_data: Bytes
    error: Optional[EthereumException]
    accessed_addresses: Set[Address]
    accessed_storage_keys: Set[Tuple[Address, Bytes32]]
    warm_code_addresses: Set[Address]


def incorporate_child_on_success(evm: Evm, child_evm: Evm) -> None:
    """
    Incorporate the state of a successful `child_evm` into the parent `evm`.

    Parameters
    ----------
    evm :
        The parent `EVM`.
    child_evm :
        The child evm to incorporate.
    """
    evm.gas_left += child_evm.gas_left
    evm.logs += child_evm.logs
    evm.refund_counter += child_evm.refund_counter
    evm.accounts_to_delete.update(child_evm.accounts_to_delete)
    evm.accessed_addresses.update(child_evm.accessed_addresses)
    evm.accessed_storage_keys.update(child_evm.accessed_storage_keys)
    evm.warm_code_addresses.update(child_evm.warm_code_addresses)


def incorporate_child_on_error(evm: Evm, child_evm: Evm) -> None:
    """
    Incorporate the state of an unsuccessful `child_evm` into the parent `evm`.

    Parameters
    ----------
    evm :
        The parent `EVM`.
    child_evm :
        The child evm to incorporate.
    """
    evm.gas_left += child_evm.gas_left
