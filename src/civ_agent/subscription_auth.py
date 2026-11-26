"""Native subscription login and centralized renewable credentials, without a CLI."""

import asyncio
import inspect
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import httpx2
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc.errors import JoseError
from joserfc.jws import extract_compact
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator
from pydantic_ai.providers.openai_codex import (
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
    OpenAICodexOAuthFlow,
)

from civ_agent.protocol import JsonObject
from civ_agent.schemas import BoundaryModel
from civ_agent.tls import private_directory, regular_file, write_private_json

CREDENTIAL_FILE = "openai-subscription.json"
_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_EXPIRY_LEEWAY = 30
_ERROR = "Subscription authentication unavailable; run civ-agent auth login."


class NativeAuthError(RuntimeError):
    """Sanitized login, renewal or credential-persistence failure."""


class _ExpiryHint(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False, hide_input_in_errors=True)
    exp: StrictInt | StrictFloat | None = None


def _expires_at(credentials: OpenAICodexCredentials) -> int | float | None:
    # A public JOSE parser supplies a bounded unverified expiry hint. It never
    # authenticates an account or determines which account receives a request.
    try:
        payload = extract_compact(credentials.access_token.encode()).payload
        return _ExpiryHint.model_validate_json(payload).exp
    except (ValueError, JoseError, UnicodeError):
        return None


class _StoredCredentials(BoundaryModel):
    version: Literal[1] = 1
    credentials: OpenAICodexCredentials = Field(repr=False)
    expiresAt: StrictInt | StrictFloat | None = None

    @model_validator(mode="after")
    def complete(self):
        values = (self.credentials.access_token, self.credentials.refresh_token, self.credentials.account_id)
        if any(not isinstance(value, str) or not value or any(ord(char) <= 32 for char in value) for value in values):
            raise ValueError("INCOMPLETE_CREDENTIALS")
        if self.expiresAt is not None and not (-1e20 < self.expiresAt < 1e20):
            raise ValueError("INVALID_EXPIRY")
        return self


class CredentialFile(OpenAICodexCredentialSource):
    """Public native credential-source protocol backed by one private atomic file."""

    def __init__(self, root: Path):
        self.root, self.path = Path(root), Path(root) / CREDENTIAL_FILE

    async def _read(self) -> _StoredCredentials:
        def read():
            private_directory(self.root)
            regular_file(self.path, private=True)
            if self.path.stat().st_size > 1_048_576:
                raise ValueError("CREDENTIAL_FILE_TOO_LARGE")
            return _StoredCredentials.model_validate_json(self.path.read_bytes())

        try:
            return await asyncio.to_thread(read)
        except Exception:
            raise NativeAuthError(_ERROR) from None

    async def _write(self, record: _StoredCredentials):
        def write():
            private_directory(self.root)
            validated = _StoredCredentials.model_validate_json(record.model_dump_json())
            write_private_json(self.path, validated.model_dump(mode="json"))

        try:
            accepted = asyncio.create_task(asyncio.to_thread(write))
            cancelled = False
            while not accepted.done():
                try:
                    await asyncio.shield(accepted)
                except asyncio.CancelledError:
                    # Match ServiceIO's accepted-write semantics: even repeated
                    # cancellation must finish the save before releasing its lock.
                    cancelled = True
            accepted.result()
            if cancelled:
                raise asyncio.CancelledError
        except Exception:
            raise NativeAuthError(_ERROR) from None

    async def load(self) -> OpenAICodexCredentials:
        return (await self._read()).credentials

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        try:
            record = _StoredCredentials(credentials=credentials, expiresAt=_expires_at(credentials))
            await self._write(record)
        except Exception:
            raise NativeAuthError(_ERROR) from None


class NativeAccount:
    """One broker owns renewal; workers receive access tokens and account IDs only."""

    def __init__(self, root: Path, *, transport: httpx2.AsyncBaseTransport | None = None):
        self.source = CredentialFile(root)
        self.transport = transport
        self.lock = asyncio.Lock()
        self.record: _StoredCredentials | None = None
        self.client: AsyncOAuth2Client | None = None
        self.unsaved = False

    async def start(self):
        async with self.lock:
            if self.client is not None:
                return
            try:
                record = self.record if self.unsaved else await self.source._read()
                client = AsyncOAuth2Client(
                    _CLIENT_ID,
                    token_endpoint_auth_method="none",
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx2.Timeout(8, connect=5),
                )
                self.record, self.client = record, client
            except Exception:
                raise NativeAuthError(_ERROR) from None

    async def close(self):
        async with self.lock:
            if self.client is not None:
                try:
                    if self.unsaved:
                        assert self.record is not None
                        await self.source._write(self.record)
                        self.unsaved = False
                except Exception:
                    raise NativeAuthError(_ERROR) from None
                finally:
                    try:
                        await cast(httpx2.AsyncClient, self.client).aclose()
                    except Exception:
                        raise NativeAuthError(_ERROR) from None
                    finally:
                        self.client = None
                        if not self.unsaved:
                            self.record = None

    async def tokens(self, *, force_refresh: bool = False) -> JsonObject:
        await self.start()
        observed = self.record
        async with self.lock:
            assert self.record is not None and self.client is not None
            # A rotated bundle remains in memory if durable storage failed. Retry
            # that save before issuing it, rather than spending another grant.
            if self.unsaved:
                await self.source._write(self.record)
                self.unsaved = False
                return self._tokens()
            stale = self.record.expiresAt is not None and time.time() >= self.record.expiresAt - _EXPIRY_LEEWAY
            if stale or (force_refresh and self.record is observed):
                previous = self.record.credentials
                try:
                    async with asyncio.timeout(8):
                        token = await self.client.refresh_token(_TOKEN_ENDPOINT, refresh_token=previous.refresh_token)
                    # Renewal cannot switch the broker's authenticated account.
                    if token.get("account_id", previous.account_id) != previous.account_id:
                        raise ValueError("ACCOUNT_CHANGED")
                    credentials = OpenAICodexCredentials(
                        access_token=token["access_token"],
                        refresh_token=token["refresh_token"],
                        account_id=previous.account_id,
                    )
                    expiry = token.get("expires_at")
                    hint = _expires_at(credentials)
                    if hint is not None:
                        expiry = min(expiry, hint) if expiry is not None else hint
                    self.record = _StoredCredentials(credentials=credentials, expiresAt=expiry)
                    self.unsaved = True
                    await self.source._write(self.record)
                    self.unsaved = False
                except Exception:
                    raise NativeAuthError(_ERROR) from None
            return self._tokens()

    def _tokens(self) -> JsonObject:
        assert self.record is not None
        return {
            "accessToken": self.record.credentials.access_token,
            "chatgptAccountId": self.record.credentials.account_id,
        }


async def login(root: Path, *, authorization_callback: Callable[[str], object] = print) -> None:
    """Print the native PKCE authorization URL and await its localhost:1455 callback."""
    try:
        flow = OpenAICodexOAuthFlow()
        notified = authorization_callback(flow.authorization_url())
        if inspect.isawaitable(notified):
            await notified
        async with asyncio.timeout(300):
            credentials = await flow.exchange_code_from_callback()
        await CredentialFile(root).save(credentials)
    except Exception:
        raise NativeAuthError(_ERROR) from None
