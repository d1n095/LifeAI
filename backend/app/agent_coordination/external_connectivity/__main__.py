"""python -m app.agent_coordination.external_connectivity

Prints the honest inspect matrix. Optional --probe hits api.cursor.com without credentials.
"""

from __future__ import annotations

import argparse
import json

from app.agent_coordination.external_connectivity.inspect import inspect_external_connectivity


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect official external coding-agent surfaces without claiming delivery.")
    parser.add_argument("--probe", action="store_true", help="Unauthenticated GET /v1/me only. No API key is sent.")
    args = parser.parse_args()
    print(json.dumps(inspect_external_connectivity(probe_network=args.probe), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
