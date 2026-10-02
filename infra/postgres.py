"""Baseline 1: PostgreSQL 16 in Docker (durable commits: fsync=on, synchronous_commit=on).

    python -m infra.postgres up | down
"""
from __future__ import annotations

import subprocess
import sys
import time

NAME = "finverity-postgres"
IMAGE = "postgres:16"


def up() -> None:
    running = subprocess.run(["docker", "ps", "--filter", f"name={NAME}", "--format", "{{.Names}}"],
                             capture_output=True, text=True).stdout.split()
    if NAME not in running:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        subprocess.run(["docker", "run", "-d", "--name", NAME, "-e", "POSTGRES_USER=finverity",
                        "-e", "POSTGRES_PASSWORD=finverity", "-e", "POSTGRES_DB=finverity",
                        "-p", "127.0.0.1:5433:5432", IMAGE, "-c", "max_connections=200",
                        "-c", "synchronous_commit=on", "-c", "fsync=on"], check=True, capture_output=True)
    import psycopg
    from finverity.config import POSTGRES_DSN
    for _ in range(60):
        try:
            psycopg.connect(POSTGRES_DSN).close()
            return
        except Exception:
            time.sleep(1)
    raise TimeoutError("postgres not ready")


def down() -> None:
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)


if __name__ == "__main__":
    {"up": up, "down": down}[sys.argv[1]]()
