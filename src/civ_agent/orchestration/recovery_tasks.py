"""Private logical task budgets; native stores and SDK histories remain authoritative.

Collectors supply verified original requests and settled native budget facts.
These pure helpers bind those facts to actual replacement tickets, never guessed
future handoffs. Inventories belong in the immutable player recovery manifest.
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from civ_agent.orchestration.contracts import (
    CommunicationBatch,
    Digest,
    OpaqueId,
    PeacePhase,
    Reference,
    ReviewRequest,
    RoleRequest,
    Scope,
    TurnTicket,
    digest,
)
from civ_agent.orchestration.correction_attempts import CorrectionAllowance, carry_correction_allowance
from civ_agent.orchestration.managed_checkpoints import NativeRunBinding
from civ_agent.orchestration.recovery_budgets import AllowancePolicy, GenerationBudgetTransfer
from civ_agent.orchestration.store import JournalConflict
from civ_agent.orchestration.task_identity import (
    CommunicationTask as CommunicationTask,
)
from civ_agent.orchestration.task_identity import (
    PeacePhaseTask as PeacePhaseTask,
)
from civ_agent.orchestration.task_identity import (
    ReviewTask as ReviewTask,
)
from civ_agent.orchestration.task_identity import (
    TaskIdentity as RecoveryTask,
)
from civ_agent.orchestration.task_identity import (
    TaskOwner as TaskOwner,
)
from civ_agent.orchestration.task_identity import (
    TurnRoleTask as TurnRoleTask,
)
from civ_agent.orchestration.task_identity import (
    task_identity_sha256 as task_identity_sha256,
)
from civ_agent.schemas import BoundaryModel, NonnegativeInt


class TurnTaskProof(BoundaryModel):
    kind: Literal["turn"] = "turn"
    ticket: TurnTicket


class CommunicationTaskProof(BoundaryModel):
    kind: Literal["communication"] = "communication"
    batch: CommunicationBatch
    startup: Reference
    catalogue: Reference


class PeaceTaskProof(BoundaryModel):
    kind: Literal["peace"] = "peace"
    consultation: Scope
    inputs: Reference


type TaskProof = Annotated[TurnTaskProof | CommunicationTaskProof | PeaceTaskProof, Field(discriminator="kind")]


class BudgetInheritance(BoundaryModel):
    inventory_sha256: Digest
    task_sha256: Digest
    generation: GenerationBudgetTransfer
    attempt_offset: NonnegativeInt

    @model_validator(mode="after")
    def refreshed(self):
        if self.generation.allowance_policy == "refresh" and self.attempt_offset:
            raise ValueError("RECOVERY_REFRESH_ATTEMPT_CONFLICT")
        return self


def _owner(owner: TaskOwner, scope: Scope):
    if (owner.game, owner.player) != (scope.game, scope.player):
        raise JournalConflict("RECOVERY_TASK_OWNER_CONFLICT")


def _role_scope(ticket: TurnTicket, task: TurnRoleTask) -> Scope:
    _owner(task.owner, ticket.scope)
    if ticket.scope.activation != digest([ticket.scope.game, ticket.scope.player, ticket.scope.epoch, ticket.handoff]):
        raise JournalConflict("RECOVERY_TICKET_IDENTITY_CONFLICT")
    if task.turn != ticket.turn:
        raise JournalConflict("RECOVERY_TASK_TURN_CONFLICT")
    return ticket.scope.model_copy(
        update={"activation": digest([ticket.scope.activation, f"{task.purpose}:{task.pass_number}"])}
    )


def _review_scope(ticket: TurnTicket, task: ReviewTask) -> Scope:
    _owner(task.owner, ticket.scope)
    if ticket.scope.activation != digest([ticket.scope.game, ticket.scope.player, ticket.scope.epoch, ticket.handoff]):
        raise JournalConflict("RECOVERY_TICKET_IDENTITY_CONFLICT")
    if task.source_turn != (0 if ticket.turn == 0 else ticket.turn - 1):
        raise JournalConflict("RECOVERY_REVIEW_TURN_CONFLICT")
    return ticket.scope.model_copy(
        update={"activation": digest([ticket.scope.model_dump(), "review", task.source_turn])}
    )


def _communication_task(owner: TaskOwner, proof: CommunicationTaskProof) -> CommunicationTask:
    _owner(owner, proof.batch.run.scope)
    if proof.batch.run.scope.activation != "communication":
        raise JournalConflict("RECOVERY_COMMUNICATION_SCOPE_CONFLICT")
    notices = proof.batch.notices
    if any(notice.scope != proof.batch.run.scope for notice in notices) or len({n.native_id for n in notices}) != len(
        notices
    ):
        raise JournalConflict("RECOVERY_COMMUNICATION_BATCH_CONFLICT")
    # Exact content references, without rewriting old event bodies or read IDs.
    return CommunicationTask(
        owner=owner,
        batch_sha256=digest([[n.native_id, n.content.sha256] for n in notices]),
        inputs_sha256=digest([proof.startup.sha256, proof.catalogue.sha256]),
    )


def _communication_scope(proof: CommunicationTaskProof) -> Scope:
    batch = proof.batch
    return batch.run.scope.model_copy(
        update={
            "activation": digest([batch.run.scope.model_dump(), [n.model_dump(mode="json") for n in batch.notices]])
        }
    )


class TaskBudget(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    task: RecoveryTask
    source: Scope
    request_sha256: Digest
    request_limit: Annotated[int, Field(ge=1, le=100)]
    attempt_limit: Annotated[int, Field(ge=1)] | None
    attempt_offset: NonnegativeInt = 0
    generation_ids: Annotated[list[Digest], Field(max_length=100)]
    proof: TaskProof
    run: NativeRunBinding | None = None
    correction: CorrectionAllowance | None = None
    inheritance: BudgetInheritance | None = None

    @property
    def used_attempts(self) -> int:
        return self.correction.used if self.correction is not None else self.attempt_offset

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def bound(self):
        task, proof, scope = self.task, self.proof, self.source
        _owner(task.owner, scope)
        if self.generation_ids != sorted(set(self.generation_ids)) or len(self.generation_ids) > self.request_limit:
            raise ValueError("RECOVERY_TASK_GENERATIONS_CONFLICT")
        if isinstance(task, TurnRoleTask) and isinstance(proof, TurnTaskProof):
            expected, kind, purpose = _role_scope(proof.ticket, task), "role", task.purpose
        elif isinstance(task, ReviewTask) and isinstance(proof, TurnTaskProof):
            expected, kind, purpose = _review_scope(proof.ticket, task), "review", "review"
        elif isinstance(task, CommunicationTask) and isinstance(proof, CommunicationTaskProof):
            if task != _communication_task(task.owner, proof):
                raise ValueError("RECOVERY_COMMUNICATION_INPUT_CONFLICT")
            expected, kind, purpose = _communication_scope(proof), "role", "communication"
        elif isinstance(task, PeacePhaseTask) and isinstance(proof, PeaceTaskProof):
            _owner(task.owner, proof.consultation)
            consultation = proof.consultation
            if (
                consultation.activation != digest([scope.game, scope.player, scope.epoch, "peace", task.basis])
                or task.inputs_sha256 != proof.inputs.sha256
            ):
                raise ValueError("RECOVERY_PEACE_INPUT_CONFLICT")
            expected = consultation.model_copy(update={"activation": digest([consultation.model_dump(), task.phase])})
            kind, purpose = "peace_phase", "consultation"
        else:
            raise ValueError("RECOVERY_TASK_PROOF_CONFLICT")
        if expected != scope or self.run is not None and (self.run.scope, self.run.kind) != (scope, kind):
            raise ValueError("RECOVERY_TASK_SCOPE_CONFLICT")
        if (kind == "peace_phase") != (self.attempt_limit is None) or self.attempt_offset > (self.attempt_limit or 0):
            raise ValueError("RECOVERY_TASK_ATTEMPT_CONFLICT")
        if self.correction is not None:
            c = self.correction
            if self.run is None or (
                c.scope,
                c.kind,
                c.purpose,
                c.workflow_run_id,
                c.attempt_limit,
                c.attempt_offset,
            ) != (scope, kind, purpose, self.run.run_id, self.attempt_limit, self.attempt_offset):
                raise ValueError("RECOVERY_TASK_CORRECTION_CONFLICT")
        elif self.run is not None and kind != "peace_phase":
            raise ValueError("RECOVERY_TASK_CORRECTION_REQUIRED")
        inherited = self.inheritance
        if inherited is not None:
            if (
                inherited.task_sha256 != task_identity_sha256(task)
                or inherited.generation.target != scope
                or inherited.attempt_offset != self.attempt_offset
                or not inherited.generation.inherited_ids <= set(self.generation_ids)
            ):
                raise ValueError("RECOVERY_TASK_INHERITANCE_CONFLICT")
        elif self.attempt_offset:
            raise ValueError("RECOVERY_TASK_INHERITANCE_REQUIRED")
        if self.run is None and (
            self.correction is not None
            or set(self.generation_ids) != (inherited.generation.inherited_ids if inherited else set())
        ):
            raise ValueError("RECOVERY_PREPARED_TASK_DISPATCHED")
        return self


class DormantTaskBudget(BoundaryModel):
    inventory_sha256: Digest
    budget_sha256: Digest
    budget: TaskBudget
    allowance_policy: AllowancePolicy = Field(default="carry", exclude_if=lambda value: value == "carry")

    @model_validator(mode="after")
    def unchanged(self):
        if self.budget_sha256 != self.budget.sha256:
            raise ValueError("RECOVERY_DORMANT_TASK_CHANGED")
        return self


class RecoveryTaskInventory(BoundaryModel):
    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    owner: TaskOwner
    epoch: OpaqueId
    current: list[TaskBudget]
    dormant: list[DormantTaskBudget] = Field(default_factory=list)

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def budgets(self) -> list[TaskBudget]:
        return [*self.current, *(item.budget for item in self.dormant)]

    @model_validator(mode="after")
    def scoped(self):
        keys = [task_identity_sha256(item.task) for item in self.budgets]
        if len(set(keys)) != len(keys):
            raise ValueError("RECOVERY_TASK_AMBIGUOUS")
        if any(item.task.owner != self.owner or item.source.epoch != self.epoch for item in self.current) or any(
            item.budget.task.owner != self.owner or item.budget.source.epoch == self.epoch for item in self.dormant
        ):
            raise ValueError("RECOVERY_INVENTORY_OWNER_CONFLICT")
        return self

    def find(self, task: RecoveryTask) -> TaskBudget | None:
        if task.owner != self.owner:
            raise JournalConflict("RECOVERY_TASK_OWNER_CONFLICT")
        key = task_identity_sha256(task)
        return next((item for item in self.budgets if task_identity_sha256(item.task) == key), None)

    def effective_policy(self, task: RecoveryTask, allowance_policy: AllowancePolicy) -> AllowancePolicy:
        """An unused explicit refresh survives until its dormant task is admitted."""
        if allowance_policy == "refresh":
            return "refresh"
        key = task_identity_sha256(task)
        return next(
            (item.allowance_policy for item in self.dormant if task_identity_sha256(item.budget.task) == key),
            "carry",
        )


def _inheritance(
    previous: RecoveryTaskInventory | None,
    task: RecoveryTask,
    target: Scope,
    allowance_policy: AllowancePolicy,
) -> BudgetInheritance | None:
    if previous is None or (source := previous.find(task)) is None:
        return None
    policy = previous.effective_policy(task, allowance_policy)
    return BudgetInheritance(
        inventory_sha256=previous.sha256,
        task_sha256=task_identity_sha256(task),
        generation=GenerationBudgetTransfer(
            source=source.source, target=target, generation_ids=source.generation_ids, allowance_policy=policy
        ),
        attempt_offset=0 if policy == "refresh" else source.used_attempts,
    )


def merge_task_budgets(
    owner: TaskOwner,
    epoch: str,
    current: list[TaskBudget],
    *,
    previous: RecoveryTaskInventory | None = None,
    allowance_policy: AllowancePolicy = "carry",
) -> RecoveryTaskInventory:
    """Keep dormant facts, checking every resumed task against its approved floor."""
    owner = TaskOwner.model_validate_json(owner.model_dump_json())
    current = [TaskBudget.model_validate_json(item.model_dump_json()) for item in current]
    if previous is not None:
        previous = RecoveryTaskInventory.model_validate_json(previous.model_dump_json())
    if previous is not None and (previous.owner != owner or previous.epoch == epoch):
        raise JournalConflict("RECOVERY_INVENTORY_PARENT_CONFLICT")
    for item in current:
        expected = _inheritance(previous, item.task, item.source, allowance_policy)
        if item.inheritance != expected:
            raise JournalConflict("RECOVERY_TASK_INHERITANCE_CONFLICT")
        prior = previous.find(item.task) if previous is not None else None
        if prior is not None and (item.request_limit, item.attempt_limit) != (prior.request_limit, prior.attempt_limit):
            raise JournalConflict("RECOVERY_TASK_LIMIT_CHANGED")
    keys = {task_identity_sha256(item.task) for item in current}
    dormant = (
        [
            DormantTaskBudget(
                inventory_sha256=previous.sha256,
                budget_sha256=item.sha256,
                budget=item,
                allowance_policy=previous.effective_policy(item.task, allowance_policy),
            )
            for item in previous.budgets
            if task_identity_sha256(item.task) not in keys
        ]
        if previous
        else []
    )
    return RecoveryTaskInventory(owner=owner, epoch=epoch, current=current, dormant=dormant)


def role_task_budget(
    owner: TaskOwner,
    request: RoleRequest,
    generation_ids: list[str],
    *,
    ticket: TurnTicket | None = None,
    pass_number: int | None = None,
    batch: CommunicationBatch | None = None,
    run: NativeRunBinding | None = None,
    correction: CorrectionAllowance | None = None,
    previous: RecoveryTaskInventory | None = None,
    allowance_policy: AllowancePolicy = "carry",
) -> TaskBudget:
    request = RoleRequest.model_validate_json(request.model_dump_json())
    if request.purpose == "communication" and batch is not None and ticket is None and pass_number is None:
        if (request.selection, request.request_limit) != (batch.run.selection, batch.run.request_limit):
            raise JournalConflict("RECOVERY_COMMUNICATION_SETTINGS_CONFLICT")
        proof = CommunicationTaskProof(batch=batch, startup=request.startup, catalogue=request.catalogue)
        task = _communication_task(owner, proof)
    elif (
        request.purpose in {"gameplay", "economy", "military"}
        and ticket is not None
        and pass_number is not None
        and batch is None
    ):
        proof = TurnTaskProof(ticket=ticket)
        task = TurnRoleTask(owner=owner, turn=ticket.turn, purpose=request.purpose, pass_number=pass_number)
    else:
        raise JournalConflict("RECOVERY_ROLE_TASK_CONFLICT")
    if request.job != request.scope.activation:
        raise JournalConflict("RECOVERY_ROLE_JOB_CONFLICT")
    return TaskBudget(
        task=task,
        source=request.scope,
        request_sha256=digest(request.model_dump(mode="json")),
        request_limit=request.request_limit,
        attempt_limit=request.attempt_limit,
        attempt_offset=request.attempt_offset,
        generation_ids=generation_ids,
        proof=proof,
        run=run,
        correction=correction,
        inheritance=_inheritance(previous, task, request.scope, allowance_policy),
    )


def review_task_budget(
    owner: TaskOwner,
    ticket: TurnTicket,
    request: ReviewRequest,
    generation_ids: list[str],
    *,
    run: NativeRunBinding | None = None,
    correction: CorrectionAllowance | None = None,
    previous: RecoveryTaskInventory | None = None,
    allowance_policy: AllowancePolicy = "carry",
) -> TaskBudget:
    request = ReviewRequest.model_validate_json(request.model_dump_json())
    task = ReviewTask(owner=owner, source_turn=0 if ticket.turn == 0 else ticket.turn - 1)
    return TaskBudget(
        task=task,
        source=request.scope,
        request_sha256=digest(request.model_dump(mode="json")),
        request_limit=request.request_limit,
        attempt_limit=request.attempt_limit,
        attempt_offset=request.attempt_offset,
        generation_ids=generation_ids,
        proof=TurnTaskProof(ticket=ticket),
        run=run,
        correction=correction,
        inheritance=_inheritance(previous, task, request.scope, allowance_policy),
    )


def peace_task_budget(
    owner: TaskOwner,
    phase: PeacePhase,
    generation_ids: list[str],
    *,
    run: NativeRunBinding | None = None,
    previous: RecoveryTaskInventory | None = None,
    allowance_policy: AllowancePolicy = "carry",
) -> TaskBudget:
    phase = PeacePhase.model_validate_json(phase.model_dump_json())
    task = PeacePhaseTask(
        owner=owner,
        basis=phase.request.basis,
        phase=phase.phase,
        inputs_sha256=phase.request.inputs.sha256,
        military_sha256=digest(phase.military) if phase.military is not None else None,
    )
    return TaskBudget(
        task=task,
        source=phase.scope,
        request_sha256=digest(phase.model_dump(mode="json")),
        request_limit=phase.request.request_limit,
        attempt_limit=None,
        generation_ids=generation_ids,
        proof=PeaceTaskProof(consultation=phase.request.scope, inputs=phase.request.inputs),
        run=run,
        inheritance=_inheritance(previous, task, phase.scope, allowance_policy),
    )


class BoundTaskBudget(BoundaryModel):
    task: RecoveryTask
    inventory_sha256: Digest
    source_budget_sha256: Digest
    generation: GenerationBudgetTransfer
    request: RoleRequest | ReviewRequest
    proof: TaskProof

    @model_validator(mode="after")
    def target(self):
        _owner(self.task.owner, self.request.scope)
        if self.generation.target != self.request.scope:
            raise ValueError("RECOVERY_TASK_TARGET_CONFLICT")
        if isinstance(self.request, RoleRequest):
            purpose = (
                self.task.purpose
                if isinstance(self.task, TurnRoleTask)
                else "communication"
                if isinstance(self.task, CommunicationTask)
                else None
            )
            if self.request.purpose != purpose or self.request.job != self.request.scope.activation:
                raise ValueError("RECOVERY_TASK_TARGET_CONFLICT")
        elif not isinstance(self.task, ReviewTask):
            raise ValueError("RECOVERY_TASK_TARGET_CONFLICT")
        TaskBudget(
            task=self.task,
            source=self.request.scope,
            request_sha256=digest(self.request.model_dump(mode="json")),
            request_limit=self.request.request_limit,
            attempt_limit=self.request.attempt_limit,
            generation_ids=[],
            proof=self.proof,
        )
        return self


def _bind(
    inventory: RecoveryTaskInventory,
    candidate: TaskBudget,
    request: RoleRequest | ReviewRequest,
    allowance_policy: AllowancePolicy,
) -> BoundTaskBudget:
    source = inventory.find(candidate.task)
    if source is None:
        raise JournalConflict("RECOVERY_TASK_NOT_FOUND")
    if (source.request_limit, source.attempt_limit) != (request.request_limit, request.attempt_limit):
        raise JournalConflict("RECOVERY_TASK_LIMIT_CHANGED")
    if request.scope.epoch in {inventory.epoch, source.source.epoch} or request.attempt_offset not in {
        0,
        source.used_attempts,
    }:
        raise JournalConflict("RECOVERY_TASK_TARGET_CONFLICT")
    policy = inventory.effective_policy(candidate.task, allowance_policy)
    if policy == "refresh" and request.attempt_offset:
        raise JournalConflict("RECOVERY_TASK_TARGET_CONFLICT")
    if policy == "carry" and source.used_attempts >= request.attempt_limit:
        raise JournalConflict("CORRECTION_ATTEMPTS_EXHAUSTED")
    if policy == "carry" and len(source.generation_ids) >= request.request_limit:
        raise JournalConflict("MODEL_REQUEST_LIMIT")
    if policy == "refresh":
        request = request.model_copy(update={"attempt_offset": 0})
    elif source.correction is not None:
        request = carry_correction_allowance(source.correction, request)
    else:
        model = RoleRequest if isinstance(request, RoleRequest) else ReviewRequest
        request = model.model_validate_json(
            request.model_copy(update={"attempt_offset": source.used_attempts}).model_dump_json()
        )
    return BoundTaskBudget(
        task=candidate.task,
        inventory_sha256=inventory.sha256,
        source_budget_sha256=source.sha256,
        generation=GenerationBudgetTransfer(
            source=source.source, target=request.scope, generation_ids=source.generation_ids, allowance_policy=policy
        ),
        request=request,
        proof=candidate.proof,
    )


def bind_role_budget(
    inventory: RecoveryTaskInventory,
    request: RoleRequest,
    *,
    ticket: TurnTicket | None = None,
    pass_number: int | None = None,
    batch: CommunicationBatch | None = None,
    allowance_policy: AllowancePolicy = "carry",
) -> BoundTaskBudget:
    """Return the transfer and corrected request; caller installs before admission."""
    inventory = RecoveryTaskInventory.model_validate_json(inventory.model_dump_json())
    request = RoleRequest.model_validate_json(request.model_dump_json())
    # Identity validation uses an unadmitted zero-offset projection only. No
    # source history or corrected target request is fabricated by this check.
    candidate = role_task_budget(
        inventory.owner,
        request.model_copy(update={"attempt_offset": 0}),
        [],
        ticket=ticket,
        pass_number=pass_number,
        batch=batch,
    )
    return _bind(inventory, candidate, request, allowance_policy)


def bind_review_budget(
    inventory: RecoveryTaskInventory,
    ticket: TurnTicket,
    request: ReviewRequest,
    *,
    allowance_policy: AllowancePolicy = "carry",
) -> BoundTaskBudget:
    inventory = RecoveryTaskInventory.model_validate_json(inventory.model_dump_json())
    request = ReviewRequest.model_validate_json(request.model_dump_json())
    candidate = review_task_budget(inventory.owner, ticket, request.model_copy(update={"attempt_offset": 0}), [])
    return _bind(inventory, candidate, request, allowance_policy)
