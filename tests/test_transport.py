"""Transport selection wiring, without starting a real server."""

from __future__ import annotations

from notes_mcp import __main__ as entry
from notes_mcp.server import mcp


def _recorder():
    calls: list[tuple[str, dict]] = []

    def run(server, transport, **kwargs):
        calls.append((transport, kwargs))

    return calls, run


def test_default_transport_is_stdio():
    calls, run = _recorder()
    entry.main([], run=run)
    assert calls == [("stdio", {})]


def test_streamable_http_wiring():
    calls, run = _recorder()
    entry.main(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8767"],
        run=run,
    )
    assert len(calls) == 1
    transport, kwargs = calls[0]
    assert transport == "streamable-http"
    assert kwargs["streamable_http_path"] == "/mcp"
    assert kwargs["stateless_http"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8767


def test_healthz_route_is_registered():
    assert any(
        getattr(route, "path", None) == "/healthz"
        for route in mcp._custom_starlette_routes
    )


def test_streamable_app_exposes_mcp_and_healthz():
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths
    assert "/healthz" in paths
