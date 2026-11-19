"""The registry and the seam scanner must be able to fail, and must be right about this tree.

Same discipline as the security controls: a verifier that only ever prints "verified" proves
nothing. Each test constructs the failure the checker exists to catch.
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "backend" / "scripts" / "requirements"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"req_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


registry_tools = _load("verify_registry")
dead_seams = _load("dead_seams")


def _real_registry() -> dict:
    return json.loads((REPO_ROOT / registry_tools.REGISTRY_RELATIVE_PATH).read_text(encoding="utf-8"))


def test_the_repository_registry_verifies():
    assert registry_tools.verify(REPO_ROOT, _real_registry()) == []


def test_a_verified_requirement_without_an_existing_test_is_rejected():
    registry = _real_registry()
    entry = next(e for e in registry["requirements"] if e["status"] == "verified")
    entry["test_coverage"] = ["backend/tests/backend/does_not_exist.py"]

    problems = registry_tools.verify(REPO_ROOT, registry)
    assert any("does not exist" in p for p in problems), problems
    assert any("cites no existing test" in p for p in problems), problems


def test_a_dangling_dependency_is_rejected():
    registry = _real_registry()
    registry["requirements"][0]["depends_on"] = ["REQ-NOPE-999"]

    problems = registry_tools.verify(REPO_ROOT, registry)
    assert any("depends_on unknown requirement" in p for p in problems), problems


def test_a_dependency_cycle_is_rejected():
    registry = _real_registry()
    first, second = registry["requirements"][0], registry["requirements"][1]
    first["id"], second["id"] = "REQ-CYC-001", "REQ-CYC-002"
    first["depends_on"] = [second["id"]]
    second["depends_on"] = [first["id"]]

    problems = registry_tools.verify(REPO_ROOT, registry)
    assert any("dependency cycle" in p for p in problems), problems


def test_a_missing_symbol_is_rejected():
    registry = _real_registry()
    registry["requirements"][0]["implementation_evidence"] = ["backend/app/db_clock.py:no_such_symbol"]

    problems = registry_tools.verify(REPO_ROOT, registry)
    assert any("symbol 'no_such_symbol' not found" in p for p in problems), problems


def test_an_unverified_status_must_state_a_gap():
    registry = _real_registry()
    entry = next(e for e in registry["requirements"] if e["status"] == "partial")
    entry["gaps"] = []

    problems = registry_tools.verify(REPO_ROOT, registry)
    assert any("must state at least one gap" in p for p in problems), problems


def test_no_configuration_flag_in_this_repository_is_unread():
    """The one seam rule strong enough to enforce: a settings field nothing reads documents a
    behavior that does not exist, and an operator who sets it changes nothing."""
    assert dead_seams.unread_settings(REPO_ROOT) == []


def test_the_seam_scanner_detects_an_unread_setting(tmp_path):
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "config.py").write_text(
        "class Settings:\n"
        "    read_by_code: bool = True\n"
        "    never_read: bool = True\n"
        "\n"
        "    @property\n"
        "    def read_by_property(self):\n"
        "        return self.read_by_code\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "app" / "user.py").write_text(
        "from app.config import Settings\n\n\ndef f(settings):\n    return settings.read_by_code\n", encoding="utf-8"
    )

    unread = dead_seams.unread_settings(tmp_path)
    assert unread == ["never_read"], unread


def test_the_seam_scanner_does_not_flag_a_same_file_helper(tmp_path):
    """A helper called twice in its own module is reachable; flagging it is how these tools become
    noise that nobody reads."""
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "config.py").write_text("class Settings:\n    x: int = 1\n", encoding="utf-8")
    (tmp_path / "backend" / "app" / "mod.py").write_text(
        "def _helper():\n    return 1\n\n\ndef public():\n    return _helper() + _helper()\n",
        encoding="utf-8",
    )

    symbols = [f["symbol"] for f in dead_seams.unreferenced_functions(tmp_path)]
    assert "_helper" not in symbols, symbols
    # `public()` genuinely is unreferenced here -- the scanner must still report THAT, so this
    # test cannot pass by the analysis simply finding nothing.
    assert "public" in symbols, symbols
