#!/usr/bin/env python3
"""Merge several pytest JUnit XML files into one evidence file.

The QA gate and the F01-F24 matrix intentionally consume ONE JUnit file. Running the
suite in focused groups keeps each command inside a bounded time budget, while this
merge preserves every testcase and the aggregate counts the gate reads.

Usage:
    python scripts/acceptance/merge_junit.py --out /tmp/program-tests.xml a.xml b.xml ...
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def _suites(root: ET.Element) -> list[ET.Element]:
    if root.tag == "testsuites":
        return list(root.findall("testsuite"))
    return [root]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    merged = ET.Element("testsuites")
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    for path in args.inputs:
        root = ET.parse(path).getroot()
        for suite in _suites(root):
            merged.append(suite)
            for key in ("tests", "failures", "errors", "skipped"):
                totals[key] += int(suite.attrib.get(key, 0))
            totals["time"] += float(suite.attrib.get("time", 0.0))

    merged.set("tests", str(totals["tests"]))
    merged.set("failures", str(totals["failures"]))
    merged.set("errors", str(totals["errors"]))
    merged.set("skipped", str(totals["skipped"]))
    merged.set("time", f"{totals['time']:.3f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(merged, space="  ")
    ET.ElementTree(merged).write(out, encoding="utf-8", xml_declaration=True)
    print(
        f"wrote {out}: tests={totals['tests']} failures={totals['failures']} "
        f"errors={totals['errors']} skipped={totals['skipped']}"
    )
    return 0 if totals["failures"] == 0 and totals["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
