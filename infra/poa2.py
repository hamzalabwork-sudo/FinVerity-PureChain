"""PoA2 emulation calibrated to the live PureChain network (used while the public RPC is unavailable).

Live PureChain is a Geth v1.13.15 fork with Clique-style proof-of-authority seals (verified by
recovering block signers). This testbed runs *standard* Geth v1.13.15 with Clique, configured to the
parameters measured on PureChain:

    4 authorised signers (round-robin, in-turn / out-of-turn sealing)   - measured: 4
    1 s block period under load                                          - measured: ~1 s
    30,000,000 block gas limit                                           - measured: 30M
    gas price 1 wei (upstream Geth's minimum for peer propagation; fee-free) - measured: 0
    1 CPU per validator container (same as the Besu baseline)

Known differences from PureChain (report them): no zero-base-fee London/Cancun fork rules (Clique in
upstream Geth does not support Shanghai+), no "contextual association" component of PoA2, and
no WAN between client and nodes. Results from this testbed are labelled "PoA2 emulation".

    python -m infra.poa2 up | down
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import requests
from eth_account import Account

from finverity.config import POA2_LOCAL, ROOT

IMAGE = "ethereum/client-go:v1.13.15"
DIR = ROOT / "infra" / "poa2_network"
NET = "finverity-poa2"
SUBNET = "172.29.0"
N_SIGNERS = 4          # default = measured PureChain validator count
PERIOD_S = 1
CPUS = os.environ.get("POA2_CPUS_PER_VALIDATOR", "1.0")
NAMES = [f"poa2-signer{i}" for i in range(1, 11)]   # up to 10 signers (E5 sweep)


def _docker(*args, check=True):
    r = subprocess.run(["docker", *args], text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr[-600:]}")
    return r


def down() -> None:
    _docker("rm", "-f", *NAMES, check=False)


def names(n: int) -> list[str]:
    return NAMES[:n]


def _genesis(signers: list[str]) -> dict:
    from finverity import accounts
    extra = "0x" + "00" * 32 + "".join(a[2:].lower() for a in signers) + "00" * 65
    alloc = {a.address[2:].lower(): {"balance": hex(10**24)} for a in [accounts.admin(), *accounts.roles(16).submitters]}
    return {
        "config": {"chainId": POA2_LOCAL.chain_id, "homesteadBlock": 0, "eip150Block": 0, "eip155Block": 0,
                   "eip158Block": 0, "byzantiumBlock": 0, "constantinopleBlock": 0, "petersburgBlock": 0,
                   "istanbulBlock": 0, "berlinBlock": 0, "clique": {"period": PERIOD_S, "epoch": 30000}},
        "difficulty": "0x1", "gasLimit": hex(30_000_000), "extradata": extra, "alloc": alloc,
    }


def up(n: int = N_SIGNERS) -> dict:
    down()
    if DIR.exists():
        shutil.rmtree(DIR)
    DIR.mkdir(parents=True)
    keys = [Account.create() for _ in range(n)]
    node_keys = [os.urandom(32).hex() for _ in range(n)]
    (DIR / "genesis.json").write_text(json.dumps(_genesis([k.address for k in keys]), indent=1))
    (DIR / "password.txt").write_text("finverity")
    if _docker("network", "inspect", NET, check=False).returncode != 0:
        _docker("network", "create", "--subnet", f"{SUBNET}.0/24", NET)
    from eth_keys import keys as ekeys
    enodes = []
    for i, (k, nk) in enumerate(zip(keys, node_keys), start=1):
        d = DIR / f"signer{i}"
        (d / "keystore").mkdir(parents=True)
        (d / "keystore" / f"key-{k.address}.json").write_text(json.dumps(Account.encrypt(k.key, "finverity")))
        pub = ekeys.PrivateKey(bytes.fromhex(nk)).public_key.to_hex()[2:]
        enodes.append(f"enode://{pub}@{SUBNET}.{10 + i}:30303")
        _docker("run", "--rm", "-v", f"{d}:/data", "-v", f"{DIR / 'genesis.json'}:/genesis.json:ro", IMAGE,
                "--datadir=/data", "init", "/genesis.json")
    for i, (k, nk) in enumerate(zip(keys, node_keys), start=1):
        d = DIR / f"signer{i}"
        args = ["run", "-d", "--name", NAMES[i - 1], "--network", NET, "--ip", f"{SUBNET}.{10 + i}",
                f"--cpus={CPUS}", "-v", f"{d}:/data", "-v", f"{DIR / 'password.txt'}:/password.txt:ro"]
        if i == 1:
            args += ["-p", "127.0.0.1:8565:8545"]
        args += [IMAGE, "--datadir=/data", f"--networkid={POA2_LOCAL.chain_id}", f"--nodekeyhex={nk}",
                 "--port=30303", f"--bootnodes={','.join(e for j, e in enumerate(enodes) if j != i - 1)}",
                 f"--netrestrict={SUBNET}.0/24", "--syncmode=full",
                 "--mine", f"--miner.etherbase={k.address}", f"--unlock={k.address}", "--password=/password.txt",
                 "--allow-insecure-unlock", "--miner.gasprice=1", "--txpool.pricelimit=1",
                 "--miner.gaslimit=30000000", "--txpool.accountslots=1024", "--txpool.globalslots=20000",
                 "--txpool.accountqueue=1024", "--txpool.globalqueue=20000",
                 "--http", "--http.addr=0.0.0.0", "--http.vhosts=*", "--http.api=eth,net,web3,txpool,clique"]
        _docker(*args)
    _wait_ready(n)
    state = {"validators": n, "signers": [k.address for k in keys], "period_s": PERIOD_S, "cpus_per_validator": CPUS}
    (DIR / "current.json").write_text(json.dumps(state, indent=1))
    return state


def _rpc(method, params=None):
    return requests.post(POA2_LOCAL.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                    "params": params or []}, timeout=5).json()["result"]


def _wait_ready(n: int, timeout: float = 180) -> None:
    end, last = time.time() + timeout, None
    while time.time() < end:
        try:
            peers, head = int(_rpc("net_peerCount"), 16), int(_rpc("eth_blockNumber"), 16)
            last = (peers, head)
            if peers >= n - 1 and head >= 5:
                return
        except Exception as e:
            last = repr(e)
        time.sleep(2)
    raise TimeoutError(f"PoA2 emulation not ready: {last}")


def ensure(n: int = N_SIGNERS) -> None:
    running = _docker("ps", "--format", "{{.Names}}", check=False).stdout.split()
    st = json.loads((DIR / "current.json").read_text()) if (DIR / "current.json").exists() else {}
    if st.get("validators") == n and all(x in running for x in names(n)) and \
            not any(x in running for x in NAMES[n:]):
        try:
            h0 = int(_rpc("eth_blockNumber"), 16)
            time.sleep(PERIOD_S * 3 + 1)
            if int(_rpc("eth_blockNumber"), 16) > h0:
                return
        except Exception:
            pass
    up(n)


if __name__ == "__main__":
    n = int(sys.argv[2]) if len(sys.argv) > 2 else N_SIGNERS
    print(json.dumps(up(n) if sys.argv[1] == "up" else down(), indent=1))
