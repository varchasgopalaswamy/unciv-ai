"""Provider names and private credential-file validation, independent of model runners."""

from pathlib import Path
from typing import Literal

from civ_agent.orchestration.provider_contracts import ProviderError
from civ_agent.tls import regular_file

Provider = Literal["openai", "openrouter", "google-cloud"]
PROVIDERS = ("openai", "openrouter", "google-cloud")


def valid_api_key(value):
    return isinstance(value, str) and 1 <= len(value) <= 4096 and all("!" <= c <= "~" for c in value)


def read_api_key(path):
    regular_file(Path(path), private=True)
    value = Path(path).read_text().strip()
    if not valid_api_key(value):
        raise ProviderError("OPENROUTER_KEY_INVALID", "Invalid OpenRouter API key file")
    return value
