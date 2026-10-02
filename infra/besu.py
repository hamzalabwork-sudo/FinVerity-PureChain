"""Baseline 2: Hyperledger Besu QBFT permissioned network (N validators) in Docker.

Parameters are aligned with PureChain where the protocols allow it: 30M block gas limit,
1-second block period, zero gas price, same Solidity contract (EVM 'paris').

    python -m infra.besu up 4      # (re)create a 4-validator network
    python -m infra.besu down
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

from finverity.config import BESU, ROOT

IMAGE = "hyperledger/besu:24.12.2"
NET_DIR = ROOT / "infra" / "besu_network"
STATE = NET_DIR / "current.json"
SUBNET = "172.28.0"
DOCKER_NET = "finverity-besu"
# Every validator gets the same CPU quota in every configuration, so validator-count scaling
# is not confounded by contention on the single test host (12 logical CPUs).
CPUS_PER_VALIDATOR = os.environ.get("BESU_CPUS_PER_VALIDATOR", "1.0")


def _docker(*args, check=True, capture=False):
    return subprocess.run(["docker", *args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)


def _generate(n: int) -> list[dict]:
    if NET_DIR.exists():
        shutil.rmtree(NET_DIR)
    NET_DIR.mkdir(parents=True)
    cfg = {
        "genesis": {
            "config": {
                "chainId": BESU.chain_id, "berlinBlock": 0, "londonBlock": 0, "shanghaiTime": 0,
                "zeroBaseFee": True,
                "qbft": {"blockperiodseconds": 1, "epochlength": 30000, "requesttimeoutseconds": 4},
            },
            "nonce": "0x0", "timestamp": "0x0", "gasLimit": "0x1C9C380", "difficulty": "0x1",
            "mixHash": "0x63746963616c2062797a616e74696e65206661756c7420746f6c6572616e6365",
            "coinbase": "0x0000000000000000000000000000000000000000", "alloc": {},
        },
        "blockchain": {"nodes": {"generate": True, "count": n}},
    }
    (NET_DIR / "qbftConfigFile.json").write_text(json.dumps(cfg, indent=1))
    _docker("run", "--rm", "--entrypoint", "/opt/besu/bin/besu", "-v", f"{NET_DIR}:/data", IMAGE, "operator", "generate-blockchain-config",
            "--config-file=/data/qbftConfigFile.json", "--to=/data/networkFiles", "--private-key-file-name=key")
    keys = sorted((NET_DIR / "networkFiles" / "keys").iterdir())
    nodes = []
    for i, kd in enumerate(keys, start=1):
        pub = (kd / "key.pub").read_text().strip().removeprefix("0x")
        nd = NET_DIR / f"validator{i}"
        nd.mkdir()
        shutil.copy(kd / "key", nd / "key")
        nodes.append({"name": f"besu-validator{i}", "ip": f"{SUBNET}.{10 + i}", "pub": pub, "dir": nd,
                      "address": kd.name})
    shutil.copy(NET_DIR / "networkFiles" / "genesis.json", NET_DIR / "genesis.json")
    return nodes


def down() -> None:
    names = _docker("ps", "-a", "--filter", "name=besu-validator", "--format", "{{.Names}}", capture=True).stdout.split()
    if names:
        _docker("rm", "-f", *names, capture=True)
    STATE.unlink(missing_ok=True)


def up(n: int) -> dict:
    down()
    nodes = _generate(n)
    if _docker("network", "inspect", DOCKER_NET, check=False, capture=True).returncode != 0:
        _docker("network", "create", "--subnet", f"{SUBNET}.0/24", DOCKER_NET, capture=True)
    boot = f"enode://{nodes[0]['pub']}@{nodes[0]['ip']}:30303"
    for i, nd in enumerate(nodes):
        args = ["run", "-d", "--name", nd["name"], "--network", DOCKER_NET, "--ip", nd["ip"],
                "-e", "BESU_OPTS=-Xmx768m", f"--cpus={CPUS_PER_VALIDATOR}", "-v", f"{nd['dir']}:/opt/besu/node",
                "-v", f"{NET_DIR / 'genesis.json'}:/opt/besu/genesis.json:ro"]
        if i == 0:
            args += ["-p", "127.0.0.1:8545:8545"]
        args += [IMAGE, "--data-path=/opt/besu/node/data", "--genesis-file=/opt/besu/genesis.json",
                 "--node-private-key-file=/opt/besu/node/key", f"--p2p-host={nd['ip']}",
                 "--rpc-http-enabled", "--rpc-http-host=0.0.0.0", "--rpc-http-api=ETH,NET,QBFT,WEB3,TXPOOL",
                 "--host-allowlist=*", "--rpc-http-cors-origins=*", "--min-gas-price=0",
                 "--sync-min-peers=1", "--logging=WARN", "--rpc-http-max-active-connections=200",
                 "--tx-pool-max-future-by-sender=1000"]
        if i > 0:
            args.append(f"--bootnodes={boot}")
        _docker(*args, capture=True)
    _wait_ready(n)
    state = {"validators": n, "cpus_per_validator": CPUS_PER_VALIDATOR,
             "addresses": [nd["address"] for nd in nodes], "started": time.time()}
    STATE.write_text(json.dumps(state, indent=1))
    return state


def _rpc(method, params=None):
    return requests.post(BESU.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
                         timeout=5).json()["result"]


def _wait_ready(n: int, timeout: float = 240) -> None:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            vals = _rpc("qbft_getValidatorsByBlockNumber", ["latest"])
            peers = int(_rpc("net_peerCount"), 16)
            head = int(_rpc("eth_blockNumber"), 16)
            if len(vals) == n and peers >= n - 1 and head >= 3:
                return
            last = (len(vals), peers, head)
        except Exception as e:
            last = repr(e)
        time.sleep(2)
    raise TimeoutError(f"Besu network not ready: {last}")


def ensure_network(n: int) -> dict:
    from infra import restart_stopped
    if STATE.exists() and json.loads(STATE.read_text()).get("validators") == n:
        restart_stopped("besu-validator")
    if STATE.exists():
        st = json.loads(STATE.read_text())
        running = _docker("ps", "--filter", "name=besu-validator", "--format", "{{.Names}}", capture=True).stdout.split()
        if st["validators"] == n and st.get("cpus_per_validator") == CPUS_PER_VALIDATOR and len(running) == n:
            try:
                _wait_ready(n, timeout=20)
                return st
            except TimeoutError:
                pass
    return up(n)


if __name__ == "__main__":
    if sys.argv[1] == "up":
        print(json.dumps(up(int(sys.argv[2]) if len(sys.argv) > 2 else 4), indent=1))
    elif sys.argv[1] == "down":
        down()
