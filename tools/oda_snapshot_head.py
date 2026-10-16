#!/usr/bin/env python3
"""Inspect ODA's backup endpoint using a bounded HTTPS HEAD request only."""

from __future__ import annotations

import getpass
import socket
import ssl
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

URL = "https://oda.ft.dk/odapublish/oda.bak"
AUTHORITY_URL = "https://oda.ft.dk/"
ALLOWED_ORIGIN = ("https", "oda.ft.dk", 443)
TIMEOUT_SECONDS = 12
MAX_REDIRECTS = 3


class UnsafeRedirectError(URLError):
    def __init__(self, target_origin: str) -> None:
        self.target_origin = target_origin
        super().__init__("redirect blocked")


def safe_origin(value: str) -> str:
    """Return only scheme and host/port; never display path or credentials."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "(unparseable destination)"
    if not parsed.scheme or not hostname:
        return "(unparseable destination)"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme.lower()}://{hostname.lower()}{suffix}"


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Follow only HTTPS redirects that remain on the official origin."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: Request,
        fp: HTTPResponse,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        try:
            parsed = urlsplit(newurl)
            origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443)
        except ValueError:
            raise UnsafeRedirectError("(unparseable destination)") from None
        if origin != ALLOWED_ORIGIN or parsed.username or parsed.password:
            raise UnsafeRedirectError(safe_origin(newurl))
        # urllib's default redirect handler turns HEAD into GET, which could
        # download the backup. Construct a new HEAD request explicitly.
        return Request(
            newurl,
            headers=req.headers,
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            method="HEAD",
        )


def main() -> int:
    request = Request(URL, method="HEAD", headers={"User-Agent": "DenmarkBot ODA snapshot HEAD check"})
    try:
        response = _open_head(request)
    except HTTPError as error:
        if error.code == 401:
            error.close()
            username = input("ODA username: ").strip()
            password = getpass.getpass("ODA password (input hidden): ") if username else ""
            if username:
                try:
                    response = _open_head(request, username, password)
                except HTTPError as auth_error:
                    return _report_http_error(auth_error)
                except (TimeoutError, URLError, OSError) as network_error:
                    return _report_network_error(network_error)
            else:
                print("HTTP status: 401")
                print("Authentication: required; no credentials entered.")
                return 1
        else:
            return _report_http_error(error)
    except (TimeoutError, URLError, OSError) as error:
        return _report_network_error(error)

    with response:
        print(f"HTTP status: {response.status}")
        print(f"Final origin: {safe_origin(response.geturl())}")
        for header in ("Content-Length", "Content-Type", "Last-Modified"):
            value = response.headers.get(header)
            print(f"{header}: {value if value is not None else '(not provided)'}")
        print("Request method: HEAD; response body was not read.")
        return 0


def _open_head(request: Request, username: str | None = None, password: str = ""):
    manager = HTTPPasswordMgrWithDefaultRealm()
    if username:
        manager.add_password(None, AUTHORITY_URL, username, password)
    opener = build_opener(HTTPBasicAuthHandler(manager), SameOriginRedirectHandler())
    return opener.open(request, timeout=TIMEOUT_SECONDS)


def _report_http_error(error: HTTPError) -> int:
    try:
        location = error.headers.get("Location")
        print(f"HTTP status: {error.code}")
        if location:
            print(f"Redirect origin: {safe_origin(location)}")
        for header in ("Content-Length", "Content-Type", "Last-Modified"):
            value = error.headers.get(header)
            print(f"{header}: {value if value is not None else '(not provided)'}")
        if error.code == 401:
            print("Authentication: endpoint requires credentials or rejected the supplied credentials.")
        return 1
    finally:
        error.close()


def _report_network_error(error: BaseException) -> int:
    # Never print exception text: urllib errors can include complete URLs.
    if isinstance(error, UnsafeRedirectError):
        print(f"Redirect blocked; target origin: {error.target_origin}")
    elif isinstance(error, TimeoutError) or "timed out" in str(getattr(error, "reason", "")).lower():
        print("Request failed: timeout")
    elif isinstance(getattr(error, "reason", None), socket.gaierror):
        print("Request failed: DNS resolution")
    elif isinstance(getattr(error, "reason", None), ssl.SSLError):
        print("Request failed: TLS/certificate")
    else:
        print(f"Request failed: {type(error).__name__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
