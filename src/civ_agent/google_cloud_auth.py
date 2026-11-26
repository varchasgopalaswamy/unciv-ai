"""Import gcloud ADC for the trusted controller without mounting the gcloud directory."""

import json
import os
from pathlib import Path

from civ_agent.orchestration.worker_configuration import GoogleCloudConfig
from civ_agent.tls import private_directory, regular_file, write_private, write_private_json

CONFIG_NAME = "google-cloud.json"
CREDENTIALS_NAME = "google-cloud-credentials.json"
CONTAINER_CONFIG = Path("/run/secrets/google-cloud.json")
CONTAINER_CREDENTIALS = Path("/run/secrets/google-cloud-credentials.json")


def default_credentials_file() -> Path:
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        return Path(explicit).expanduser().resolve()
    cloud_sdk = Path(os.environ.get("CLOUDSDK_CONFIG", "~/.config/gcloud")).expanduser()
    return (cloud_sdk / "application_default_credentials.json").resolve()


def import_login(root: Path, *, project: str, location: str = "global", credentials_file: Path | None = None):
    source = (credentials_file or default_credentials_file()).expanduser().resolve()
    if not source.is_file():
        raise ValueError("Run gcloud auth application-default login first, or supply --credentials-file PATH")
    load_credentials(source, project=project)
    content = source.read_bytes()
    config = GoogleCloudConfig(project=project, location=location, credentials_file=root / CREDENTIALS_NAME)
    private_directory(root)
    write_private(root / CREDENTIALS_NAME, content)
    write_private_json(root / CONFIG_NAME, config.model_dump(mode="json", exclude={"credentials_file"}))
    return config


def load_credentials(path: Path, *, project: str):
    """Use native credential factories; the selected project owns quota/billing."""
    from google.auth import load_credentials_from_dict
    from google.auth.exceptions import GoogleAuthError
    from google.oauth2 import credentials, service_account

    regular_file(path, private=True)
    try:
        info = json.loads(path.read_text())
        kind = info.get("type") if isinstance(info, dict) else None
        if kind == "authorized_user":
            loaded = credentials.Credentials.from_authorized_user_info(info)
        elif kind == "service_account":
            loaded = service_account.Credentials.from_service_account_info(info)
        else:
            # Preserve the SDK's support for other trusted ADC types, including
            # impersonated and federated credentials, instead of parsing OAuth.
            other, _ = load_credentials_from_dict(info, quota_project_id=project)
            return other
        return loaded.with_quota_project(project)
    except (GoogleAuthError, ValueError, TypeError, KeyError):
        # Credential parser diagnostics can quote private file content.
        raise ValueError("GOOGLE_CLOUD_CREDENTIALS_INVALID") from None


def load_config(path: Path, *, credentials_file: Path) -> GoogleCloudConfig:
    regular_file(path, private=True)
    regular_file(credentials_file, private=True)
    return GoogleCloudConfig.model_validate({**json.loads(path.read_text()), "credentials_file": str(credentials_file)})


def saved_config(root: Path, *, container: bool = False) -> GoogleCloudConfig:
    config = load_config(root / CONFIG_NAME, credentials_file=root / CREDENTIALS_NAME)
    return config.model_copy(update={"credentials_file": CONTAINER_CREDENTIALS}) if container else config
