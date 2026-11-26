"""Trusted managed-startup metadata discovery, before any model job is admitted."""

import json
from collections.abc import Awaitable, Callable, Sequence

import httpx2

from civ_agent.orchestration.contracts import ModelSelection, digest
from civ_agent.orchestration.managed_player import MODEL_ROLES, NativePlayerSession
from civ_agent.orchestration.provider_capabilities import prepare_profile_contract
from civ_agent.orchestration.provider_contracts import ModelCapabilities

MAX_CATALOG_BYTES = 16 * 1024 * 1024
CATALOG_URL = "https://openrouter.ai/api/v1/models"


async def fetch_openrouter_catalog(models: set[str]) -> dict[str, ModelCapabilities]:
    """One bounded read of public metadata; no token or game content is sent."""
    if not models:
        return {}
    try:
        async with httpx2.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", CATALOG_URL, headers={"Accept": "application/json"}) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_CATALOG_BYTES:
                        raise ValueError("WORKFLOW_CATALOG_TOO_LARGE")
        raw = json.loads(content)
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
            raise ValueError("WORKFLOW_CATALOG_INVALID")
        selected = {}
        for row in raw["data"]:
            if isinstance(row, dict) and row.get("id") in models:
                value = ModelCapabilities.model_validate(row)
                if value.id in selected:
                    raise ValueError("WORKFLOW_CATALOG_DUPLICATE")
                selected[value.id] = value
        if set(selected) != models:
            raise ValueError("WORKFLOW_CATALOG_MODEL_MISSING")
        return selected
    except (httpx2.HTTPError, ValueError, TypeError):
        raise ValueError("WORKFLOW_CATALOG_UNAVAILABLE") from None


def session_selections(session: NativePlayerSession) -> dict[str, ModelSelection]:
    selected = [session.get_selection(role) for role in MODEL_ROLES]
    if session.settings.decisions_effort_enabled:
        from civ_agent.orchestration.effort import supported_levels

        for role in ("gameplay", "economy"):
            base = session.get_selection(role)
            for level in session.settings.decisions_effort_levels:
                if level in supported_levels(base):
                    selected.append(base.model_copy(update={"reasoning_effort": level}))
    return {digest(selection.model_dump(mode="json")): selection for selection in selected}


async def prepare_managed_sessions(
    sessions: Sequence[NativePlayerSession],
    *,
    frozen: Sequence[NativePlayerSession] | None = None,
    fetch_catalog: Callable[[set[str]], Awaitable[dict[str, ModelCapabilities]]] | None = None,
) -> list[NativePlayerSession]:
    """Freeze once; retained claims are validated offline and never replaced on resume."""
    previous = {session.player: session for session in frozen} if frozen is not None else {}
    if frozen is not None and set(previous) != {session.player for session in sessions}:
        raise ValueError("MANAGED_PROVIDER_PLAYERS_CHANGED")
    catalogs: dict[str, ModelCapabilities] = {}
    for session in previous.values():
        session.require_provider_contracts()
        for contract in session.provider_contracts.values():
            if contract.catalog is not None:
                old = catalogs.setdefault(contract.catalog.id, contract.catalog)
                if old != contract.catalog:
                    raise ValueError("MANAGED_PROVIDER_CATALOG_CONFLICT")
    wanted = {
        selection.model
        for session in sessions
        if not session.settings.scripted
        for selection in session_selections(session).values()
        if selection.provider == "openrouter"
    }
    missing = wanted - set(catalogs)
    if missing:
        if frozen is not None:
            raise ValueError("MANAGED_PROVIDER_CONTRACT_REQUIRED")
        catalogs.update(await (fetch_catalog or fetch_openrouter_catalog)(missing))
    result = []
    for session in sessions:
        if session.settings.scripted:
            session.require_provider_contracts()
            result.append(session)
            continue
        selections = session_selections(session)
        prior = previous.get(session.player)
        if prior is not None and set(prior.provider_contracts) != set(selections):
            raise ValueError("MANAGED_PROVIDER_SELECTIONS_CHANGED")
        contracts = {}
        for key, selection in selections.items():
            contracts[key] = await prepare_profile_contract(
                selection,
                catalog=catalogs.get(selection.model) if selection.provider == "openrouter" else None,
                existing=prior.provider_contracts[key] if prior is not None else None,
            )
        prepared = session.model_copy(update={"provider_contracts": contracts})
        prepared.require_provider_contracts()
        result.append(prepared)
    return result
