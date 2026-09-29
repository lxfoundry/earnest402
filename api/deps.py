"""One assembled bundle of collaborators, constructor-injected everywhere.

Nothing below api/server.py reads the environment or opens a network
connection, so every module in this package can be tested with a fake in place
of a network. `build_deps` is the assembly api/server.py runs, taking the algod
client it is handed rather than building one.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from algosdk import account, mnemonic
from algosdk.atomic_transaction_composer import AccountTransactionSigner

from api.config import Settings, verify_before_assembly
from api.escrow import EscrowClient
from api.jobs import JobStore


@dataclass(frozen=True)
class Deps:
    """Frozen for the same reason Settings is: it is assembled once at boot.

    The collaborators themselves are mutable objects with their own state; what
    is fixed is *which* collaborator each name refers to. A later reassignment
    would mean part of the process is talking to a different store or a
    different chain client than the part that was checked at startup.
    """

    settings: Settings
    escrow: Any
    jobs: Any
    algod: Any
    http: Any
    clock: Any = time.time
    # Agreement creation reads agreement_count to name its box references, so
    # two concurrent creates would name the same id and the second would fail
    # on `roster box already exists`. One lock, held across the read and the
    # submit, is the whole fix.
    create_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def now(self) -> int:
        return int(self.clock())


def build_deps(settings: Settings, algod: Any) -> Deps:
    """The real collaborators for a configuration.

    Only `/pin` signs anything: its unpaid pass creates an agreement, and only
    `/pin` keeps job records. With that route off no key may be present, the
    escrow client is read-only, and no store is opened -- `/index` reads the
    chain and writes nothing. The guards on what the process may hold run
    first, so a refused configuration never decodes a key or opens a store.
    """
    verify_before_assembly(settings)
    if settings.pin_route_enabled:
        private_key = mnemonic.to_private_key(settings.admin_mnemonic)
        admin_address = account.address_from_private_key(private_key)
        admin_signer = AccountTransactionSigner(private_key)
        jobs = JobStore(settings.job_db_path)
    else:
        admin_address = ""
        admin_signer = None
        jobs = None

    return Deps(
        settings=settings,
        escrow=EscrowClient(
            algod,
            app_id=settings.app_id,
            app_account=settings.app_account,
            usdc_asa_id=settings.usdc_asa_id,
            admin_address=admin_address,
            admin_signer=admin_signer,
        ),
        jobs=jobs,
        algod=algod,
        http=None,
    )
