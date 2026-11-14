from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from langfuse import propagate_attributes

P = ParamSpec("P")
T = TypeVar("T")


def langfuse_tags(
    *tags: str,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Propagate a small set of workflow tags through an async operation."""
    unique_tags = list(dict.fromkeys(tags))

    def decorator(
        func: Callable[P, Awaitable[T]],
    ) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with propagate_attributes(tags=unique_tags):
                return await func(*args, **kwargs)

        return wrapper

    return decorator
