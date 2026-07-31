import pytest
from fastapi import HTTPException
from starlette.requests import Request

from mkv_episode_matcher.backend.control_access import require_local_control


def _request(
    client_host: str,
    *,
    host: str = "127.0.0.1:8000",
    origin: str | None = None,
    fetch_site: str | None = None,
) -> Request:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/rip/jobs",
        "headers": headers,
        "client": (client_host, 50000),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
    })


def test_loopback_same_origin_control_is_allowed():
    require_local_control(
        _request(
            "127.0.0.1",
            origin="http://127.0.0.1:8000",
            fetch_site="same-origin",
        )
    )
    require_local_control(_request("::1"))


def test_remote_control_is_refused_until_pairing_exists():
    with pytest.raises(HTTPException, match="Remote control"):
        require_local_control(_request("192.168.1.50"))


def test_cross_origin_control_is_refused():
    with pytest.raises(HTTPException, match="Cross-origin"):
        require_local_control(
            _request(
                "127.0.0.1",
                origin="https://example.invalid",
            )
        )


def test_cross_site_fetch_is_refused():
    with pytest.raises(HTTPException, match="Cross-site"):
        require_local_control(_request("127.0.0.1", fetch_site="cross-site"))
