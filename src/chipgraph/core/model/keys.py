"""The Design Model key grammar: how every entity gets a stable, parseable ``key``.

A model key has the form::

    <kind>:<part>[.<part>...]

``kind`` is the entity kind (``block``, ``module``, ``port``, ``register``, ``field``,
``requirement``, ...). The parts after ``:`` are joined with ``.`` and describe the
entity's position, most-general first. There is no single fixed number of parts: each
kind picks a grammar that is convenient and stable for that kind, for example:

======================  ===============================
Kind                     Example key
======================  ===============================
``project``              ``project:qsoc``
``block``                ``block:timer``
``module``               ``module:tiny_timer``
``port``                 ``port:tiny_timer.i_clk``
``clock``                ``clock:peri``
``reset``                ``reset:rst_n``
``parameter``            ``parameter:tiny_timer.WIDTH``
``register``             ``register:timer.CTRL``
``field``                ``field:timer.CTRL.EN``
``interrupt``            ``interrupt:timer.overflow``
``memory_region``        ``memory_region:timer``
``interface``            ``interface:timer.apb``
``requirement``          ``requirement:REQ-TIM-004``
``decision``             ``decision:D-timer-0001``
``open_item``            ``open_item:OI-timer-0001``
``test``                 ``test:tb_timer_cnt.test_overflow``
======================  ===============================

A part must not contain ``:`` (reserved for the kind separator) or be empty. Parts may
contain ``.`` internally only if that ``.`` is not meant as a separator; callers that
need a literal ``.`` inside a part should avoid this grammar or escape at a higher
layer, since ``parse_key`` always splits on every ``.``.

Extension kinds registered by packs (:mod:`chipgraph.core.model.registry`) follow the
same grammar: ``<pack_kind>:<part>[.<part>...]``.
"""

from __future__ import annotations

_KIND_SEPARATOR = ":"
_PART_SEPARATOR = "."


class InvalidKeyError(ValueError):
    """Raised when a model key does not follow the key grammar."""


def make_key(kind: str, *parts: str) -> str:
    """Build a model key for ``kind`` from one or more ``parts``.

    Example: ``make_key("register", "timer", "CTRL") == "register:timer.CTRL"``.
    """
    if not kind:
        raise InvalidKeyError("kind must not be empty")
    if _KIND_SEPARATOR in kind:
        raise InvalidKeyError(f"kind must not contain {_KIND_SEPARATOR!r}: {kind!r}")
    if not parts:
        raise InvalidKeyError(f"make_key({kind!r}) needs at least one part")
    for part in parts:
        _check_part(part)
    return f"{kind}{_KIND_SEPARATOR}{_PART_SEPARATOR.join(parts)}"


def parse_key(key: str) -> tuple[str, tuple[str, ...]]:
    """Split a model key into ``(kind, parts)``. Raises `InvalidKeyError` if malformed."""
    if _KIND_SEPARATOR not in key:
        raise InvalidKeyError(f"not a valid model key (missing {_KIND_SEPARATOR!r}): {key!r}")
    kind, _, rest = key.partition(_KIND_SEPARATOR)
    if not kind:
        raise InvalidKeyError(f"not a valid model key (empty kind): {key!r}")
    if not rest:
        raise InvalidKeyError(
            f"not a valid model key (no parts after {_KIND_SEPARATOR!r}): {key!r}"
        )
    parts = tuple(rest.split(_PART_SEPARATOR))
    for part in parts:
        _check_part(part)
    return kind, parts


def key_kind(key: str) -> str:
    """Return just the kind of a model key, without fully parsing its parts."""
    kind, _ = parse_key(key)
    return kind


def _check_part(part: str) -> None:
    if not part:
        raise InvalidKeyError("key parts must not be empty")
    if _KIND_SEPARATOR in part:
        raise InvalidKeyError(f"key parts must not contain {_KIND_SEPARATOR!r}: {part!r}")
