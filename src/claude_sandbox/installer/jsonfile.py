"""Read and write JSON settings files the way ``jq`` 1.7 and later does.

The bash installer merged settings with ``jq``; this replaces it, and a
file the bash wrote is one this rewrites to the same bytes. ``jq`` prints
two-space indented JSON, keeps key order, keeps a number's literal (in
decNumber's canonical spelling, which ``decimal`` shares) and escapes DEL.
"""

import json
from decimal import Decimal
from typing import cast


class Number(str):
    """A JSON number, kept as its literal so a rewrite does not round it."""


Json = None | bool | Number | str | list["Json"] | dict[str, "Json"]


class NotJson(ValueError):
    """The text is not one JSON value ``jq -e .`` would accept."""


def _number(literal: str) -> Number:
    return Number(str(Decimal(literal)))


def _no_constant(name: str) -> Json:
    raise NotJson(f"{name} is not JSON")


def loads(data: bytes) -> Json:
    """Parse one JSON value. ``jq -e .`` also fails on ``null`` and ``false``,
    so those raise too; invalid UTF-8 becomes U+FFFD, as in ``jq``."""
    text = data.decode("utf-8-sig", errors="replace")
    try:
        value = json.loads(
            text,
            parse_int=_number,
            parse_float=_number,
            parse_constant=_no_constant,
        )
    except json.JSONDecodeError as exc:
        raise NotJson(str(exc)) from exc
    if value is None or value is False:
        raise NotJson("null and false are not settings")
    return cast(Json, value)


def _string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def dumps(value: Json, indent: str = "") -> str:
    """``jq .``'s output for ``value``, without the trailing newline."""
    inner = indent + "  "
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{inner}{_string(k)}: {dumps(v, inner)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + indent + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [inner + dumps(v, inner) for v in value]
        return "[\n" + ",\n".join(items) + "\n" + indent + "]"
    if isinstance(value, Number):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    return "null" if value is None else ("true" if value else "false")
