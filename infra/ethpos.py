"""Baseline 3: conventional public-chain configuration - Ethereum proof-of-stake devnet.

Geth v1.13.15 (the same execution-client line PureChain is derived from) + Prysm beacon chain
and validator (64 interop validators), with Ethereum *mainnet timing*: 12 s slots, 32 slots
per epoch, finality by Casper FFG checkpoints. EIP-1559 fees apply (non-zero gas price).
Based on the OffchainLabs/eth-pos-devnet reference setup.

    python -m infra.ethpos up | down
"""
from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

from finverity import accounts
from finverity.config import ETHPOS, ROOT

DIR = ROOT / "infra" / "ethpos_network"
REF = ROOT / "infra" / "ethpos_ref"
GETH = "ethereum/client-go:v1.13.15"
PRYSMCTL = "gcr.io/prysmaticlabs/prysm/cmd/prysmctl:v5.0.4"
BEACON = "gcr.io/prysmaticlabs/prysm/beacon-chain:v5.0.4"
VALIDATOR = "gcr.io/prysmaticlabs/prysm/validator:v5.0.4"
NET = "finverity-ethpos"
NAMES = ["ethpos-geth", "ethpos-beacon", "ethpos-validator"]
SLOTS_PER_EPOCH = 32
SECONDS_PER_SLOT = 12


def _docker(*args, check=True):
    r = subprocess.run(["docker", *args], text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}...: {r.stderr[-800:]}")
    return r


def down() -> None:
    _docker("rm", "-f", *NAMES, check=False)


def _prepare() -> None:
    if DIR.exists():
        shutil.rmtree(DIR)
    (DIR / "consensus").mkdir(parents=True)
    (DIR / "execution").mkdir(parents=True)
    cfg = (REF / "config.yml").read_text()
    cfg = cfg.replace("SLOTS_PER_EPOCH: 6", f"SLOTS_PER_EPOCH: {SLOTS_PER_EPOCH}")
    cfg = cfg.replace("DENEB_FORK_VERSION: 0x20000093", "DENEB_FORK_EPOCH: 0\nDENEB_FORK_VERSION: 0x20000093")
    (DIR / "consensus" / "config.yml").write_text(cfg)
    g = json.loads((REF / "genesis.json").read_text())
    g["config"]["chainId"] = ETHPOS.chain_id
    rs = accounts.roles(16)
    for a in [accounts.admin(), *rs.submitters]:
        g["alloc"][a.address[2:].lower()] = {"balance": hex(10**24)}   # 1M ETH each
    (DIR / "execution" / "genesis.json").write_text(json.dumps(g, indent=1))
    (DIR / "execution" / "jwtsecret").write_text("0x" + secrets.token_hex(32))
    (DIR / "execution" / "geth_password.txt").write_text("")


def up() -> dict:
    down()
    _prepare()
    if _docker("network", "inspect", NET, check=False).returncode != 0:
        _docker("network", "create", NET)
    c, e = f"{DIR / 'consensus'}:/consensus", f"{DIR / 'execution'}:/execution"
    _docker("run", "--rm", "-v", c, "-v", e, PRYSMCTL, "testnet", "generate-genesis", "--fork=deneb",
            "--num-validators=64", "--genesis-time-delay=20", "--output-ssz=/consensus/genesis.ssz",
            "--chain-config-file=/consensus/config.yml", "--geth-genesis-json-in=/execution/genesis.json",
            "--geth-genesis-json-out=/execution/genesis.json")
    _docker("run", "--rm", "-v", e, GETH, "--datadir=/execution", "init", "/execution/genesis.json")
    _docker("run", "-d", "--name", "ethpos-geth", "--network", NET, "-v", e, "-p", "127.0.0.1:8555:8545", GETH,
            "--http", "--http.api=eth,net,web3,txpool", "--http.addr=0.0.0.0", "--http.vhosts=*",
            "--authrpc.vhosts=*", "--authrpc.addr=0.0.0.0", "--authrpc.jwtsecret=/execution/jwtsecret",
            "--datadir=/execution", "--nodiscover", "--syncmode=full", "--txpool.accountslots=1024",
            "--txpool.globalslots=20000", "--txpool.accountqueue=1024", "--txpool.globalqueue=20000")
    _docker("run", "-d", "--name", "ethpos-beacon", "--network", NET, "-v", c, "-v", e, BEACON,
            "--datadir=/consensus/beacondata", "--min-sync-peers=0", "--genesis-state=/consensus/genesis.ssz",
            "--bootstrap-node=", "--interop-eth1data-votes", "--chain-config-file=/consensus/config.yml",
            "--contract-deployment-block=0", f"--chain-id={ETHPOS.chain_id}", "--rpc-host=0.0.0.0",
            "--grpc-gateway-host=0.0.0.0", "--execution-endpoint=http://ethpos-geth:8551", "--accept-terms-of-use",
            "--jwt-secret=/execution/jwtsecret", f"--suggested-fee-recipient={accounts.admin().address}",
            "--minimum-peers-per-subnet=0", "--force-clear-db")
    _docker("run", "-d", "--name", "ethpos-validator", "--network", NET, "-v", c, VALIDATOR,
            "--beacon-rpc-provider=ethpos-beacon:4000", "--datadir=/consensus/validatordata",
            "--accept-terms-of-use", "--interop-num-validators=64", "--interop-start-index=0",
            "--chain-config-file=/consensus/config.yml", "--force-clear-db")
    _wait_blocks(3)
    _wait_finalized()   # measure steady-state finality, not a fresh chain's first checkpoint
    return {"slots_per_epoch": SLOTS_PER_EPOCH, "seconds_per_slot": SECONDS_PER_SLOT, "validators": 64}


def _finalized() -> int:
    r = requests.post(ETHPOS.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                                            "params": ["finalized", False]}, timeout=5).json()
    return int(r["result"]["number"], 16) if r.get("result") else -1


def _wait_finalized(timeout: float = 45 * 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if _finalized() > 0:
                return
        except Exception:
            pass
        time.sleep(15)
    raise TimeoutError("PoS devnet did not finalize a checkpoint")


def healthy(window_s: float = 30) -> bool:
    """Head advances within one-two slots and a finalized checkpoint exists."""
    try:
        h0 = int(_rpc("eth_blockNumber"), 16)
        end = time.time() + window_s
        while time.time() < end:
            time.sleep(3)
            if int(_rpc("eth_blockNumber"), 16) > h0:
                return _finalized() > 0
        return False
    except Exception:
        return False


def _rpc(method, params=None):
    return requests.post(ETHPOS.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
                         timeout=5).json()["result"]


def _wait_blocks(n: int, timeout: float = 300) -> None:
    end, last = time.time() + timeout, None
    while time.time() < end:
        try:
            last = int(_rpc("eth_blockNumber"), 16)
            if last >= n:
                return
        except Exception as ex:
            last = repr(ex)
        time.sleep(3)
    logs = _docker("logs", "--tail", "20", "ethpos-beacon", check=False).stderr
    raise TimeoutError(f"PoS devnet not producing blocks (last={last}):\n{logs}")


def ensure() -> None:
    running = _docker("ps", "--format", "{{.Names}}", check=False).stdout.split()
    if all(n in running for n in NAMES) and healthy():
        return
    up()


if __name__ == "__main__":
    print(json.dumps(up() if sys.argv[1] == "up" else down(), indent=1))
