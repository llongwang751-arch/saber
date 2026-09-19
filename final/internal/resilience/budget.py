"""Request-local, thread-safe dispatch limits; retries consume budget too."""
from contextvars import ContextVar, copy_context
from contextlib import contextmanager
from threading import Lock

_current = ContextVar('execution_budget', default=None)


class BudgetExceeded(RuntimeError):
    pass


@contextmanager
def request_budget(llm_calls=24, tool_calls=32):
    token = _current.set((Lock(), {'llm': max(1, int(llm_calls)), 'tool': max(1, int(tool_calls))}))
    try:
        yield
    finally:
        _current.reset(token)


def charge(kind):
    current = _current.get()
    if current is None:
        return
    lock, remaining = current
    with lock:
        if remaining[kind] <= 0:
            raise BudgetExceeded(f'{kind} call budget exhausted')
        remaining[kind] -= 1


def inherit_context(fn):
    context = copy_context()
    return lambda *args, **kwargs: context.run(fn, *args, **kwargs)
