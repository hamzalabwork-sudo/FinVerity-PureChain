"""Backend factory."""
from __future__ import annotations

from .. import accounts
import os

from ..config import BESU, ETHPOS, POA2_LOCAL, PURECHAIN


def make_backend(name: str, **kw):
    if name == "purechain":
        from .evm import EvmBackend
        # PoA2 is Clique-derived: live forks up to depth 5 were observed, so finality = 6 confirmations
        depth = int(os.environ.get("PURECHAIN_FINALITY_DEPTH", "6"))
        return EvmBackend(PURECHAIN, accounts.admin(), recover_signer=True, n_validators=4,
                          finality="confirmations", finality_depth=depth, **kw)
    if name == "poa2_local":
        from .evm import EvmBackend
        from infra.poa2 import names
        n = kw.pop("validators", 4)
        depth = int(os.environ.get("PURECHAIN_FINALITY_DEPTH", "6"))
        return EvmBackend(POA2_LOCAL, accounts.admin(), recover_signer=True, containers=names(n), n_validators=n,
                          finality="confirmations", finality_depth=depth, **kw)
    if name == "ethpos":
        from .evm import EvmBackend
        from infra.ethpos import NAMES
        # energy attribution uses the same 4-node replication as the other blockchain configurations
        return EvmBackend(ETHPOS, accounts.admin(), containers=list(NAMES), n_validators=4,
                          finality="finalized", poll_s=0.25, **kw)
    if name == "besu":
        from .evm import EvmBackend
        n = kw.pop("validators", 4)
        return EvmBackend(BESU, accounts.admin(), containers=[f"besu-validator{i}" for i in range(1, n + 1)],
                          n_validators=n, **kw)
    if name in ("db_postgres", "db_sqlite"):
        from .database import DatabaseBackend
        return DatabaseBackend(engine=name.split("_", 1)[1])
    raise ValueError(f"unknown backend {name}")
