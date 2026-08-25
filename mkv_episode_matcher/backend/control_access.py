"""Local same-origin guard for orchestration control-plane routes."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def _is_loopback(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def require_local_control(request: Request) -> None:
    """Reject remote or cross-origin access until secure pairing exists."""

    if request.client is None or not _is_loopback(request.client.host):
        raise HTTPException(
            status_code=403,
            detail="Remote control is disabled until secure pairing is configured",
        )

    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        host = request.headers.get("host", "")
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.netloc.casefold() != host.casefold()
        ):
            raise HTTPException(
                status_code=403,
                detail="Cross-origin control request was refused",
            )

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site not in {None, "same-origin", "none"}:
        raise HTTPException(
            status_code=403,
            detail="Cross-site control request was refused",
        )
