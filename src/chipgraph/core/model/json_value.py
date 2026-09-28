"""A recursive JSON-value type alias shared by the Design Model's ``attrs`` fields."""

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
"""Any value that round-trips through JSON: used for open-ended entity/relation ``attrs``."""
