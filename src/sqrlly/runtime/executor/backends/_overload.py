"""Shared transient-error → ``OverloadError`` mapping for backends.

A backend translates transient failures (rate limits, overloads from the
upstream API) into ``OverloadError`` so the model-downgrade chain in
``execute_with_downgrade`` activates.
"""
from __future__ import annotations

from sqrlly.runtime.result import OverloadError

# CLI errors are message-shaped, not attribute-shaped — a provider CLI
# surfaces overloads as a non-zero exit whose output text carries the
# signal. These substrings (lower-cased) trigger downgrade.
OVERLOAD_SUBSTRINGS = frozenset({"529", "overload"})


def maybe_raise_overload(
    exc: BaseException,
    *,
    message_substrings: frozenset[str],
) -> None:
    """Raise ``OverloadError`` if ``str(exc)`` contains an overload marker.

    The match is case-insensitive against ``message_substrings``. Returns
    silently otherwise — the caller raises its own error so real failures
    propagate.
    """
    msg = str(exc).lower()
    if any(sub in msg for sub in message_substrings):
        raise OverloadError(str(exc)) from exc
