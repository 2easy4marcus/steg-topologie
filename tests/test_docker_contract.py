from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _read_required_file(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} must exist"
    return path.read_text()


def _read_compose() -> dict:
    return yaml.safe_load(_read_required_file("compose.yaml"))


def test_compose_app_binds_port_to_localhost() -> None:
    app = _read_compose()["services"]["app"]

    assert app["ports"] == ["127.0.0.1:8010:8010"]


def test_compose_app_database_allows_hosted_override_with_local_default() -> None:
    app = _read_compose()["services"]["app"]

    assert app["environment"]["TURSO_DATABASE_URL"] == (
        "${TURSO_DATABASE_URL:-file:/data/tracker.db}"
    )


def test_compose_app_mounts_persistent_database_and_read_only_docs() -> None:
    app = _read_compose()["services"]["app"]

    assert app["volumes"] == [
        "tracker-data:/data",
        "./docs/data:/app/docs/data:ro",
    ]


def test_compose_app_healthcheck_contract() -> None:
    app = _read_compose()["services"]["app"]

    assert app["healthcheck"] == {
        "test": [
            "CMD",
            "python",
            "-c",
            (
                "import urllib.request; "
                "urllib.request.urlopen("
                "'http://127.0.0.1:8010/api/status', timeout=3)"
            ),
        ],
        "interval": "10s",
        "timeout": "5s",
        "retries": 6,
        "start_period": "20s",
    }


def test_compose_test_service_contract() -> None:
    test_service = _read_compose()["services"]["test"]

    assert test_service["profiles"] == ["test"]
    assert test_service["command"] == "pytest -q"
    assert test_service["environment"]["TURSO_DATABASE_URL"] == "file:/tmp/test.db"
    assert test_service["volumes"] == [".:/app"]


def test_compose_newman_service_contract() -> None:
    services = _read_compose()["services"]

    assert "smoke" not in services
    assert services["newman"]["profiles"] == ["smoke"]
    assert services["newman"]["depends_on"] == {
        "app": {"condition": "service_healthy"}
    }


def test_compose_newman_mounts_and_command_match_task_5_contract() -> None:
    services = _read_compose()["services"]
    assert "newman" in services
    newman = services["newman"]

    assert newman["volumes"] == [
        {
            "type": "bind",
            "source": "postman/tunisia-outage-tracker.postman_collection.json",
            "target": "/etc/newman/collection.json",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "postman/environment.example.json",
            "target": "/etc/newman/environment.json",
            "read_only": True,
        },
    ]
    assert newman["command"] == [
        "run",
        "/etc/newman/collection.json",
        "--environment",
        "/etc/newman/environment.json",
    ]


def test_dockerfile_pins_python_and_runs_as_non_root_app_user() -> None:
    dockerfile = _read_required_file("Dockerfile").splitlines()

    assert dockerfile[0] == "FROM python:3.13.14-slim"
    assert "USER app" in dockerfile


def test_requirements_pin_verified_uvicorn_resolution() -> None:
    requirements = _read_required_file("requirements.txt").splitlines()

    assert "uvicorn==0.52.0" in requirements


def test_dockerignore_excludes_generated_data_and_virtualenv() -> None:
    dockerignore = _read_required_file(".dockerignore").splitlines()

    assert "data/processed/" in dockerignore
    assert ".venv/" in dockerignore


def test_env_example_documents_secret_names_without_real_secrets() -> None:
    env_example = _read_required_file(".env.example")

    values = {}
    for line in env_example.splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name] = value

    assert values["CRON_SECRET"] in {"", "local-cron-secret"}
    assert values["OPS_SECRET"] in {"", "local-ops-secret"}
    assert values["TURSO_DATABASE_URL"] == ""
    assert values["TURSO_AUTH_TOKEN"] == ""
