"""Self-contained smoke check for a packaged RipWeaver application.

The normal backend startup owns the MakeMKV process boundary and may reconcile
durable work.  A release-build check must not exercise either behavior.  This
module therefore serves only the packaged health response and static frontend
on a temporary loopback port, verifies them, and shuts the server down.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mkv_episode_matcher import __version__

_ASSET_REFERENCE = re.compile(r"(?:href|src)=[\"'](/assets/[^\"']+)[\"']")


class PortableSmokeError(RuntimeError):
    """Raised when the packaged application cannot serve its bundled UI."""


@dataclass(frozen=True)
class PortableSmokeResult:
    """Path-free result suitable for CI output."""

    version: str
    asset_count: int


def frontend_dist_directory() -> Path:
    """Return the bundled frontend directory without exposing candidate paths."""

    frontend = Path(__file__).resolve().parent / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise PortableSmokeError("The packaged frontend index is missing")
    if not (frontend / "assets").is_dir():
        raise PortableSmokeError("The packaged frontend assets are missing")
    return frontend


def build_portable_smoke_app() -> FastAPI:
    """Build a read-only app containing no production or media routes."""

    frontend = frontend_dist_directory()
    smoke_app = FastAPI(
        title="RipWeaver portable smoke check",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    smoke_app.mount(
        "/assets",
        StaticFiles(directory=str(frontend / "assets")),
        name="portable-assets",
    )

    @smoke_app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "mode": "portable-smoke",
        }

    @smoke_app.get("/")
    async def index() -> FileResponse:
        return FileResponse(frontend / "index.html")

    return smoke_app


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _read_url(url: str, *, timeout: float) -> bytes:
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
        if response.status != 200:
            raise PortableSmokeError("The portable server returned an error")
        return response.read()


def _wait_for_health(
    *,
    base_url: str,
    deadline: float,
    server_failure: list[BaseException],
) -> None:
    health_payload: dict[str, object] | None = None
    while time.monotonic() < deadline:
        if server_failure:
            raise PortableSmokeError("The portable server failed during startup")
        try:
            health_payload = json.loads(_read_url(f"{base_url}/health", timeout=1.0))
            break
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.1)

    if health_payload is None:
        raise PortableSmokeError("The portable server did not become ready")
    if health_payload != {
        "status": "ok",
        "version": __version__,
        "mode": "portable-smoke",
    }:
        raise PortableSmokeError("The portable health response was invalid")


def _verify_frontend(base_url: str) -> int:
    index = _read_url(f"{base_url}/", timeout=5.0).decode("utf-8")
    asset_paths = sorted(set(_ASSET_REFERENCE.findall(index)))
    if not asset_paths:
        raise PortableSmokeError("The packaged frontend contains no asset references")
    for asset_path in asset_paths:
        payload = _read_url(f"{base_url}{asset_path}", timeout=10.0)
        if not payload:
            raise PortableSmokeError("A packaged frontend asset was empty")
    return len(asset_paths)


def run_portable_smoke_check(
    *, startup_timeout_seconds: float = 30.0
) -> PortableSmokeResult:
    """Start, verify, and gracefully stop the minimal packaged server."""

    if startup_timeout_seconds <= 0:
        raise PortableSmokeError("The portable smoke timeout must be positive")

    port = _unused_loopback_port()
    server = uvicorn.Server(
        uvicorn.Config(
            build_portable_smoke_app(),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            lifespan="off",
        )
    )
    server.install_signal_handlers = lambda: None
    server_failure: list[BaseException] = []

    def run_server() -> None:
        try:
            server.run()
        except BaseException as error:  # Preserve an exact failure for the caller.
            server_failure.append(error)

    thread = threading.Thread(
        target=run_server,
        name="ripweaver-portable-smoke",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + startup_timeout_seconds
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_for_health(
            base_url=base_url,
            deadline=deadline,
            server_failure=server_failure,
        )
        return PortableSmokeResult(
            version=__version__,
            asset_count=_verify_frontend(base_url),
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        if thread.is_alive():
            raise PortableSmokeError("The portable server did not shut down cleanly")
