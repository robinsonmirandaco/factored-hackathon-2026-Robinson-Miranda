"""Structured JSON logging with a trace_id that follows a request through every component."""

import contextvars
import logging
import uuid
from collections.abc import MutableMapping
from typing import Any

import structlog

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    """Generates a trace id and binds it to the current context.

    Returns:
        The new 16-character hexadecimal trace id.
    """
    tid = uuid.uuid4().hex[:16]
    trace_id_var.set(tid)
    return tid


def _add_trace_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict["trace_id"] = trace_id_var.get()
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Configures stdlib logging and structlog to emit one JSON object per line.

    Args:
        level: Minimum log level name, for example "INFO" or "WARNING".
    """
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "bankagent") -> structlog.stdlib.BoundLogger:
    """Returns a structlog logger bound to a component name.

    Args:
        name: Component name shown in each log line.

    Returns:
        A structlog logger.
    """
    return structlog.get_logger(name)
