"""Compile FinVerityRegistry.sol and cache the artifact."""
from __future__ import annotations

import hashlib
import json

import solcx

from .config import BUILD, CONTRACTS, EVM_VERSION, SOLC_VERSION

SOURCE = CONTRACTS / "FinVerityRegistry.sol"
ARTIFACT = BUILD / "FinVerityRegistry.json"


def compile_registry(force: bool = False) -> dict:
    src = SOURCE.read_text()
    src_hash = hashlib.sha256(src.encode()).hexdigest()
    if ARTIFACT.exists() and not force:
        art = json.loads(ARTIFACT.read_text())
        if art.get("source_sha256") == src_hash:
            return art
    if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_standard({
        "language": "Solidity",
        "sources": {"FinVerityRegistry.sol": {"content": src}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "evmVersion": EVM_VERSION,
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}},
        },
    }, solc_version=SOLC_VERSION)
    c = out["contracts"]["FinVerityRegistry.sol"]["FinVerityRegistry"]
    art = {
        "contract": "FinVerityRegistry", "solc": SOLC_VERSION, "evm_version": EVM_VERSION,
        "source_sha256": src_hash, "abi": c["abi"],
        "bytecode": "0x" + c["evm"]["bytecode"]["object"],
        "deployed_bytecode_size": len(c["evm"]["deployedBytecode"]["object"]) // 2,
    }
    BUILD.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(art, indent=1))
    return art
