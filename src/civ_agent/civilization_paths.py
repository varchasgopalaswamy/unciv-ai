"""Stable, injective private-directory names for civilization-owned files."""


class TurnArchiveError(ValueError):
    pass


def civilization_directory_name(civilization):
    """Encode one path segment injectively; ordinary civilization names stay readable."""
    if not isinstance(civilization, str) or not civilization:
        raise TurnArchiveError("Civilization name must be a nonempty string.")
    try:
        encoded = "".join(
            chr(byte) if chr(byte).isascii() and (chr(byte).isalnum() or byte in (45, 95)) else f"%{byte:02X}"
            for byte in civilization.encode("utf-8")
        )
    except UnicodeError:
        raise TurnArchiveError("Civilization name must be valid Unicode.") from None
    if len(encoded) > 255:
        raise TurnArchiveError("Civilization name is too long for a directory name.")
    return encoded
