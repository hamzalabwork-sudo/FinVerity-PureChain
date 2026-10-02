"""Local infrastructure control. Only the backend under test runs during a trial."""
from __future__ import annotations

import subprocess


def _names(prefix: str, all_: bool = True) -> list[str]:
    args = ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={prefix}"] + (["-a"] if all_ else [])
    return subprocess.run(args, capture_output=True, text=True).stdout.split()


GROUPS = {"besu": "besu-validator", "ethpos": "ethpos-", "db_postgres": "finverity-postgres", "poa2_local": "poa2-signer"}
RECREATE = {"ethpos", "poa2_local"}   # removed, not paused: always started fresh


def isolate(active: str) -> None:
    """Stop every other local network so it cannot compete for the host CPU.

    Besu and PostgreSQL are stopped (state kept). The PoS devnet is removed instead: its
    execution client does not recover from a stop/start (it stalls in 'syncing'), so it is
    always recreated fresh when needed.
    """
    for backend, prefix in GROUPS.items():
        if backend == active:
            continue
        running = _names(prefix, all_=False)
        if not running:
            continue
        if backend in RECREATE:
            subprocess.run(["docker", "rm", "-f", *_names(prefix)], capture_output=True)
        else:
            subprocess.run(["docker", "stop", "-t", "5", *running], capture_output=True)


def restart_stopped(prefix: str, order: list[str] | None = None) -> bool:
    """Start previously stopped containers (state preserved). Returns True if any were started."""
    stopped = [n for n in _names(prefix) if n not in _names(prefix, all_=False)]
    if order:
        stopped.sort(key=lambda n: order.index(n) if n in order else len(order))
    for n in stopped:
        subprocess.run(["docker", "start", n], capture_output=True)
    return bool(stopped)
