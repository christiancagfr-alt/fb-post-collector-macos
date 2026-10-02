"""Security boundaries for the loopback desktop UI and exported sheet values."""
import ipaddress
import os
import re
import secrets
from urllib.parse import urlsplit
from flask import abort, request


class SpreadsheetFormula(str):
    """Only application-generated formulas may be evaluated by Sheets."""


def safe_sheet_value(value):
    if isinstance(value, SpreadsheetFormula):
        return str(value)
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("=", "+", "-", "@")) and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", stripped):
            return "'" + value
    return value


def configure_local_security(app):
    app.secret_key = os.environ.get("FB_COLLECTOR_SESSION_SECRET") or secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")

    @app.before_request
    def protect_loopback_ui():
        try:
            host = urlsplit(request.host_url)
            if host.hostname not in {"localhost", "127.0.0.1", "::1"}:
                abort(403)
            if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
                abort(403)
        except ValueError:
            abort(403)
        # Stop DNS rebinding, cross-site form posts and browser requests to localhost.
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            referer = request.headers.get("Referer")
            if origin or referer:
                try:
                    source = urlsplit(origin or referer)
                    expected = urlsplit(request.host_url)
                    if (source.scheme, source.hostname, source.port) != (expected.scheme, expected.hostname, expected.port):
                        abort(403)
                except ValueError:
                    abort(403)
            # Header-less local scripts remain supported. Browsers send Origin,
            # Referer or Fetch Metadata; no cookie-only authentication is assumed.

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response
