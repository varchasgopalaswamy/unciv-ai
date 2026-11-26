"""Managed provider proofs freeze every selected role before any inference."""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI

from civ_agent.game_config import EconomyModel
from civ_agent.orchestration import provider_preparation
from civ_agent.orchestration.contracts import digest
from civ_agent.orchestration.managed_player import MODEL_ROLES, NativePlayerSession, NativePlayerSettings
from civ_agent.orchestration.provider_capabilities import model_profile_overrides, profile_projection, provider_model
from civ_agent.orchestration.provider_contracts import ModelCapabilities, ReasoningCapabilities
from civ_agent.orchestration.provider_preparation import (
    fetch_openrouter_catalog,
    prepare_managed_sessions,
    session_selections,
)


def session(player="p0", *, adaptive=False):
    return NativePlayerSession(
        game="game",
        player=player,
        settings=NativePlayerSettings(
            model="gpt-6.1-sol",
            reasoning_effort="high",
            communication_reasoning_effort="low",
            fast=True,
            economy_model=EconomyModel(provider="openrouter", model="vendor/economy", reasoning_effort="low"),
            military_model=EconomyModel(provider="openai", model="gpt-6-sol", reasoning_effort="medium"),
            decisions_enabled=adaptive,
            decisions_effort_enabled=adaptive,
            decisions_effort_levels=["low", "medium", "high"],
        ),
    )


async def catalog(models):
    return {
        model: ModelCapabilities(
            id=model,
            context_length=32000,
            supported_parameters=["tools", "structured_outputs", "reasoning"],
            reasoning=ReasoningCapabilities(mandatory=False, supported_efforts=["none", "low", "medium", "high"]),
            endpoints=["/api/v1/responses"],
        )
        for model in models
    }


async def prepared_profile(selection, proof):
    async with AsyncOpenAI(api_key="synthetic-offline") as client:
        model = provider_model(selection, client, profile=model_profile_overrides(selection, proof))
        return profile_projection(model)


def test_contract_dto_import_does_not_load_sdk_or_legacy_provider_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from civ_agent.orchestration.provider_contracts import ProviderModelContract; assert not any(name in sys.modules for name in ('pydantic_ai', 'civ_agent.providers', 'civ_agent.orchestration.provider_capabilities'))",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_all_roles_and_adaptive_variants_freeze_once_and_resume_without_metadata_reads():
    async def run():
        fetch = AsyncMock(side_effect=catalog)
        original = [session(adaptive=True), session("p1", adaptive=True)]
        prepared = await prepare_managed_sessions(original, fetch_catalog=fetch)
        fetch.assert_awaited_once_with({"vendor/economy"})
        for base, frozen in zip(original, prepared, strict=True):
            frozen.require_provider_contracts()
            assert frozen.settings == base.settings and frozen.request_limit == base.request_limit
            assert len(frozen.provider_contracts) == len(session_selections(base))
            for role in MODEL_ROLES:
                selection = frozen.get_selection(role)
                proof = frozen.get_provider_contract(role)
                assert proof is not None and proof.selection_sha256 == digest(selection.model_dump(mode="json"))
                assert await prepared_profile(selection, proof) == proof.profile
                assert proof.source == ("provider-catalog" if selection.provider == "openrouter" else "sdk-profile")
            chosen = frozen.get_selection("gameplay").model_copy(update={"reasoning_effort": "low"})
            assert frozen.get_provider_contract("gameplay", selection=chosen) != frozen.get_provider_contract(
                "gameplay"
            )
            proof = frozen.get_provider_contract("gameplay", selection=chosen)
            assert proof is not None and proof.settings["thinking"] == "low"
        unavailable = AsyncMock(side_effect=AssertionError("Resume must use the original catalog claims"))
        resumed = await prepare_managed_sessions(original, frozen=prepared, fetch_catalog=unavailable)
        assert resumed == prepared
        unavailable.assert_not_awaited()

    asyncio.run(run())


def test_terra_adaptive_variants_freeze_and_resume_without_network_or_capability_overrides():
    async def run():
        original = NativePlayerSession(
            game="terra",
            player="p0",
            settings=NativePlayerSettings(
                model="gpt-6.1-terra",
                reasoning_effort="medium",
                economy_model=EconomyModel(provider="openai", model="gpt-6.1-terra", reasoning_effort="medium"),
                military_model=EconomyModel(provider="openai", model="gpt-6.1-terra", reasoning_effort="medium"),
                decisions_enabled=True,
                decisions_effort_enabled=True,
                decisions_backend="jev",
            ),
        )
        forbidden = AsyncMock(side_effect=AssertionError("First-party preparation must remain network free"))
        prepared = (await prepare_managed_sessions([original], fetch_catalog=forbidden))[0]
        expected = ["low", "medium", "high", "xhigh"]
        for role in ("gameplay", "economy"):
            base = prepared.get_selection(role)
            assert base.authentication == "subscription"
            for effort in expected:
                chosen = base.model_copy(update={"reasoning_effort": effort})
                proof = prepared.get_provider_contract(role, selection=chosen)
                assert proof is not None and proof.settings["thinking"] == effort
                assert proof.source == "sdk-profile" and proof.catalog is None
                assert proof.profile["openai_reasoning_efforts"] is None
            assert (
                prepared.get_provider_contract(role, selection=base.model_copy(update={"reasoning_effort": "max"}))
                is None
            )
        assert {selection.reasoning_effort for selection in session_selections(prepared).values()} == set(expected)
        assert await prepare_managed_sessions([original], frozen=[prepared], fetch_catalog=forbidden) == [prepared]
        forbidden.assert_not_awaited()

    asyncio.run(run())


def test_prepared_session_preserves_typed_metadata_in_private_json():
    from civ_agent.orchestration.managed_player import ConfigureManagedPlayer

    async def run():
        prepared = (await prepare_managed_sessions([session(adaptive=True)], fetch_catalog=catalog))[0]
        request = ConfigureManagedPlayer(session=prepared, expected_epoch="epoch")
        restored = ConfigureManagedPlayer.model_validate_json(request.model_dump_json())
        assert restored == request
        restored.session.require_provider_contracts()

    asyncio.run(run())


def test_unbounded_selected_metadata_and_proof_maps_are_rejected_before_publication():
    from civ_agent.orchestration.provider_contracts import MAX_PROVIDER_CONTRACT_BYTES, ProviderModelContract

    async def run():
        prepared = (await prepare_managed_sessions([session(adaptive=True)], fetch_catalog=catalog))[0]
        oversized = next(iter(prepared.provider_contracts.values())).model_dump(mode="json")
        oversized["profile"]["padding"] = "x" * MAX_PROVIDER_CONTRACT_BYTES
        with pytest.raises(ValueError, match="PROVIDER_CONTRACT_TOO_LARGE"):
            ProviderModelContract.model_validate(oversized)
        contracts = {}
        for key, proof in prepared.provider_contracts.items():
            wire = proof.model_dump(mode="json")
            wire["profile"]["padding"] = "x" * (
                MAX_PROVIDER_CONTRACT_BYTES - len(proof.model_dump_json().encode()) - 64
            )
            contracts[key] = ProviderModelContract.model_validate(wire)
        for index in range(8):
            key = digest({"variant": index})
            contracts[key] = next(iter(contracts.values())).model_copy(update={"selection_sha256": key})
        with pytest.raises(ValueError, match="MANAGED_PLAYER_SESSION_TOO_LARGE"):
            prepared.model_copy(update={"provider_contracts": contracts}).require_provider_contracts()

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["sdk", "settings", "profile", "missing", "foreign"])
def test_retained_proof_drift_fails_without_fetching_or_replacing_metadata(damage):
    async def run():
        original = session()
        prepared = (await prepare_managed_sessions([original], fetch_catalog=catalog))[0]
        wire = prepared.model_dump(mode="json")
        proof = next(iter(wire["provider_contracts"].values()))
        if damage == "sdk":
            proof["sdk_version"] = "changed"
        elif damage == "settings":
            proof["settings"]["max_retries"] = 1
        elif damage == "profile":
            proof["profile"]["supports_tools"] = False
        elif damage == "missing":
            wire["provider_contracts"].pop(next(iter(wire["provider_contracts"])))
        else:
            wire["player"] = "foreign"
        damaged = NativePlayerSession.model_validate(wire)
        forbidden = AsyncMock(side_effect=AssertionError("No fresh metadata during drift rejection"))
        with pytest.raises(ValueError):
            await prepare_managed_sessions([original], frozen=[damaged], fetch_catalog=forbidden)
        forbidden.assert_not_awaited()
        assert damaged.model_dump(mode="json") == wire

    asyncio.run(run())


def test_scripted_fixture_skips_catalog_and_sdk_profile_preparation(monkeypatch):
    async def run():
        scripted = NativePlayerSession(
            game="game",
            player="p0",
            settings=NativePlayerSettings(
                scripted=True,
                provider="openai",
                model="scripted-v1",
                economy_model=EconomyModel(provider="openai", model="scripted-v1", reasoning_effort="medium"),
                military_model=EconomyModel(provider="openai", model="scripted-v1", reasoning_effort="medium"),
            ),
        )
        forbidden = AsyncMock(side_effect=AssertionError("Fixture must remain credential and provider free"))
        monkeypatch.setattr(provider_preparation, "prepare_profile_contract", forbidden)
        assert await prepare_managed_sessions([scripted], fetch_catalog=forbidden) == [scripted]
        forbidden.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["valid", "redirect", "oversize", "missing"])
def test_public_catalog_fetch_has_fixed_destination_no_auth_and_bounded_response(monkeypatch, mode):
    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            assert request.method == "GET" and str(request.url) == "https://openrouter.ai/api/v1/models"
            assert "authorization" not in request.headers and not request.content
            if mode == "redirect":
                return httpx2.Response(302, headers={"location": "https://other.invalid"})
            return httpx2.Response(
                200,
                json={
                    "data": []
                    if mode == "missing"
                    else [
                        {
                            "id": "vendor/economy",
                            "context_length": 32000,
                            "supported_parameters": ["tools", "structured_outputs", "reasoning"],
                        }
                    ]
                },
            )

        native = httpx2.AsyncClient

        class HTTP(native):
            def __init__(self, **kwargs):
                assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False and kwargs["timeout"] == 30
                super().__init__(**kwargs, transport=httpx2.MockTransport(respond))

        monkeypatch.setattr(httpx2, "AsyncClient", HTTP)
        if mode == "oversize":
            monkeypatch.setattr(provider_preparation, "MAX_CATALOG_BYTES", 10)
        if mode == "valid":
            assert (await fetch_openrouter_catalog({"vendor/economy"}))["vendor/economy"].context_length == 32000
        else:
            with pytest.raises(ValueError, match="CATALOG_UNAVAILABLE"):
                await fetch_openrouter_catalog({"vendor/economy"})
        assert len(requests) == 1

    asyncio.run(run())
