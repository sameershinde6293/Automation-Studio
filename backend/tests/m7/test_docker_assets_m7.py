"""Static validation of the Docker deployment assets (M7).

Why these exist
---------------
No container runtime has ever been available to this project: M5 wrote the
Docker assets, M6 could not execute them, and M7 confirmed the same (no
``docker``/``podman``/``nerdctl`` binary, no ``/var/run/docker.sock``, the
Docker package repositories and every container registry unreachable, and
``podman`` absent from the configured apt sources). ``docker build`` and
``docker compose up`` therefore remain **unverified** — see
``docs/M7_RELEASE_AUDIT.md`` §6.

What can still be checked is the *internal consistency* of the assets, and
that is worth doing precisely because nothing else checks it. Every assertion
below encodes a mistake that would surface only as a failed deployment:

* a service referencing an env var the template never defines;
* the frontend proxying to a hostname that is not a compose service;
* the container port disagreeing with the port the app actually binds;
* the healthcheck probing a path the API does not serve;
* migrations wired to run from the app container (which races across replicas);
* the image running as root, or the database published to the host.

These are text-level checks, not a substitute for running the stack. They are
labelled as such in the audit so the distinction is never blurred.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
BACKEND_DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
NGINX_CONF = REPO_ROOT / "frontend" / "nginx.conf"
ENV_TEMPLATE = REPO_ROOT / ".env.production.example"


@pytest.fixture(scope="module")
def compose_text() -> str:
    if not COMPOSE_FILE.is_file():
        pytest.skip("docker-compose.yml is not present")
    return COMPOSE_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_dockerfile() -> str:
    if not BACKEND_DOCKERFILE.is_file():
        pytest.skip("backend/Dockerfile is not present")
    return BACKEND_DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_dockerfile() -> str:
    if not FRONTEND_DOCKERFILE.is_file():
        pytest.skip("frontend/Dockerfile is not present")
    return FRONTEND_DOCKERFILE.read_text(encoding="utf-8")


class TestComposeServiceTopology:
    def test_defines_the_four_expected_services(self, compose_text):
        for service in ("db:", "migrate:", "backend:", "frontend:"):
            assert re.search(rf"^  {service}", compose_text, re.M), (
                f"compose service {service!r} is missing"
            )

    def test_backend_waits_for_a_healthy_database(self, compose_text):
        """``depends_on`` alone does not wait for readiness, only for start."""
        assert "condition: service_healthy" in compose_text

    def test_database_is_not_published_to_the_host(self, compose_text):
        """The db must be reachable only on the compose network."""
        db_block = compose_text.split("  migrate:")[0]
        assert "5432:5432" not in db_block, (
            "the database port is published to the host; only the backend "
            "needs to reach it"
        )

    def test_named_volumes_are_declared(self, compose_text):
        """State that must survive `docker compose down` needs a named volume."""
        assert re.search(r"^volumes:", compose_text, re.M)
        for volume in ("db_data", "media_data"):
            assert volume in compose_text, f"volume {volume!r} is not declared"

    def test_migrate_service_is_a_one_shot_tool(self, compose_text):
        """Migrations must not run from the app container (replica race)."""
        migrate_block = compose_text.split("  migrate:")[1].split("  backend:")[0]
        assert 'profiles: ["tools"]' in migrate_block
        assert 'restart: "no"' in migrate_block
        assert "alembic" in migrate_block and "upgrade" in migrate_block

    def test_backend_does_not_run_migrations_on_start(self, compose_text):
        backend_block = compose_text.split("  backend:")[1].split("  frontend:")[0]
        assert "alembic" not in backend_block, (
            "the backend service runs migrations at start; with >1 replica "
            "they race. Use the one-shot `migrate` service."
        )

    def test_services_drop_privilege_escalation(self, compose_text):
        assert compose_text.count("no-new-privileges:true") >= 2


class TestComposeEnvironmentContract:
    """Every ``${VAR}`` compose needs must be documented in the template."""

    def test_required_variables_are_in_the_template(self, compose_text):
        if not ENV_TEMPLATE.is_file():
            pytest.skip(".env.production.example is not present")
        template = ENV_TEMPLATE.read_text(encoding="utf-8")

        referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose_text))
        # HTTP_PORT has a compose-level default and is optional for the operator.
        documented_elsewhere = {"HTTP_PORT"}
        missing = {
            name
            for name in referenced - documented_elsewhere
            if not re.search(rf"^#?\s*{name}=", template, re.M)
        }
        assert not missing, (
            f"docker-compose.yml uses {sorted(missing)} but "
            ".env.production.example never mentions them, so an operator "
            "following the quick start cannot know to set them"
        )

    def test_mandatory_secrets_fail_fast_when_unset(self, compose_text):
        """``:?`` makes compose refuse to start rather than default silently."""
        assert "POSTGRES_PASSWORD:?" in compose_text
        assert "AUTH_SECRET_KEY:?" in compose_text

    def test_backend_is_pinned_to_production(self, compose_text):
        backend_block = compose_text.split("  backend:")[1].split("  frontend:")[0]
        assert "ENVIRONMENT: production" in backend_block

    def test_backend_trusts_proxy_headers_behind_the_frontend(self, compose_text):
        """The frontend proxies, so the real client IP arrives forwarded."""
        backend_block = compose_text.split("  backend:")[1].split("  frontend:")[0]
        assert 'TRUST_PROXY_HEADERS: "true"' in backend_block


class TestPortAndProbeConsistency:
    """The most common silent-deployment-failure class."""

    def test_backend_exposes_the_port_it_binds(self, compose_text, backend_dockerfile):
        assert "PORT=8000" in backend_dockerfile
        assert "EXPOSE 8000" in backend_dockerfile
        backend_block = compose_text.split("  backend:")[1].split("  frontend:")[0]
        assert '"8000"' in backend_block

    def test_nginx_proxies_to_the_backend_service_name(self, compose_text):
        """The upstream host must match the compose service name exactly."""
        if not NGINX_CONF.is_file():
            pytest.skip("nginx.conf is not present")
        nginx = NGINX_CONF.read_text(encoding="utf-8")
        upstreams = set(re.findall(r"proxy_pass\s+http://([A-Za-z0-9_.-]+)", nginx))
        assert upstreams, "nginx.conf defines no proxy_pass upstream"
        for host in upstreams:
            assert re.search(rf"^  {host}:", compose_text, re.M), (
                f"nginx proxies to {host!r}, which is not a compose service"
            )

    def test_nginx_proxies_to_the_backend_container_port(self):
        if not NGINX_CONF.is_file():
            pytest.skip("nginx.conf is not present")
        assert "backend:8000" in NGINX_CONF.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "probe_path", ["/health/live", "/health/ready"]
    )
    def test_probe_paths_are_real_routes(self, probe_path):
        """A healthcheck against a 404 marks a healthy container unhealthy."""
        from app.main import app

        routes = {getattr(r, "path", None) for r in app.routes}
        assert probe_path in routes, (
            f"{probe_path} is used as a container probe but the application "
            "serves no such route"
        )

    def test_compose_backend_healthcheck_uses_readiness(self, compose_text):
        backend_block = compose_text.split("  backend:")[1].split("  frontend:")[0]
        assert "/health/ready" in backend_block

    def test_dockerfile_healthcheck_uses_liveness(self, backend_dockerfile):
        """Liveness must not touch the DB, or a DB blip restarts the app."""
        assert "/health/live" in backend_dockerfile


class TestImageHardening:
    def test_backend_runs_as_an_unprivileged_user(self, backend_dockerfile):
        assert re.search(r"^USER\s+creator", backend_dockerfile, re.M), (
            "the backend image runs as root; a script-node escape would then "
            "hold full container privileges"
        )

    def test_backend_is_multi_stage(self, backend_dockerfile):
        """Keeps the build toolchain out of the runtime layer."""
        assert len(re.findall(r"^FROM ", backend_dockerfile, re.M)) >= 2

    def test_backend_writable_state_is_confined_to_data(self, backend_dockerfile):
        assert "/data/media" in backend_dockerfile

    def test_frontend_build_skips_the_electron_binary(self, frontend_dockerfile):
        """The web image never runs Electron; downloading it breaks builds."""
        assert "ELECTRON_SKIP_BINARY_DOWNLOAD" in frontend_dockerfile

    def test_dockerignore_files_exclude_secrets_and_state(self):
        for ignore in (
            REPO_ROOT / "backend" / ".dockerignore",
            REPO_ROOT / "frontend" / ".dockerignore",
        ):
            assert ignore.is_file(), f"{ignore} is missing"
        backend_ignore = (REPO_ROOT / "backend" / ".dockerignore").read_text()
        assert ".env" in backend_ignore
        assert "*.db" in backend_ignore
