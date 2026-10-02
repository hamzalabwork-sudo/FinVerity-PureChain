"""EVM backend (PureChain PoA² and Hyperledger Besu QBFT).

Submission is pipelined: transactions are signed client-side in advance and sent with
eth_sendRawTransaction from several submitter accounts in parallel, so the measured
throughput reflects the network rather than a send-and-wait client loop.
Inclusion is observed by a block watcher that polls the chain head and records, for
every new block, the wall-clock time it became visible to the client.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import requests
import rlp
import urllib3
from eth_abi import decode as abi_decode
from eth_keys import keys
from eth_utils import keccak, to_checksum_address
from web3 import Web3

from ..config import BUILD, ChainConfig
from ..contract import compile_registry
from .base import Backend, Item, sign_approval

DEPLOYMENTS = BUILD / "deployments.json"


class RpcError(RuntimeError):
    pass


CONNECTIONS_OPENED = 0
_orig_connect = urllib3.connection.HTTPConnection.connect


def _counting_connect(self):
    global CONNECTIONS_OPENED
    CONNECTIONS_OPENED += 1
    return _orig_connect(self)


urllib3.connection.HTTPConnection.connect = _counting_connect


class Rpc:
    """Minimal JSON-RPC client with byte accounting for the network metric.

    Each submission worker owns a persistent keep-alive session (key = worker index) that
    is warmed before measurement, so connection setup (a TLS handshake costs ~0.2 s of
    client CPU on the testbed) is not attributed to individual transactions.
    """

    def __init__(self, url: str, timeout: float = 30.0):
        self.url, self.timeout = url, timeout
        self._local = threading.local()
        self._pool: dict[object, requests.Session] = {}
        self._lock = threading.Lock()
        self.bytes_out = 0
        self.bytes_in = 0
        self.calls = 0

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers["Content-Type"] = "application/json"
        return s

    def _session(self, key=None) -> requests.Session:
        if key is not None:
            with self._lock:
                s = self._pool.get(key)
                if s is None:
                    s = self._pool[key] = self._new_session()
            return s
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = self._new_session()
        return s

    def warm(self, keys) -> None:
        for k in keys:
            self._session(k).post(self.url, data=b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}',
                                  timeout=self.timeout).content

    def raw(self, payload, key=None) -> object:
        body = json.dumps(payload).encode()
        r = self._session(key).post(self.url, data=body, timeout=self.timeout)
        with self._lock:
            self.bytes_out += len(body) + 200   # + approx. HTTP/TLS request headers
            self.bytes_in += len(r.content) + 200
            self.calls += 1
        r.raise_for_status()
        return r.json()

    def call(self, method: str, params: list | None = None):
        out = self.raw({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
        if "error" in out:
            raise RpcError(f"{method}: {out['error'].get('message', out['error'])}")
        return out["result"]

    def batch(self, calls: list[tuple[str, list]]) -> list:
        if not calls:
            return []
        out = self.raw([{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)])
        by_id = {o["id"]: o for o in out}
        return [by_id[i].get("result") for i in range(len(calls))]


def recover_clique_signer(block: dict) -> str:
    """Recover the sealing validator of a PoA² (Clique-derived, Cancun header) block."""
    h = lambda x: bytes.fromhex(x[2:].rjust(len(x[2:]) + len(x[2:]) % 2, "0"))
    n = lambda x: int(x, 16)
    extra = h(block["extraData"])
    fields = [h(block["parentHash"]), h(block["sha3Uncles"]), h(block["miner"]), h(block["stateRoot"]),
              h(block["transactionsRoot"]), h(block["receiptsRoot"]), h(block["logsBloom"]),
              n(block["difficulty"]), n(block["number"]), n(block["gasLimit"]), n(block["gasUsed"]),
              n(block["timestamp"]), extra[:-65], h(block["mixHash"]), h(block["nonce"])]
    if "baseFeePerGas" in block:
        fields.append(n(block["baseFeePerGas"]))
    if "withdrawalsRoot" in block:
        fields.append(h(block["withdrawalsRoot"]))
    if "blobGasUsed" in block:
        fields += [n(block["blobGasUsed"]), n(block["excessBlobGas"]), h(block["parentBeaconBlockRoot"])]
    sig = extra[-65:]
    v = sig[64] - 27 if sig[64] >= 27 else sig[64]
    pub = keys.Signature(signature_bytes=sig[:64] + bytes([v])).recover_public_key_from_msg_hash(
        keccak(rlp.encode(fields)))
    return pub.to_checksum_address()


CONFIRMATION_DEPTHS = (1, 2, 3, 6)


class BlockWatcher(threading.Thread):
    """Fork-aware chain observer.

    Every poll re-reads the newest blocks plus a short window below the head, and walks back
    along parent hashes when a new block does not extend the locally known chain. Every
    distinct block version is recorded with the time it was first observed, so competing
    PoA2 blocks at the same height (and views from different load-balanced RPC nodes) are
    captured instead of being silently overwritten.
    """

    WINDOW = 2

    def __init__(self, rpc: Rpc, poll_s: float, recover_signer: bool, track_finalized: bool = False):
        super().__init__(daemon=True)
        self.rpc, self.poll_s, self.recover_signer = rpc, poll_s, recover_signer
        self.track_finalized = track_finalized
        self.finalized: list[tuple[float, int]] = []   # (t_observed, finalized height)
        self._last_final_poll = 0.0
        self.versions: dict[int, dict[str, dict]] = {}          # height -> hash -> block info
        self.tip: dict[int, str] = {}                             # latest known hash per height
        self.seen: dict[str, list[tuple[int, str, float]]] = {}  # tx -> [(height, hash, t_obs)]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.start_height = self.head = int(self._call("eth_blockNumber", []), 16)

    def _call(self, method, params):
        return self.rpc.raw({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, key="watcher")["result"]

    def _batch(self, heights):
        out = self.rpc.raw([{"jsonrpc": "2.0", "id": i, "method": "eth_getBlockByNumber", "params": [hex(h), False]}
                            for i, h in enumerate(heights)], key="watcher")
        return [o.get("result") for o in sorted(out, key=lambda o: o["id"])]

    def _record(self, b: dict, t_obs: float) -> None:
        num, h = int(b["number"], 16), b["hash"]
        with self._lock:
            self.tip[num] = h
            if h in self.versions.setdefault(num, {}):
                return
            self.versions[num][h] = {
                "number": num, "hash": h, "parent": b["parentHash"], "timestamp": int(b["timestamp"], 16),
                "t_observed": t_obs, "tx_count": len(b["transactions"]), "gas_used": int(b["gasUsed"], 16),
                "gas_limit": int(b["gasLimit"], 16), "size": int(b.get("size", "0x0"), 16),
                "difficulty": int(b.get("difficulty", "0x0"), 16),
                "signer": recover_clique_signer(b) if self.recover_signer else "",
            }
            for txh in b["transactions"]:
                self.seen.setdefault(txh.lower(), []).append((num, h, t_obs))

    def poll_once(self) -> None:
        head = int(self._call("eth_blockNumber", []), 16)
        lo = max(self.start_height + 1, min(self.head, head) - self.WINDOW)
        if head < lo:
            return
        t_obs = time.perf_counter()
        blocks = [b for b in self._batch(range(lo, head + 1)) if b]
        for b in blocks:
            self._record(b, t_obs)
        if blocks:  # walk back if the lowest fetched block does not extend our view of the chain
            b = blocks[0]
            for _ in range(16):
                num = int(b["number"], 16)
                if num - 1 <= self.start_height or self.tip.get(num - 1) in (None, b["parentHash"]):
                    break
                (b,) = self._batch([num - 1])
                if not b:
                    break
                self._record(b, t_obs)
        self.head = max(self.head, head)
        if self.track_finalized and time.perf_counter() - self._last_final_poll >= 1.0:
            self._last_final_poll = time.perf_counter()
            fb = self._call("eth_getBlockByNumber", ["finalized", False])
            if fb:
                num = int(fb["number"], 16)
                if not self.finalized or num > self.finalized[-1][1]:
                    self.finalized.append((time.perf_counter(), num))

    def finalized_at(self, height: int):
        for t, num in self.finalized:
            if num >= height:
                return t
        return None

    def run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop.wait(self.poll_s)

    def stop(self):
        self._stop.set()

    def first_seen(self, height: int, block_hash: str):
        v = self.versions.get(height, {}).get(block_hash)
        return v["t_observed"] if v else None


class EvmBackend(Backend):
    kind = "blockchain"

    def __init__(self, chain: ChainConfig, admin, poll_s: float = 0.05, recover_signer: bool = False,
                 containers: list[str] | None = None, n_validators: int = 1, finality: str = "inclusion",
                 finality_depth: int = 0):
        self.chain, self.admin, self.poll_s = chain, admin, poll_s
        self.finality, self.finality_depth = finality, finality_depth   # inclusion | confirmations | finalized
        self.gas_price = chain.gas_price
        self.name = chain.name
        self.recover_signer = recover_signer
        self.containers = containers or []
        self.n_validators = n_validators
        self.rpc = Rpc(chain.rpc_url)
        self.w3 = Web3()
        self.art = compile_registry()
        self.contract = None
        self.nonces: dict[str, int] = {}
        self.gas_limit = chain.tx_gas_limit
        self.watcher: BlockWatcher | None = None

    # ---------------------------------------------------------------- setup
    def _send_and_wait(self, account, tx: dict, timeout: float = 120) -> dict:
        tx = {"gasPrice": self._gas_price(), "chainId": self.chain.chain_id,
              "nonce": int(self.rpc.call("eth_getTransactionCount", [account.address, "pending"]), 16), **tx}
        raw = account.sign_transaction(tx).rawTransaction
        txh = self.rpc.call("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
        end = time.time() + timeout
        while time.time() < end:
            rc = self.rpc.call("eth_getTransactionReceipt", [txh])
            if rc:
                if int(rc["status"], 16) != 1:
                    raise RpcError(f"transaction {txh} reverted")
                return rc
            time.sleep(0.2)
        raise TimeoutError(txh)

    def _gas_price(self) -> int:
        if self.chain.gas_price >= 0:
            return self.chain.gas_price
        # EIP-1559 chain: pay 2x the current price so transactions stay valid if the base fee rises
        return 2 * int(self.rpc.call("eth_gasPrice"), 16)

    def _deployment_key(self) -> str:
        return f"{self.chain.name}:{self.chain.chain_id}:{self.art['source_sha256'][:16]}:{self.admin.address}"

    def setup(self, submitters, approvers) -> dict:
        deps = json.loads(DEPLOYMENTS.read_text()) if DEPLOYMENTS.exists() else {}
        key = self._deployment_key()
        info = deps.get(key)
        code = self.rpc.call("eth_getCode", [info["address"], "latest"]) if info else "0x"
        if not info or code in ("0x", "0x0"):
            factory = self.w3.eth.contract(abi=self.art["abi"], bytecode=self.art["bytecode"])
            data = factory.constructor().data_in_transaction
            est = int(self.rpc.call("eth_estimateGas", [{"from": self.admin.address, "data": data}]), 16)
            rc = self._send_and_wait(self.admin, {"data": data, "gas": int(est * 1.3)})
            info = {"address": to_checksum_address(rc["contractAddress"]), "deploy_tx": rc["transactionHash"],
                    "deploy_block": int(rc["blockNumber"], 16), "deploy_gas": int(rc["gasUsed"], 16),
                    "roles": {}}
            deps[key] = info
        self.contract = self.w3.eth.contract(address=info["address"], abi=self.art["abi"])

        # register roles that are not yet registered
        for role, accts, flags in (("submitter", submitters, (True, False)), ("approver", approvers, (False, True))):
            fn = "isSubmitter" if role == "submitter" else "isApprover"
            missing = [a.address for a in accts if not self._call_bool(fn, a.address)]
            if missing:
                data = self.contract.encodeABI(fn_name="setRoles", args=[missing, *flags])
                self._send_and_wait(self.admin, {"to": info["address"], "data": data, "gas": 60_000 + 30_000 * len(missing)})
            info["roles"][role] = len(accts)
        DEPLOYMENTS.parent.mkdir(parents=True, exist_ok=True)
        DEPLOYMENTS.write_text(json.dumps(deps, indent=1))

        for a in submitters:
            self.nonces[a.address] = int(self.rpc.call("eth_getTransactionCount", [a.address, "pending"]), 16)
        self.gas_price = self._gas_price()
        self.submitters = submitters
        return {"registry": info["address"], **{k: v for k, v in info.items() if k != "roles"}}

    def _call_bool(self, fn: str, *args) -> bool:
        data = self.contract.encodeABI(fn_name=fn, args=list(args))
        out = self.rpc.call("eth_call", [{"to": self.contract.address, "data": data}, "latest"])
        return int(out, 16) == 1

    def _calldata(self, item: Item) -> str:
        if item.members:
            return self.contract.encodeABI(fn_name="recordBatch", args=[
                item.record_id, item.evidence_hash, len(item.members), item.meta_hash, item.level, item.approval])
        return self.contract.encodeABI(fn_name="recordEvidence", args=[
            item.record_id, item.evidence_hash, item.meta_hash, item.level, item.approval])

    def calibrate_gas(self, item: Item, submitter, approver) -> int:
        item.approval = sign_approval(approver, self.chain.chain_id, self.contract.address, item)
        data = self._calldata(item)
        est = int(self.rpc.call("eth_estimateGas", [{"from": submitter.address, "to": self.contract.address,
                                                     "data": data}]), 16)
        self.gas_limit = int(est * 1.25)
        return est

    # ---------------------------------------------------------------- workload
    def prepare(self, item: Item, submitter, approver) -> None:
        t0 = time.perf_counter()
        item.approval = sign_approval(approver, self.chain.chain_id, self.contract.address, item)
        t1 = time.perf_counter()
        data = self._calldata(item)
        nonce = self.nonces[submitter.address]
        self.nonces[submitter.address] = nonce + 1
        raw = submitter.sign_transaction({
            "to": self.contract.address, "data": data, "gas": self.gas_limit, "gasPrice": self.gas_price,
            "nonce": nonce, "chainId": self.chain.chain_id, "value": 0}).rawTransaction
        t2 = time.perf_counter()
        item.payload = "0x" + bytes(raw).hex()
        item.payload_bytes = len(raw)
        item.tx_hash = "0x" + keccak(bytes(raw)).hex()
        item.prep["approval_sign_s"] = t1 - t0
        item.prep["tx_build_sign_s"] = t2 - t1

    def start(self, workers: int = 1) -> None:
        self.rpc.warm(["watcher", *range(workers)])
        self.watcher = BlockWatcher(self.rpc, self.poll_s, self.recover_signer,
                                    track_finalized=self.finality == "finalized")
        self._conn0 = CONNECTIONS_OPENED
        self.watcher.start()

    def send(self, item: Item, worker: int) -> None:
        item.t_send = time.perf_counter()
        for attempt in range(3):
            try:
                out = self.rpc.raw({"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                                    "params": [item.payload]}, key=worker)
                item.t_ack = time.perf_counter()
                if "error" in out:
                    msg = str(out["error"].get("message", out["error"]))
                    if "already known" in msg:
                        return
                    item.status, item.error = "failed", msg[:200]
                return
            except requests.RequestException as e:
                item.error = f"{type(e).__name__}"
                time.sleep(0.2 * (attempt + 1))
        item.t_ack = time.perf_counter()
        item.status = "failed"

    def _receipts(self, items: list[Item]) -> list:
        out = []
        for i in range(0, len(items), 200):
            out += self.rpc.batch([("eth_getTransactionReceipt", [it.tx_hash]) for it in items[i:i + 200]])
        return out

    def wait(self, items: list[Item], timeout_s: float) -> None:
        """Resolve every submitted transaction from canonical receipts, then wait for k confirmations.

        Receipts are polled until found (the RPC endpoint is load-balanced, so one node may lag
        the other). After the deepest confirmation depth has been observed, every receipt is
        re-checked against the canonical chain and re-read if its block was replaced.
        """
        unresolved = [it for it in items if it.status == "pending"]
        end = time.perf_counter() + timeout_s
        while unresolved and time.perf_counter() < end:
            still = []
            for it, rc in zip(unresolved, self._receipts(unresolved)):
                if rc and rc.get("blockHash"):
                    it.block, it.block_hash = int(rc["blockNumber"], 16), rc["blockHash"]
                    it.gas_used = int(rc["gasUsed"], 16)
                    it.status = "committed" if int(rc["status"], 16) == 1 else "reverted"
                else:
                    still.append(it)
            unresolved = still
            if unresolved:
                time.sleep(0.5)
        for it in unresolved:
            it.status, it.error = "timeout", "not included before timeout"

        self.canonical = {}
        done = [it for it in items if it.block >= 0]
        if not done:
            return
        top = max(it.block for it in done) + max(CONFIRMATION_DEPTHS)
        end = time.perf_counter() + 90
        while self.watcher.head < top and time.perf_counter() < end:
            time.sleep(0.2)
        time.sleep(1.0)
        self.watcher.poll_once()
        if self.finality == "finalized":
            top_incl = max(it.block for it in done)
            end = time.perf_counter() + 45 * 60
            while (not self.watcher.finalized or self.watcher.finalized[-1][1] < top_incl) and time.perf_counter() < end:
                time.sleep(2.0)

        # canonical re-check: a receipt read during a short fork may point at an orphaned block
        for _ in range(5):
            heights = list(range(min(it.block for it in done), min(top, self.watcher.head) + 1))
            canon = {}
            for i in range(0, len(heights), 200):
                for blk in self.rpc.batch([("eth_getBlockByNumber", [hex(h), False]) for h in heights[i:i + 200]]):
                    if blk:
                        canon[int(blk["number"], 16)] = blk["hash"]
                        self.watcher._record(blk, time.perf_counter())
            self.canonical = canon
            stale = [it for it in done if canon.get(it.block) != it.block_hash]
            if not stale:
                break
            for it, rc in zip(stale, self._receipts(stale)):
                if rc and rc.get("blockHash"):
                    it.block, it.block_hash = int(rc["blockNumber"], 16), rc["blockHash"]
            time.sleep(1.0)

        for it in done:
            t = self.watcher.first_seen(it.block, it.block_hash)
            it.t_commit = t if t is not None else float("nan")
            sightings = self.watcher.seen.get(it.tx_hash.lower(), [])
            it.t_first_seen = min((t for _, _, t in sightings), default=float("nan"))
            it.orphaned_sightings = sum(1 for n, h, _ in sightings if self.canonical.get(n) != h)
            for k in CONFIRMATION_DEPTHS:
                h = self.canonical.get(it.block + k)
                t = self.watcher.first_seen(it.block + k, h) if h else None
                it.conf[k] = t if t is not None else float("nan")
            if self.finality == "finalized":
                t = self.watcher.finalized_at(it.block)
                it.t_final = t if t is not None else float("nan")
            elif self.finality == "confirmations":
                it.t_final = it.conf.get(self.finality_depth, float("nan"))
            else:
                it.t_final = it.t_commit

    def stop(self) -> None:
        self.connections_during_run = CONNECTIONS_OPENED - getattr(self, "_conn0", CONNECTIONS_OPENED)
        if self.watcher:
            self.watcher.stop()
            self.watcher.join(timeout=5)

    def reorg_check(self) -> dict:
        """Fork statistics: every observed block version vs the final canonical chain."""
        canon = getattr(self, "canonical", {})
        heights = sorted(h for h in self.watcher.versions if h in canon)
        orphans = [(h, v) for h in heights for hh, v in self.watcher.versions[h].items() if hh != canon[h]]
        replaced = sorted({h for h, _ in orphans})
        depth, run, prev = 0, 0, None
        for h in replaced:
            run = run + 1 if prev is not None and h == prev + 1 else 1
            depth, prev = max(depth, run), h
        return {"blocks_checked": len(heights), "reorged_blocks": len(replaced), "orphan_blocks": len(orphans),
                "orphan_txs": sum(v["tx_count"] for _, v in orphans), "max_reorg_depth": depth,
                "orphans_out_of_turn": sum(1 for _, v in orphans if v["difficulty"] == 1)}

    def fetch_batch(self, batch_id: bytes) -> dict | None:
        data = self.contract.encodeABI(fn_name="getBatch", args=[batch_id])
        out = self.rpc.call("eth_call", [{"to": self.contract.address, "data": data}, "latest"])
        r = abi_decode(["(bytes32,bytes32,address,address,uint64,uint64,uint32,uint8)"], bytes.fromhex(out[2:]))[0]
        if r[4] == 0:
            return None
        return {"merkle_root": bytes(r[0]), "meta_hash": bytes(r[1]), "submitter": to_checksum_address(r[2]),
                "approver": to_checksum_address(r[3]), "timestamp": r[4], "block": r[5], "count": r[6], "level": r[7]}

    def fetch_record(self, record_id: bytes) -> dict | None:
        data = self.contract.encodeABI(fn_name="getRecord", args=[record_id])
        out = self.rpc.call("eth_call", [{"to": self.contract.address, "data": data}, "latest"])
        r = abi_decode(["(bytes32,bytes32,address,address,uint64,uint64,uint8)"], bytes.fromhex(out[2:]))[0]
        if r[4] == 0:
            return None
        return {"evidence_hash": bytes(r[0]), "meta_hash": bytes(r[1]), "submitter": to_checksum_address(r[2]),
                "approver": to_checksum_address(r[3]), "timestamp": r[4], "block": r[5], "level": r[6]}

    def info(self) -> dict:
        d = super().info()
        d.update({"rpc": self.chain.rpc_url, "chain_id": self.chain.chain_id, "gas_price_wei": self.gas_price,
                  "finality_rule": self.finality + (f":{self.finality_depth}" if self.finality_depth else ""),
                  "registry": self.contract.address if self.contract else None,
                  "client": self.rpc.call("web3_clientVersion")})
        return d

    def block_log(self) -> list[dict]:
        if not self.watcher:
            return []
        canon = getattr(self, "canonical", {})
        return [{**v, "canonical": canon.get(h) == v["hash"]} for h in sorted(self.watcher.versions)
                for v in self.watcher.versions[h].values()]

    def extra_metrics(self, items: list[Item]) -> dict:
        return {"rpc_calls": self.rpc.calls, "rpc_bytes_out": self.rpc.bytes_out, "rpc_bytes_in": self.rpc.bytes_in,
                "connections_opened_during_run": getattr(self, "connections_during_run", None)}
