"""Paths, network settings and model parameters shared by every module."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
EVIDENCE_STORE = ROOT / "data" / "evidence_store"
CONTRACTS = ROOT / "contracts"
BUILD = ROOT / "build"
RESULTS = ROOT / "results"

SOLC_VERSION = "0.8.19"
EVM_VERSION = "paris"  # portable across PureChain (Cancun) and Besu


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        from dotenv import load_dotenv
        load_dotenv(env)


def enable_system_trust() -> None:
    """Use the OS certificate store (required when an antivirus re-signs TLS traffic)."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass


_load_env()
enable_system_trust()


@dataclass(frozen=True)
class ChainConfig:
    name: str
    rpc_url: str
    chain_id: int
    gas_price: int = 0
    tx_gas_limit: int = 400_000
    fee_bearing: bool = False    # True only where the network charges users for gas (public-chain config)


PURECHAIN = ChainConfig(
    name="purechain",
    rpc_url=os.environ.get("PURECHAIN_RPC", "https://purechainnode.com:8547"),
    chain_id=int(os.environ.get("PURECHAIN_CHAIN_ID", "900520900520")),
)

BESU = ChainConfig(
    name="besu",
    rpc_url=os.environ.get("BESU_RPC", "http://127.0.0.1:8545"),
    chain_id=int(os.environ.get("BESU_CHAIN_ID", "2025")),
)

# PoA2 emulation calibrated to live PureChain (see infra/poa2.py); used while the public RPC is down.
POA2_LOCAL = ChainConfig(
    name="poa2_local",
    rpc_url=os.environ.get("POA2_LOCAL_RPC", "http://127.0.0.1:8565"),
    chain_id=int(os.environ.get("POA2_LOCAL_CHAIN_ID", "900521")),
    # upstream Geth forces a >= 1 wei price floor for transactions received from peers; 1 wei is
    # economically zero (~1.5e-13 ETH per record) and the network is treated as fee-free
    gas_price=1,
)

# Conventional public-chain configuration: Ethereum PoS devnet with mainnet timing.
# gas_price = -1 means "use eth_gasPrice from the node" (EIP-1559 base fee applies).
ETHPOS = ChainConfig(
    name="ethpos",
    rpc_url=os.environ.get("ETHPOS_RPC", "http://127.0.0.1:8555"),
    chain_id=int(os.environ.get("ETHPOS_CHAIN_ID", "32382")),
    gas_price=-1,
    fee_bearing=True,
)

# Reference prices used to express public-chain gas fees in USD (override for sensitivity analysis).
MAINNET_GAS_GWEI = float(os.environ.get("MAINNET_GAS_GWEI", "5"))
ETH_USD = float(os.environ.get("ETH_USD", "3000"))

POSTGRES_DSN =os.environ.get(
    "POSTGRES_DSN", "host=127.0.0.1 port=5433 dbname=finverity user=finverity password=finverity"
)


def admin_key() -> str:
    key = os.environ.get("PURECHAIN_PRIVATE_KEY")
    if not key:
        raise RuntimeError("PURECHAIN_PRIVATE_KEY missing from .env")
    return key


@dataclass(frozen=True)
class EnergyModel:
    """Linear host power model P(u) = P_idle + (P_max - P_idle) * u  (Fan et al., ISCA 2007).

    Only the dynamic share is attributed to the workload:
        E = P_dynamic * cpu_seconds / logical_cores
    Values are for the testbed CPU (Intel Core i5-12400F, 65 W base power, 12 threads)
    and can be overridden for sensitivity analysis.
    """
    p_dynamic_w: float = float(os.environ.get("ENERGY_P_DYNAMIC_W", "65"))
    logical_cores: int = os.cpu_count() or 12
    # Remote validator model (PureChain nodes cannot be metered from the client).
    validator_power_w: float = float(os.environ.get("ENERGY_VALIDATOR_W", "25"))
    # Grid carbon intensity; replace with the value from the reference you cite for Korea.
    grid_gco2_per_kwh: float = float(os.environ.get("GRID_GCO2_PER_KWH", "436"))


@dataclass(frozen=True)
class CostModel:
    """Unit prices for C_tx = C_compute + C_storage + C_network + C_fee."""
    electricity_usd_per_kwh: float = 0.12
    storage_usd_per_gb_month: float = 0.023     # object storage for off-chain evidence
    chain_storage_usd_per_gb_month: float = 0.08  # block storage (SSD) per full node
    network_usd_per_gb: float = 0.09
    retention_months: int = 84                   # 7-year audit retention


ENERGY = EnergyModel()
COST = CostModel()

# Evidence size presets (bytes of attached supporting documents)
EVIDENCE_SIZES = {
    "none": 0,
    "small": 10 * 1024,
    "medium": 100 * 1024,
    "large": 1024 * 1024,
    "xlarge": 5 * 1024 * 1024,
}
