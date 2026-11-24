"""Explicit capability evidence states — import success is not working functionality.

The states are deliberately ordinal and named, never a percentage:

    unknown → module_located → implementation_inspected → tests_observed
            → integration_verified → runtime_verified

Only a caller who has observed a real runtime outcome may supply
``runtime_refs``; this module never upgrades a state on its own and never
turns "the import worked" into "Life can do this". Test references are file
mentions, not passing tests, and are labelled as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EVIDENCE_STATES = (
    "unknown",
    "module_located",
    "implementation_inspected",
    "tests_observed",
    "integration_verified",
    "runtime_verified",
)
_STATE_ORDER = {state: index for index, state in enumerate(EVIDENCE_STATES)}

# A source file with fewer executable lines than this is treated as a stub even when it
# exists — a package marker or a docstring-only module is not an implementation.
_MIN_CODE_LINES = 5
_MAX_TEST_SCAN_BYTES = 400_000
_MAX_TEST_REFERENCES = 10

_PLACEHOLDER_RE = re.compile(
    r"^\s*(pass\s*(#.*)?|raise\s+NotImplementedError\b|\.\.\.\s*)$"
)


def default_repo_root() -> Path:
    """Repository root for a checkout laid out as ``<root>/backend/app/...``."""
    return Path(__file__).resolve().parents[3]


def module_source_path(module: str, repo_root: Path | str | None = None) -> Path | None:
    """Resolve a dotted module to a file WITHOUT importing it.

    Importing a module can execute application code; a capability scan must be able to
    answer "does this file exist" without that side effect.
    """
    base = Path(repo_root or default_repo_root()) / "backend"
    target = base.joinpath(*module.split("."))
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _read_text(path: Path, *, limit: int = _MAX_TEST_SCAN_BYTES) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


@dataclass(frozen=True)
class ModuleEvidence:
    module: str
    state: str
    basis: tuple[str, ...]
    source_path: str | None
    test_references: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def operational_claim(self) -> bool:
        """True only for an observed runtime outcome — never for located/inspected code."""
        return self.state == "runtime_verified"

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "state": self.state,
            "basis": list(self.basis),
            "source_path": self.source_path,
            "test_references": list(self.test_references),
            "operational_claim": self.operational_claim,
            "limitations": list(self.limitations),
        }


@lru_cache(maxsize=256)
def _test_references_cached(module: str, repo_root: str) -> tuple[str, ...]:
    tests_root = Path(repo_root) / "backend" / "tests"
    if not tests_root.is_dir():
        return ()
    # Require the dotted module identity (or an explicit import of it) so a generic
    # basename such as "types" or "loop" cannot produce a false test reference.
    needles = (f"import {module}", f"from {module}", module)
    references: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        text = _read_text(path)
        if not text:
            continue
        if any(needle in text for needle in needles):
            try:
                references.append(str(path.relative_to(Path(repo_root) / "backend")))
            except ValueError:
                references.append(str(path))
            if len(references) >= _MAX_TEST_REFERENCES:
                break
    return tuple(references)


def test_references(module: str, repo_root: Path | str | None = None) -> tuple[str, ...]:
    return _test_references_cached(module, str(repo_root or default_repo_root()))


def _inspect_source(path: Path) -> tuple[bool, int]:
    """Return (has_real_implementation, executable_line_count)."""
    text = _read_text(path)
    code_lines = 0
    placeholder_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(('"""', "'''")):
            continue
        code_lines += 1
        if _PLACEHOLDER_RE.match(line):
            placeholder_lines += 1
    real = code_lines >= _MIN_CODE_LINES and placeholder_lines < code_lines
    return real, code_lines


def assess_module(
    module: str,
    *,
    repo_root: Path | str | None = None,
    integration_refs: tuple[str, ...] | list[str] = (),
    runtime_refs: tuple[str, ...] | list[str] = (),
) -> ModuleEvidence:
    """Assess one module up to the strongest evidence the caller can actually supply.

    ``integration_refs``/``runtime_refs`` are caller assertions of observed facts. This
    function does not verify them; it records them verbatim so the claim stays auditable.
    """
    root = Path(repo_root or default_repo_root())
    path = module_source_path(module, root)
    if path is None:
        return ModuleEvidence(
            module=module,
            state="unknown",
            basis=("module_not_found_in_checkout",),
            source_path=None,
            test_references=(),
            limitations=("absence from this checkout does not prove absence from another branch",),
        )

    basis = ["module_file_located"]
    limitations: list[str] = []
    state = "module_located"
    real, code_lines = _inspect_source(path)
    if real:
        state = "implementation_inspected"
        basis.append(f"source_inspected:{code_lines}_executable_lines")
    else:
        limitations.append("source is a stub or docstring-only module")

    refs = test_references(module, root)
    if refs:
        state = "tests_observed"
        basis.append(f"test_files_mention_module:{len(refs)}")
        limitations.append("test file mention is not a passing test run")

    if tuple(integration_refs):
        state = "integration_verified"
        basis.append(f"caller_supplied_integration_refs:{len(tuple(integration_refs))}")

    if tuple(runtime_refs):
        state = "runtime_verified"
        basis.append(f"caller_supplied_runtime_refs:{len(tuple(runtime_refs))}")

    return ModuleEvidence(
        module=module,
        state=state,
        basis=tuple(basis),
        source_path=str(path),
        test_references=refs,
        limitations=tuple(limitations),
    )


def assess_modules(
    modules: tuple[str, ...] | list[str],
    *,
    repo_root: Path | str | None = None,
    integration_refs: dict[str, tuple[str, ...] | list[str]] | None = None,
    runtime_refs: dict[str, tuple[str, ...] | list[str]] | None = None,
) -> dict:
    """Assess a group of modules and report the strongest AND weakest evidence.

    The weakest state is the honest aggregate: one runtime-verified module does not make a
    capability area operational if its siblings were never inspected.
    """
    integration_refs = integration_refs or {}
    runtime_refs = runtime_refs or {}
    evidence = [
        assess_module(
            module,
            repo_root=repo_root,
            integration_refs=integration_refs.get(module, ()),
            runtime_refs=runtime_refs.get(module, ()),
        )
        for module in modules
    ]
    states = [item.state for item in evidence]
    strongest = max(states, key=lambda value: _STATE_ORDER[value]) if states else "unknown"
    weakest = min(states, key=lambda value: _STATE_ORDER[value]) if states else "unknown"
    return {
        "modules": evidence,
        "states": {item.module: item.state for item in evidence},
        "strongest_state": strongest,
        "weakest_state": weakest,
        "all_operational": bool(evidence) and all(item.operational_claim for item in evidence),
        "percentage": None,
        "percentage_note": "explicit states only; a coverage percentage is not evidence",
    }
