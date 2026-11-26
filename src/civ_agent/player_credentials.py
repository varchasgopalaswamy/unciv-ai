"""Bounded private player-credential selection for human native API commands."""

import json
import os
import re
import stat


class PlayerCredentialError(ValueError):
    pass


def player_credentials(path, requested):
    try:
        with path.open("rb") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & ~0o600:
                raise PlayerCredentialError(
                    "Player credentials must be a regular file with owner-only permissions (chmod 600)."
                )
            content = source.read(65537)
        if len(content) > 65536:
            raise PlayerCredentialError("Player credentials exceed 64 KiB.")
        config = json.loads(content)
        if not isinstance(config, dict) or set(config) != {"players"}:
            raise ValueError()
        players = config["players"]
        if not isinstance(players, list) or not 1 <= len(players) <= 64:
            raise ValueError()
        names, tokens = set(), set()
        for player in players:
            if (
                not isinstance(player, dict)
                or not {"civilizationId", "token"} <= set(player)
                or not set(player) <= {"civilizationId", "token", "controller"}
                or player.get("controller", "AGENT") not in {"HUMAN", "AGENT"}
            ):
                raise ValueError()
            name, token = player["civilizationId"], player["token"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError()
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise ValueError()
            if name in names or token in tokens:
                raise ValueError()
            names.add(name)
            tokens.add(token)
    except FileNotFoundError:
        raise PlayerCredentialError("No player credentials found. Start or restore the native game first.") from None
    except (ValueError, UnicodeError, TypeError):
        # JSON exceptions may quote credentials. Never include their text or cause.
        raise PlayerCredentialError("Invalid player credentials file.") from None
    matches = [p for p in players if p["civilizationId"].casefold() == requested.casefold()]
    if len(matches) != 1:
        raise PlayerCredentialError("Civilization is missing or ambiguous in the player credentials file.")
    return matches[0]["civilizationId"], matches[0]["token"]
