"""Execution capabilities are installed by the Web run producer, never by a tool."""
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class BrowserRunContext:
    run_id: UUID
    grant_id: UUID
    user_id: UUID


browser_run: ContextVar[BrowserRunContext | None] = ContextVar("browser_run", default=None)
# Also set for later turns containing private browser history.
private_browser_history: ContextVar[bool] = ContextVar("private_browser_history", default=False)


def is_private_browser_run() -> bool:
    return private_browser_history.get() or browser_run.get() is not None
