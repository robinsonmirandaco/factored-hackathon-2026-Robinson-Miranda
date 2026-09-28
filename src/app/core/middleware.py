"""Request middleware: binds a trace_id to every request and catches unhandled errors."""

import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.core.errors import error_response
from app.core.logging import get_logger, new_trace_id, trace_id_var

log = get_logger("api")

TRACE_HEADER = "x-trace-id"
# Matches the width of the trace_id columns, so a client value can never break an insert. The
# first character is alphanumeric so a client cannot send "-", which the audit log refuses.
_VALID_TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def install_trace_middleware(app: FastAPI) -> None:
    """Adds the middleware that assigns the trace_id and returns it in a response header.

    A client-supplied `x-trace-id` is reused only if it is short and safe; otherwise a new one
    is generated. Unhandled exceptions become a 500 error envelope carrying the same trace_id.

    Args:
        app: The FastAPI application.
    """

    @app.middleware("http")
    async def trace(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(TRACE_HEADER, "")
        if _VALID_TRACE_ID.match(incoming):
            trace_id_var.set(incoming)
            tid = incoming
        else:
            tid = new_trace_id()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error", path=request.url.path)
            response = error_response(500, "internal_error", "Internal server error.")
        response.headers[TRACE_HEADER] = tid
        return response
