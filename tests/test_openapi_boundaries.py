import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATHS = {"/api/status", "/api/model-readiness", "/api/stats"}
POSTMAN_PACKAGE = ROOT / "node_modules" / "openapi-to-postmanv2"


def _postman_generator_available(node_path, package_path):
    return node_path is not None and package_path.is_dir()


def test_fastapi_default_documentation_is_disabled():
    assert main.app.openapi_url is None
    assert main.app.docs_url is None
    assert main.app.redoc_url is None


def test_public_openapi_is_an_explicit_safe_versioned_contract():
    client = TestClient(main.app)
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["version"] == "2.0.0"
    assert set(schema["paths"]) == PUBLIC_PATHS
    assert client.get("/api/status").json()["status"] == "ok"
    serialized = json.dumps(schema).lower()
    for forbidden in (
        "/api/internal",
        "/api/reports",
        "reportin",
        "x-ops-secret",
        "x-cron-secret",
        "internal_error",
        "turso",
        "asset_id",
        "candidate",
        "zone_text",
    ):
        assert forbidden not in serialized


def test_public_docs_uses_public_schema():
    response = TestClient(main.app).get("/docs")

    assert response.status_code == 200
    assert "/openapi.json" in response.text
    assert "swagger-ui" in response.text


def test_internal_openapi_requires_ops_secret(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "configured-ops-value")
    client = TestClient(main.app)

    assert client.get("/api/internal/openapi.json").status_code == 401
    assert client.get(
        "/api/internal/openapi.json",
        headers={"X-Ops-Secret": "wrong"},
    ).status_code == 401


def test_internal_openapi_contains_internal_ops_routes(monkeypatch):
    monkeypatch.setattr(main, "OPS_SECRET", "configured-ops-value")
    response = TestClient(main.app).get(
        "/api/internal/openapi.json",
        headers={"X-Ops-Secret": "configured-ops-value"},
    )

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["version"] == "2.0.0"
    assert "/api/internal/ops/summary" in schema["paths"]
    assert all(path.startswith("/api/internal/") for path in schema["paths"])


def test_public_openapi_export_matches_committed_artifact_from_any_cwd(tmp_path):
    output = tmp_path / "build" / "openapi-public.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "export_openapi.py"),
            "--output-root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        check=True,
    )
    first = output.read_bytes()
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "export_openapi.py"),
            "--output-root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        check=True,
    )

    assert output.read_bytes() == first
    assert first == (ROOT / "build" / "openapi-public.json").read_bytes()
    assert first.endswith(b"\n")
    assert list(json.loads(first)["paths"]) == sorted(PUBLIC_PATHS)


@pytest.mark.skipif(
    not _postman_generator_available(shutil.which("node"), POSTMAN_PACKAGE),
    reason="Node.js or openapi-to-postmanv2 is not installed",
)
def test_postman_generation_matches_committed_artifacts(tmp_path):
    export_command = [
        sys.executable,
        str(ROOT / "scripts" / "export_openapi.py"),
        "--output-root",
        str(tmp_path),
    ]
    postman_command = [
        "node",
        str(ROOT / "scripts" / "generate_postman.mjs"),
        str(tmp_path),
    ]

    subprocess.run(export_command, cwd=tmp_path, check=True)
    subprocess.run(postman_command, cwd=tmp_path, check=True)
    generated = {}
    for relative in (
        "build/openapi-public.json",
        "postman/tunisia-outage-tracker.postman_collection.json",
        "postman/tunisia-outage-tracker-security-smoke.postman_collection.json",
        "postman/environment.example.json",
    ):
        generated[relative] = (tmp_path / relative).read_bytes()
        assert generated[relative] == (ROOT / relative).read_bytes()

    subprocess.run(export_command, cwd=tmp_path, check=True)
    subprocess.run(postman_command, cwd=tmp_path, check=True)
    assert all(
        (tmp_path / relative).read_bytes() == contents
        for relative, contents in generated.items()
    )


def test_postman_generator_availability_requires_node_and_package(tmp_path):
    package = tmp_path / "openapi-to-postmanv2"

    assert not _postman_generator_available(None, package)
    assert not _postman_generator_available("/usr/bin/node", package)
    package.mkdir()
    assert _postman_generator_available("/usr/bin/node", package)


def test_postman_generator_uses_cross_platform_package_api():
    source = (ROOT / "scripts" / "generate_postman.mjs").read_text()

    assert "node_modules/.bin" not in source
    assert "createRequire" in source
    assert 'require("openapi-to-postmanv2")' in source


def test_generated_public_artifacts_are_safe():
    artifacts = [
        ROOT / "build" / "openapi-public.json",
        ROOT / "postman" / "tunisia-outage-tracker.postman_collection.json",
    ]
    forbidden = (
        "/api/internal",
        "/api/reports",
        "x-ops-secret",
        "x-cron-secret",
        "internal_error",
        "turso_database_url",
        "libsql://",
        "asset_id",
        "candidate",
        "zone_text",
        "configured-ops-value",
        "local-ops-secret",
    )

    for path in artifacts:
        contents = path.read_text().lower()
        assert all(value not in contents for value in forbidden), path


def test_postman_collections_cover_public_and_security_smoke_cases():
    public = json.loads(
        (
            ROOT
            / "postman"
            / "tunisia-outage-tracker.postman_collection.json"
        ).read_text()
    )
    public_text = json.dumps(public)
    for path in sorted(PUBLIC_PATHS):
        assert path in public_text
    assert public_text.count("pm.response.to.have.status(200)") == 3

    security = json.loads(
        (
            ROOT
            / "postman"
            / "tunisia-outage-tracker-security-smoke.postman_collection.json"
        ).read_text()
    )
    security_text = json.dumps(security)
    assert "/api/internal/ops/summary" in security_text
    assert "pm.response.to.have.status(401)" in security_text
    assert "X-Ops-Secret" not in security_text
    assert "configured-ops-value" not in security_text
    assert "local-ops-secret" not in security_text


def test_postman_environment_and_generator_are_safe_and_locked():
    environment_path = ROOT / "postman" / "environment.example.json"
    environment = json.loads(environment_path.read_text())
    values = {entry["key"]: entry["value"] for entry in environment["values"]}
    assert values == {"baseUrl": "http://app:8010", "opsSecret": ""}

    package = json.loads((ROOT / "package.json").read_text())
    lock = json.loads((ROOT / "package-lock.json").read_text())
    assert package["devDependencies"]["openapi-to-postmanv2"] == "5.0.0"
    assert lock["packages"][""]["devDependencies"]["openapi-to-postmanv2"] == "5.0.0"
    assert lock["packages"]["node_modules/openapi-to-postmanv2"]["version"] == "5.0.0"

    combined = "".join(
        path.read_text()
        for path in (
            ROOT / "build" / "openapi-public.json",
            ROOT / "postman" / "tunisia-outage-tracker.postman_collection.json",
            ROOT / "postman" / "tunisia-outage-tracker-security-smoke.postman_collection.json",
            environment_path,
        )
    )
    assert "configured-ops-value" not in combined
    assert "local-ops-secret" not in combined


def test_smoke_command_sequences_both_collections_with_failure_propagation():
    package = json.loads((ROOT / "package.json").read_text())

    assert package["scripts"]["smoke"] == (
        "docker compose --profile smoke run --rm newman && "
        "docker compose --profile smoke run --rm newman-security"
    )
