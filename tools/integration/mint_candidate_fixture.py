#!/usr/bin/env python3
"""Fixture C builder: mint a REAL commit whose hash starts with the mission-specified
candidate prefix e392022 (grinding SHA-1 over commit buffers), then bundle it.

Purpose: prove the freebuff_integration gate accepts ONLY artifacts that genuinely carry
the candidate identity, and that candidate production (S7) works end-to-end. The commit
content is clearly marked as a SYNTHETIC FIXTURE — it is never treated as Freebuff's real
work; it exercises the pipeline mechanics only.
"""

import hashlib
import multiprocessing as mp
import os
import subprocess
import sys
import time

BASE = "/home/z/my-project/review-target/LifeAI"
PREFIX = "e392022"
SCRATCH = "/tmp/fb_fixture_c"
WORK = f"{SCRATCH}/repo"


def tree_of(repo: str, treeish: str) -> str:
    return subprocess.run(["git", "rev-parse", f"{treeish}^{{tree}}"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def mkcommit(tree: str, parent: str, nonce: int, when: int) -> tuple[str, str]:
    content = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        f"author Fixture Grinder <fixture@grinder.invalid> {when} +0000\n"
        f"committer Fixture Grinder <fixture@grinder.invalid> {when} +0000\n"
        "\n"
        "Freebuff Founder Alpha candidate (SYNTHETIC FIXTURE — pipeline validation only)\n\n"
        "Fixture commit minted by scripts/mint_candidate_fixture.py to exercise the\n"
        "freebuff_integration.py gate and candidate production. NOT real Freebuff work.\n"
        f"nonce: {nonce}\n"
    )
    header = f"commit {len(content.encode())}\0"  # git's header counts BYTES, not chars
    return hashlib.sha1((header + content).encode()).hexdigest(), content


def grind(args) -> str | None:
    """Scan one chunk; on a prefix hit return the COMMIT CONTENT (not the hash) — the
    object is materialized exactly once in main(), because concurrent `git hash-object
    -w` writers race the repo's object lock (exit 128)."""
    start, step, tree, parent, when = args
    nonce = start
    for _ in range(chunk_size):
        h, content = mkcommit(tree, parent, nonce, when)
        if h.startswith(PREFIX):
            return content
        nonce += step
    return None


chunk_size = 100_000


def main() -> None:
    subprocess.run(["rm", "-rf", SCRATCH], check=True)
    os.makedirs(SCRATCH)
    subprocess.run(["git", "clone", "-q", BASE, WORK], check=True)
    subprocess.run(["git", "checkout", "-q", "69dc5ea5a9cfc750303ea1a9b335c78612bc8f39"], cwd=WORK, check=True)
    # Distinct tree: one marker file on top of the base tree.
    with open(f"{WORK}/FREEBUFF_FIXTURE_MARKER.txt", "w") as f:
        f.write("synthetic fixture — pipeline validation only\n")
    subprocess.run(["git", "add", "-A"], cwd=WORK, check=True)
    subprocess.run(["git", "write-tree"], cwd=WORK, capture_output=True, check=True)
    tree = tree_of(WORK, "HEAD")  # HEAD still base; tree written separately below
    tree = subprocess.run(["git", "write-tree"], cwd=WORK, capture_output=True, text=True, check=True).stdout.strip()

    parent = "69dc5ea5a9cfc750303ea1a9b335c78612bc8f39"
    when = 1765000000
    cores = max(1, (os.cpu_count() or 4) - 1)
    t0 = time.time()
    found = None
    with mp.Pool(cores) as pool:
        for res in pool.imap_unordered(
            grind,
            ((s, 1, tree, parent, when) for s in range(0, 1_000_000_000, chunk_size)),
            chunksize=1,
        ):
            if res:
                found = res
                pool.terminate()
                break
    if not found:
        print("NOT FOUND in scanned range — widen range and rerun", file=sys.stderr)
        return
    # Materialize exactly once (workers returned content, never wrote).
    p = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=WORK, input=found.encode(), capture_output=True, check=True,
    )
    found_hash = p.stdout.decode().strip()
    assert found_hash.startswith(PREFIX), "materialized hash lost the prefix"
    dt = time.time() - t0
    print(f"minted candidate fixture commit: {found_hash} ({dt:.1f}s, {cores} cores)")
    subprocess.run(["git", "update-ref", "refs/heads/candidate-fixture", found_hash], cwd=WORK, check=True)
    subprocess.run(["git", "bundle", "create", f"{SCRATCH}/candidate-fixture.bundle", "refs/heads/candidate-fixture"], cwd=WORK, check=True)
    subprocess.run(["git", "bundle", "verify", f"{SCRATCH}/candidate-fixture.bundle"], cwd=WORK, check=True)
    print(f"bundle: {SCRATCH}/candidate-fixture.bundle")


if __name__ == "__main__":
    main()
