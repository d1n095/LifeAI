#!/usr/bin/env python3
"""Fixture D: an e392022-prefixed candidate that CARRIES A CONFLICTING MIGRATION (0069
with a different revision body). The gate must refuse candidate production (exit 2)."""
import os
import subprocess
import sys

sys.path.insert(0, "/home/z/my-project/scripts")
from mint_candidate_fixture import PREFIX, BASE, grind, mkcommit, chunk_size  # noqa: E402

import multiprocessing as mp

SCRATCH = "/tmp/fb_fixture_d"
WORK = f"{SCRATCH}/repo"


def main() -> None:
    subprocess.run(["rm", "-rf", SCRATCH], check=True)
    os.makedirs(SCRATCH)
    subprocess.run(["git", "clone", "-q", BASE, WORK], check=True)
    subprocess.run(["git", "checkout", "-q", "69dc5ea5a9cfc750303ea1a9b335c78612bc8f39"], cwd=WORK, check=True)
    with open(f"{WORK}/backend/alembic/versions/0069_conflicting_fixture.py", "w") as f:
        f.write('"""Conflicting fixture migration (revision 0069, different content)."""\n\n'
                "from alembic import op\n\n"
                'revision = "0069"\n'
                'down_revision = "0068"\n\n'
                "def upgrade() -> None:\n    op.execute(\"SELECT 1\")\n\n"
                "def downgrade() -> None:\n    pass\n")
    subprocess.run(["git", "add", "-A"], cwd=WORK, check=True)
    tree = subprocess.run(["git", "write-tree"], cwd=WORK, capture_output=True, text=True, check=True).stdout.strip()
    parent = "69dc5ea5a9cfc750303ea1a9b335c78612bc8f39"
    when = 1765000100
    cores = max(1, (os.cpu_count() or 4) - 1)
    found = None
    with mp.Pool(cores) as pool:
        for res in pool.imap_unordered(grind, ((s, 1, tree, parent, when) for s in range(0, 1_000_000_000, chunk_size)), chunksize=1):
            if res:
                found = res
                pool.terminate()
                break
    if not found:
        print("NOT FOUND", file=sys.stderr)
        return
    p = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=WORK, input=found.encode(), capture_output=True, check=True)
    h = p.stdout.decode().strip()
    assert h.startswith(PREFIX)
    subprocess.run(["git", "update-ref", "refs/heads/candidate-fixture-conflict", h], cwd=WORK, check=True)
    subprocess.run(["git", "bundle", "create", f"{SCRATCH}/candidate-fixture-conflict.bundle", "refs/heads/candidate-fixture-conflict"], cwd=WORK, check=True)
    print(f"conflicting fixture: {h}")


if __name__ == "__main__":
    main()
