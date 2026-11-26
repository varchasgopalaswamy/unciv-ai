"""Bounded native provider/tool protocol smoke; no strategic-quality claims.

Help and --dry-run never open authentication files or make inference requests.
A live invocation selects exactly one provider and uses only a synthetic fixture.
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.capabilities import Caching
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    UsageLimitExceeded,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from civ_agent.orchestration.contracts import ModelSelection
from civ_agent.orchestration.job_contracts import host_usage
from civ_agent.orchestration.provider_capabilities import prepare_profile_contract
from civ_agent.orchestration.provider_contracts import ModelCapabilities, ProviderModelContract
from civ_agent.orchestration.provider_errors import sanitize_model_error
from civ_agent.orchestration.provider_preparation import fetch_openrouter_catalog
from civ_agent.orchestration.providers import ProviderClients, model_settings
from civ_agent.player_settings import setting_argument
from civ_agent.schemas import BoundaryModel
from civ_agent.tls import private_directory, regular_file, write_private_json

REQUEST_LIMIT = 2
OUTPUT_PER_REQUEST = 256
SDK_OUTPUT_LIMIT = 512
TIMEOUT_SECONDS = 120


class FixtureQuery(BoundaryModel):
    city: Literal["harbor-city"]


class FixtureObservation(BoundaryModel):
    city: Literal["harbor-city"] = "harbor-city"
    population: int = 3
    food_per_turn: int = 4


class FixtureAnswer(BoundaryModel):
    city: Literal["harbor-city"]
    population: int
    food_per_turn: int


class SmokeUsage(BoundaryModel):
    requests: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int | None


class SmokeReport(BoundaryModel):
    kind: Literal["native_provider_protocol_smoke"] = "native_provider_protocol_smoke"
    provider: Literal["openai", "openrouter"]
    authentication: Literal["api_key", "subscription"]
    model: str
    reasoning_effort: str
    outcome: Literal[
        "passed",
        "grounding_failed",
        "provider_error",
        "quota",
        "rate_limit",
        "usage_limit",
        "protocol_error",
        "timeout",
        "configuration_error",
        "dry_run",
    ] = "configuration_error"
    contract_sha256: str | None = None
    logical_request_limit: Literal[2] = REQUEST_LIMIT
    timeout_seconds: Literal[120] = TIMEOUT_SECONDS
    provider_output_cap_enforced: bool = False
    provider_max_output_tokens: int | None = None
    sdk_postresponse_output_limit: Literal[512] = SDK_OUTPUT_LIMIT
    sdk_inference_retries: Literal[0] = 0
    definite_401_renewal: Literal["none", "production_bounded_once"] = "none"
    tool_calls: int = 0
    grounded: bool = False
    usage: SmokeUsage | None = None
    usage_complete: bool = False
    http_status: int | None = None
    latency_seconds: float = Field(default=0, ge=0)


def report_for(selection):
    return SmokeReport(
        provider=selection.provider,
        authentication=selection.authentication,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        definite_401_renewal="production_bounded_once" if selection.authentication == "subscription" else "none",
    )


def output_cap(contract: ProviderModelContract) -> bool:
    return "max_tokens" not in (contract.profile.get("openai_unsupported_model_settings") or [])


async def run_smoke(
    clients: ProviderClients, selection: ModelSelection, *, catalog: ModelCapabilities | None = None
) -> SmokeReport:
    """Actual public provider adapter and Agent loop; reports contain no messages."""
    report, usage, start = report_for(selection), RunUsage(), time.monotonic()
    tool_calls = []
    model = None
    try:
        contract = await prepare_profile_contract(selection, catalog=catalog)
        report.contract_sha256 = contract.fingerprint
        report.provider_output_cap_enforced = output_cap(contract)
        report.provider_max_output_tokens = OUTPUT_PER_REQUEST if report.provider_output_cap_enforced else None
        model = await clients.model(selection, contract=contract)
        settings = model_settings(selection)
        settings.update(timeout=TIMEOUT_SECONDS, request_timeout=TIMEOUT_SECONDS)
        if report.provider_output_cap_enforced:
            settings["max_tokens"] = OUTPUT_PER_REQUEST

        async def inspect_fixture(query: FixtureQuery) -> FixtureObservation:
            """Read the one permitted synthetic city before answering; use its exact fields."""
            tool_calls.append(query.city)
            return FixtureObservation()

        agent = Agent(
            model,
            name="native_provider_smoke",
            output_type=ToolOutput(FixtureAnswer, name="submit_fixture"),
            tools=[inspect_fixture],
            capabilities=[Caching()],
            retries=0,
            instructions="This is a synthetic protocol fixture. Call inspect_fixture once for harbor-city, then submit_fixture with exactly the returned city, population and food_per_turn. Do not guess or add prose.",
        )
        async with asyncio.timeout(TIMEOUT_SECONDS):
            result = await agent.run(
                "Inspect harbor-city and submit the observed fields.",
                model_settings=settings,
                usage=usage,
                usage_limits=UsageLimits(
                    request_limit=REQUEST_LIMIT, tool_calls_limit=1, output_tokens_limit=SDK_OUTPUT_LIMIT
                ),
            )
        report.grounded = (
            tool_calls == ["harbor-city"]
            and usage.requests == REQUEST_LIMIT
            and result.output == FixtureAnswer(city="harbor-city", population=3, food_per_turn=4)
        )
        report.outcome = "passed" if report.grounded else "grounding_failed"
        report.usage_complete = True
    except UsageLimitExceeded:
        report.outcome = "usage_limit"
    except ModelAPIError as error:
        sanitized = sanitize_model_error(
            error, system=model.system if model is not None else selection.provider, model_name=selection.model
        )
        report.outcome = (
            "quota"
            if isinstance(sanitized, ModelQuotaExceededError)
            else "rate_limit"
            if isinstance(sanitized, ModelRateLimitError)
            else "provider_error"
        )
        if isinstance(sanitized, ModelHTTPError) and 400 <= sanitized.status_code <= 599:
            report.http_status = sanitized.status_code
    except TimeoutError:
        report.outcome = "timeout"
    except ValueError:
        report.outcome = "configuration_error"
    except Exception:
        # Provider bodies, headers, messages and causes never enter reports or stdout.
        report.outcome = "protocol_error"
    finally:
        report.tool_calls = len(tool_calls)
        report.latency_seconds = round(time.monotonic() - start, 6)
        if usage.requests or usage.input_tokens or usage.output_tokens:
            report.usage = SmokeUsage(
                requests=usage.requests,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cache_read_tokens,
                output_tokens=usage.output_tokens,
                reasoning_output_tokens=host_usage(usage).reasoning_output_tokens,
            )
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--provider", choices=("openai", "openrouter"), required=True)
    result.add_argument("--model", type=setting_argument, required=True)
    result.add_argument(
        "--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"), default="low"
    )
    credentials = result.add_mutually_exclusive_group(required=True)
    credentials.add_argument(
        "--api-key-file", type=Path, help="Explicit owner-only selected-provider API key; no environment fallback"
    )
    credentials.add_argument(
        "--auth-dir",
        type=Path,
        help="DEDICATED native subscription login directory; never use a shared account directory",
    )
    result.add_argument(
        "--catalog-file",
        type=Path,
        help="Optional saved public ModelCapabilities JSON for OpenRouter; live mode otherwise reads its public catalog",
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Owner-only destination; each invocation creates a new private report directory",
    )
    result.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the public profile/limits without authentication or network; OpenRouter requires --catalog-file",
    )
    return result


async def invoke(args) -> SmokeReport:
    selection = ModelSelection(
        provider=args.provider,
        model=args.model,
        authentication="subscription" if args.auth_dir is not None else "api_key",
        reasoning_effort=args.reasoning_effort,
    )
    report = report_for(selection)
    if selection.authentication == "subscription" and selection.provider != "openai":
        return report
    catalog = None
    if selection.provider == "openrouter":
        if args.catalog_file is not None:
            catalog = ModelCapabilities.model_validate_json(regular_file(args.catalog_file).read_bytes())
        elif not args.dry_run:
            catalog = (await fetch_openrouter_catalog({selection.model}))[selection.model]
    contract = await prepare_profile_contract(selection, catalog=catalog)
    if args.dry_run:
        report.outcome, report.contract_sha256 = "dry_run", contract.fingerprint
        report.provider_output_cap_enforced = output_cap(contract)
        report.provider_max_output_tokens = OUTPUT_PER_REQUEST if report.provider_output_cap_enforced else None
        return report
    async with AsyncExitStack() as resources:
        broker = None
        if args.auth_dir is not None:
            # Reuse production generation-aware auth and its one definite401
            # renewal. No credential-source policy or HTTP transport is copied.
            from aiohttp import web

            from civ_agent.container_auth import LoginBroker, TokenClient
            from civ_agent.subscription_auth import NativeAccount

            temporary = resources.enter_context(tempfile.TemporaryDirectory(prefix="native-smoke-auth-ipc-"))
            ipc = Path(temporary)
            token = secrets.token_urlsafe(32)
            write_private_json(ipc / "clients.json", {"smoke": hashlib.sha256(token.encode()).hexdigest()})
            write_private_json(ipc / "worker.json", {"token": token})
            runner = web.AppRunner(
                LoginBroker(ipc, account=NativeAccount(args.auth_dir)).application(), access_log=None
            )
            await runner.setup()
            resources.push_async_callback(runner.cleanup)
            await web.UnixSite(runner, str(ipc / "broker.sock")).start()
            broker = TokenClient(ipc / "broker.sock", ipc / "worker.json")
        clients = ProviderClients(
            broker=broker,
            openai_key_file=args.api_key_file if selection.provider == "openai" else None,
            openrouter_key_file=args.api_key_file if selection.provider == "openrouter" else None,
        )
        resources.push_async_callback(clients.close)
        return await run_smoke(clients, selection, catalog=catalog)


def main(argv=None):
    options = parser().parse_args(argv)
    # Validate/create only the explicit output directory. Authentication is
    # lazy and is never initialized by help or profile-only dry runs.
    private_directory(options.output_dir)
    try:
        report = asyncio.run(invoke(options))
    except Exception:
        selection = ModelSelection(
            provider=options.provider,
            model=options.model,
            authentication="subscription" if options.auth_dir else "api_key",
            reasoning_effort=options.reasoning_effort,
        )
        report = report_for(selection)
    destination = Path(tempfile.mkdtemp(prefix="provider-smoke-", dir=options.output_dir)) / "report.json"
    write_private_json(destination, report.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "outcome": report.outcome,
                "report": str(destination),
                "providerOutputCapEnforced": report.provider_output_cap_enforced,
            }
        )
    )
    return 0 if report.outcome in {"passed", "dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
