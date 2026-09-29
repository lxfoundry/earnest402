"""The built browser client, served under `/app/` from this same process.

Mounted as the outermost layer of the application, so a request under `/`,
`/app` or `/app/` is answered here and never travels inward: not through
`PaymentMiddlewareASGI`, not through the pool gate, and not through the path
normalisation that folds slash variants onto a paid pattern. `/app/index` is
a client page whose last segment happens to spell a paid route, and it must
never be confused for one.

Same origin as the paid routes on purpose: the client's `fetch` must read the
`PAYMENT-REQUIRED` header, and with one origin there is no CORS to configure
and no `expose_headers` to forget.
"""

from __future__ import annotations

from pathlib import Path

from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

BASE = "/app/"
_BARE_BASE = BASE.rstrip("/")
_READS = ("GET", "HEAD")

# Vite content-hashes every file it emits under assets/, so a name there never
# refers to different bytes. Everything else -- index.html above all, which
# names the current hashes -- is revalidated on every load, or a buyer keeps a
# page pointing at assets a later deploy no longer has.
_IMMUTABLE = "public, max-age=31536000, immutable"
_REVALIDATE = "no-cache"


class ClientBundle:
    """ASGI middleware answering every request under the client's paths."""

    def __init__(self, app: ASGIApp, *, dist_dir: str) -> None:
        self._app = app
        self._dist = Path(dist_dir).resolve()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_client_path(scope["path"]):
            await self._app(scope, receive, send)
            return
        response = self._respond(scope)
        await response(scope, receive, send)

    def _respond(self, scope: Scope) -> Response:
        if scope["method"] not in _READS:
            return JSONResponse(
                {"detail": "Method Not Allowed"},
                status_code=405,
                headers={"Allow": ", ".join(_READS)},
            )
        path = scope["path"]
        if path in ("/", _BARE_BASE):
            query = scope.get("query_string", b"").decode("latin-1")
            return RedirectResponse(
                BASE + (f"?{query}" if query else ""), status_code=302
            )
        return self._file(path[len(BASE) :])

    def _file(self, relative: str) -> Response:
        found = self._existing_file(relative)
        if found is not None:
            # Decided on the resolved file, not on the path's spelling:
            # `assets/../index.html` names the page, and the page is never
            # immutable.
            immutable = found.is_relative_to(self._dist / "assets")
            return _serve(found, immutable=immutable)
        last_segment = relative.rsplit("/", 1)[-1]
        if "." in last_segment:
            # A missing asset. Answering it with the page would hand a script
            # tag an HTML document, which fails far less legibly than a 404.
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # A client route such as pool/<id>: only the client's router knows it.
        return _serve(self._dist / "index.html", immutable=False)

    def _existing_file(self, relative: str) -> Path | None:
        if not relative:
            return None
        try:
            candidate = (self._dist / relative).resolve()
            if not candidate.is_relative_to(self._dist) or not candidate.is_file():
                return None
        except (OSError, ValueError):
            return None
        return candidate


def _is_client_path(path: str) -> bool:
    return path in ("/", _BARE_BASE) or path.startswith(BASE)


def _serve(path: Path, *, immutable: bool) -> FileResponse:
    return FileResponse(
        path, headers={"Cache-Control": _IMMUTABLE if immutable else _REVALIDATE}
    )
