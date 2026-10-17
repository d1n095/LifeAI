#!/usr/bin/env python3
"""Canonical JSON + SHA-256 for MainAI activation attestations (no signing, no secrets).

Canonical form: UTF-8, keys sorted at every level, separators (",", ":"), no insignificant
whitespace, ensure_ascii=False. Floats are FORBIDDEN (all numbers must be integers) so the
output is identical to RFC 8785 (JCS) for the permitted value space. Arrays keep their order;
producers MUST sort arrays whose order is not meaningful (required check contexts by name,
repositories by full_name, effective rules by type).

Usage:  python3 canonical_digest.py payload.json   -> prints sha256 hex of the canonical bytes
Sign (Founder, outside MainAI, NOT done here):
        python3 canonical_digest.py --canonical payload.json > payload.canonical.json
        ssh-keygen -Y sign -f <founder_key> -n mainai-activation-v1 payload.canonical.json
Verify: ssh-keygen -Y verify -f allowed_signers -I Founder -n mainai-activation-v1 -s payload.canonical.json.sig < payload.canonical.json
"""
import hashlib, json, sys


def _reject_floats(value, path="$"):
    if isinstance(value, float):
        raise ValueError(f"float not allowed in attestation payload at {path}")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string key at {path}")
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_floats(v, f"{path}[{i}]")


def canonical_bytes(payload) -> bytes:
    _reject_floats(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(payload) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


if __name__ == "__main__":
    args = sys.argv[1:]
    as_canonical = "--canonical" in args
    path = [a for a in args if a != "--canonical"][0]
    data = json.load(open(path, encoding="utf-8"), parse_float=lambda s: (_ for _ in ()).throw(ValueError("float")))
    sys.stdout.buffer.write(canonical_bytes(data) if as_canonical else (digest(data) + "\n").encode())
