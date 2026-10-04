"""Single error contract for the API: every failure returns {error_code, message, trace_id}.

Stack traces are logged, never sent to the client.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, trace_id_var

log = get_logger("errors")


class AppError(Exception):
    """An expected failure with a stable machine-readable code.

    Attributes:
        error_code: Stable identifier clients can branch on, for example "case_not_found".
        message: Human-readable explanation, safe to show to the client.
        status_code: HTTP status returned to the client.
        headers: Extra response headers, such as Retry-After on a 429.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        status_code: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.headers = headers


def error_response(
    status_code: int, error_code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Builds the error envelope for the current request.

    Args:
        status_code: HTTP status to return.
        error_code: Stable machine-readable code.
        message: Client-safe explanation.
        headers: Extra response headers.

    Returns:
        A JSON response with error_code, message and the current trace_id.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "message": message, "trace_id": trace_id_var.get()},
        headers=headers,
    )


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status_code, exc.error_code, exc.message, exc.headers)


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ()))
    return error_response(422, "validation_error", f"{loc}: {first.get('msg', 'invalid input')}")


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return error_response(exc.status_code, f"http_{exc.status_code}", str(exc.detail))


# Said to the customer, in both languages of the service, when the database cannot be reached:
# the request's language cannot be known without the database (TRZ-36 CA2).
MAINTENANCE = (
    "Estamos en mantenimiento. No se hizo ningún cambio; intenta de nuevo en unos minutos. / "
    "Estamos em manutenção. Nenhuma alteração foi feita; tente novamente em alguns minutos."
)


async def _db_unreachable(_request: Request, exc: Exception) -> JSONResponse:
    # The audit log lives in the database that failed, so the failure is only logged here.
    log.error("db_unavailable", error=type(exc).__name__)
    return error_response(503, "db_unavailable", MAINTENANCE)


def register_error_handlers(app: FastAPI) -> None:
    """Installs the handlers that map every known exception type to the error envelope.

    Unhandled exceptions are turned into the envelope by the trace middleware, which is the
    only place that still has the trace_id when they surface.

    Args:
        app: The FastAPI application.
    """
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    # A lost connection rolls the request's transaction back: no action is left half done.
    app.add_exception_handler(OperationalError, _db_unreachable)
    app.add_exception_handler(InterfaceError, _db_unreachable)
