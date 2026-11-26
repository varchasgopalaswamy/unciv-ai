"""Native-tool dispatch through the established domain guards and receipt journal.

`decision` is an internal batch assembled from typed tool arguments. No envelope
generation, model subprocess, generated program or automatic command retry runs
here. The shared domain executor owns prerequisites and native receipt recovery.
"""

import copy

from civ_agent.decision_contracts import DecisionContract
from civ_agent.decision_journal import check_epoch, journal
from civ_agent.gateway_batches import BatchExecutor
from civ_agent.orchestration.contracts import GatewayReceipt, digest
from civ_agent.orchestration.gateway_state import GatewayState, capture_job
from civ_agent.orchestration.store import JournalConflict
from civ_agent.proposal_gateway import JobAccess, ProposalGateway
from civ_agent.protocol import JsonObject


class GatewayExecution(GatewayReceipt):
    state: GatewayState


def native_call_id(call_id: str) -> str:
    """Keep SDK identities in histories; use bounded identities at native boundaries."""
    if 1 <= len(call_id) <= 128 and all(33 <= ord(char) <= 126 for char in call_id):
        return call_id
    return "sdk_" + digest(call_id)


def operation_identity(job: str, epoch: str, model_step: int, call_id: str) -> str:
    return "native:" + digest([job, epoch, model_step, native_call_id(call_id)])


class NativeGatewayExecutor:
    def __init__(self, gateway: ProposalGateway, job: JobAccess, contract: DecisionContract, state: GatewayState):
        if (job.id, job.purpose, job.epoch) != (state.job, state.purpose, state.epoch):
            raise JournalConflict("GATEWAY_STATE_IDENTITY_CONFLICT")
        self.gateway, self.job, self.contract = gateway, job, contract
        self.loop = BatchExecutor(gateway, job, contract)
        self.loop.delivered_access = copy.deepcopy(state.delivered_access)
        self.loop.delivered_revision = state.delivered_access.get("revision")
        self.loop.review = copy.deepcopy(state.military_review)
        self.loop.review_targets = set(state.military_targets)
        self.delivery_step = state.delivery_step
        self.pending_access = copy.deepcopy(state.pending_access)
        self.pending_review = copy.deepcopy(state.pending_military_review)
        self.pending_targets = set(state.pending_military_targets)

    def capture(self) -> GatewayState:
        return capture_job(
            self.job,
            delivered_access=self.loop.delivered_access,
            military_review=self.loop.review,
            military_targets=self.loop.review_targets,
            delivery_step=self.delivery_step,
            pending_access=self.pending_access,
            pending_military_review=self.pending_review,
            pending_military_targets=self.pending_targets,
        )

    def _begin_step(self, step: int):
        if type(step) is not int or step < 1 or step < self.delivery_step:
            raise JournalConflict("GATEWAY_MODEL_STEP_REWOUND")
        if step > self.delivery_step:
            # A new provider response establishes that earlier tool results were
            # supplied. Sibling tools in one response cannot see each other's reads.
            if self.pending_access is not None:
                self.loop.delivered_access = copy.deepcopy(self.pending_access)
                self.loop.delivered_revision = self.pending_access.get("revision")
                self.loop.review = copy.deepcopy(self.pending_review)
                self.loop.review_targets = set(self.pending_targets)
            self.pending_access, self.pending_review, self.pending_targets = None, None, set()
            self.delivery_step = step

    def _stage_delivery(self, result: JsonObject):
        access, review, targets = self.loop.delivered_access, self.loop.review, self.loop.review_targets
        revision = self.loop.delivered_revision
        self.loop.delivered_access = copy.deepcopy(self.pending_access if self.pending_access is not None else access)
        self.loop.review = copy.deepcopy(self.pending_review if self.pending_access is not None else review)
        self.loop.review_targets = set(self.pending_targets if self.pending_access is not None else targets)
        self.loop.delivered(result)
        self.pending_access = self.loop.delivered_access
        self.pending_review, self.pending_targets = self.loop.review, self.loop.review_targets
        self.loop.delivered_access, self.loop.review, self.loop.review_targets = access, review, targets
        self.loop.delivered_revision = revision

    async def execute(self, call_id: str, decision: JsonObject, *, model_step: int) -> GatewayExecution:
        # Canonical validation checks the entire batch before any child is
        # admitted. Pydantic AI tool schemas will supply these same owned models.
        parsed = self.contract.parse(self.contract.encode(copy.deepcopy(decision)))
        self.gateway.check(self.job, ended_allowed=True)
        await self.gateway.io.call(check_epoch, self.gateway.memory, self.job.epoch)
        identity = operation_identity(self.job.id, self.job.epoch, model_step, call_id)
        saved = await self.gateway.io.call(journal, self.gateway.memory, identity)
        if saved is not None:
            if (saved["job"], saved["epoch"], saved["purpose"]) != (self.job.id, self.job.epoch, self.job.purpose):
                raise JournalConflict("GATEWAY_OPERATION_IDENTITY_CONFLICT")
            if saved["decision"] != parsed:
                raise JournalConflict("GATEWAY_OPERATION_CHANGED")
            self.loop.current = saved
            result = await self.loop.execute(recovery=True)
            if saved.get("gatewayState") is not None:
                # Return the immutable post-call context; never roll a live job
                # backwards by applying an old activity's snapshot to it here.
                state = GatewayState.model_validate(saved["gatewayState"])
                if (state.job, state.purpose, state.epoch) != (self.job.id, self.job.purpose, self.job.epoch):
                    raise JournalConflict("GATEWAY_STATE_IDENTITY_CONFLICT")
                return GatewayExecution(result=result, state=state)
            # A receipt can commit before its context snapshot. The command is
            # reconciled above; its old observation cannot authorize new actions.
            self.gateway.forget_observation(self.job)
            self.job.details_read.clear()
            self.loop.delivered_access.update(revision=None, details=[], entities={})
            self.loop.delivered_revision = None
            self.loop.review, self.loop.review_targets = None, set()
            self.delivery_step = max(self.delivery_step, model_step)
            self.pending_access, self.pending_review, self.pending_targets = None, None, set()
            result = {
                **result,
                "refreshRequired": True,
                "instruction": "Inspect the current player view before choosing further commands.",
            }
        else:
            self._begin_step(model_step)
            self.loop.current = {
                "id": identity,
                "job": self.job.id,
                "epoch": self.job.epoch,
                "purpose": self.job.purpose,
                "status": "validated",
                "decision": parsed,
                "deliveredAccess": copy.deepcopy(self.loop.delivered_access),
                "modelStep": model_step,
            }
            await self.loop.save()
            result = await self.loop.execute()
            self._stage_delivery(result)
        state = self.capture()
        assert self.loop.current is not None
        self.loop.current["gatewayState"] = state.model_dump(mode="json")
        self.loop.current["result"] = copy.deepcopy(result)
        await self.loop.save()
        return GatewayExecution(result=result, state=state)

    async def receipts_only(self, call_id: str, decision: JsonObject, *, model_step: int) -> GatewayExecution:
        parsed = self.contract.parse(self.contract.encode(copy.deepcopy(decision)))
        identity = operation_identity(self.job.id, self.job.epoch, model_step, call_id)
        saved = await self.gateway.io.call(journal, self.gateway.memory, identity)
        if saved is None:
            # The claimed call never prepared a child. No action or journal
            # row is invented to dispose of this admission.
            result = {"error": "SOURCE_EPOCH_RETIRED", "applied": False, "receipts": [], "unexecuted": []}
        else:
            if (saved["job"], saved["epoch"], saved["purpose"], saved["decision"]) != (
                self.job.id,
                self.job.epoch,
                self.job.purpose,
                parsed,
            ):
                raise JournalConflict("GATEWAY_OPERATION_CHANGED")
            self.loop.current = saved
            result = await self.loop.receipts_only()
        self.gateway.forget_observation(self.job)
        self.pending_access, self.pending_review, self.pending_targets = None, None, set()
        self.loop.delivered_access.update(revision=None, details=[], entities={})
        self.loop.review, self.loop.review_targets = None, set()
        state = self.capture()
        if saved is not None:
            assert self.loop.current is not None
            self.loop.current.update(gatewayState=state.model_dump(mode="json"))
            await self.loop.save()
        return GatewayExecution(result={**result, "refreshRequired": True}, state=state)
