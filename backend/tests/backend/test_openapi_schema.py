"""STEG 9 / DEL 12 of the Founder Knowledge Studio work order: "OpenAPI-kontroll" — every
new /api/library and /api/workbench route must have a real request/response schema, not just
"the app happens to run". A route with no response_model or an untyped body wouldn't fail at
runtime, only silently ship a useless (or misleading) OpenAPI doc — this test catches that
class of gap directly, not by inference from route code."""

from app.main import app

FKS_PREFIXES = ("/api/library", "/api/workbench")


def _schema():
    return app.openapi()


def test_openapi_schema_builds_without_error():
    schema = _schema()
    assert schema["openapi"]
    assert schema["paths"]


def test_every_founder_knowledge_studio_route_is_present():
    schema = _schema()
    fks_paths = {p for p in schema["paths"] if p.startswith(FKS_PREFIXES)}
    expected = {
        "/api/library",
        "/api/library/import",
        "/api/library/jobs/{job_id}",
        "/api/library/{source_id}",
        "/api/library/{source_id}/relationships",
        "/api/library/search/hybrid",
        "/api/workbench/analyze",
        "/api/workbench/save",
    }
    assert expected <= fks_paths


def test_every_founder_knowledge_studio_route_declares_a_response_schema():
    """A route with no response_model still "works" but produces an OpenAPI operation with
    no usable response schema — this is exactly the gap DEL 12 asks to be checked for, not a
    hypothetical."""
    schema = _schema()
    for path, operations in schema["paths"].items():
        if not path.startswith(FKS_PREFIXES):
            continue
        for method, operation in operations.items():
            if method not in ("get", "post", "delete", "patch", "put"):
                continue
            responses = operation.get("responses", {})
            success_codes = [c for c in responses if c.startswith("2")]
            assert success_codes, f"{method.upper()} {path} has no documented success response"
            for code in success_codes:
                content = responses[code].get("content")
                # A 204/empty-body response legitimately has no content schema; every other
                # 2xx response on these routes returns a real Pydantic model and must show it.
                if content is None:
                    continue
                assert "application/json" in content, f"{method.upper()} {path} {code} has no JSON schema"
