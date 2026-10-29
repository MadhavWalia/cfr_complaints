"""C05 output: validated, revisioned task DAG (ADR-01)."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from ._base import SHA256_PATTERN, Contract


class TaskKind(StrEnum):
    RETRIEVAL = "RETRIEVAL"
    COVERAGE = "COVERAGE"
    ASSESSMENT = "ASSESSMENT"
    CRITIQUE = "CRITIQUE"
    RECONCILIATION = "RECONCILIATION"
    AGGREGATION = "AGGREGATION"
    PUBLICATION_CHECK = "PUBLICATION_CHECK"


MANDATORY_TASK_KINDS: frozenset[TaskKind] = frozenset(
    {TaskKind.COVERAGE, TaskKind.CRITIQUE, TaskKind.RECONCILIATION, TaskKind.PUBLICATION_CHECK}
)


class TaskNode(Contract):
    node_id: str = Field(min_length=1)
    kind: TaskKind
    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    claim_id: str | None = None
    vertical_id: str | None = None
    injected_by_controller: bool = False


class TaskEdge(Contract):
    upstream: str = Field(min_length=1)
    downstream: str = Field(min_length=1)

    @model_validator(mode="after")
    def _no_self_loop(self) -> "TaskEdge":
        if self.upstream == self.downstream:
            raise ValueError("self-loop edge")
        return self


class PlannerSource(StrEnum):
    TEMPLATE = "TEMPLATE"
    DSPY = "DSPY"


class ExecutionPlan(Contract):
    """Structural validity only; authorization and budget checks belong to the controller."""

    run_id: str
    revision: int = Field(ge=0)
    claim_set_hash: str = Field(pattern=SHA256_PATTERN)
    planner: PlannerSource
    nodes: tuple[TaskNode, ...] = Field(min_length=1)
    edges: tuple[TaskEdge, ...] = ()

    @model_validator(mode="after")
    def _well_formed_dag(self) -> "ExecutionPlan":
        ids = [n.node_id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("node ids must be unique")
        known = set(ids)
        for e in self.edges:
            if e.upstream not in known or e.downstream not in known:
                raise ValueError(f"edge references unknown node: {e.upstream}->{e.downstream}")
        if self.topological_order() is None:
            raise ValueError("plan contains a cycle")
        return self

    def topological_order(self) -> list[str] | None:
        """Deterministic Kahn ordering (ties broken by node id); ``None`` if cyclic."""
        indegree = {n.node_id: 0 for n in self.nodes}
        children: dict[str, list[str]] = {n.node_id: [] for n in self.nodes}
        for e in self.edges:
            indegree[e.downstream] += 1
            children[e.upstream].append(e.downstream)
        ready = sorted(n for n, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        return order if len(order) == len(self.nodes) else None


class PlanRevisionTrigger(StrEnum):
    INITIAL = "INITIAL"
    CLAIM_SET_REVISED = "CLAIM_SET_REVISED"
    MORE_EVIDENCE_REQUESTED = "MORE_EVIDENCE_REQUESTED"
    RECONCILIATION_REASSESSMENT = "RECONCILIATION_REASSESSMENT"


class PlanRevision(Contract):
    run_id: str
    revision: int = Field(ge=0)
    previous_revision: int | None = Field(default=None, ge=0)
    trigger: PlanRevisionTrigger
    added_node_ids: tuple[str, ...] = ()
    cancelled_node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _lineage(self) -> "PlanRevision":
        if (self.trigger is PlanRevisionTrigger.INITIAL) != (self.previous_revision is None):
            raise ValueError("only the INITIAL revision has no predecessor")
        if self.previous_revision is not None and self.previous_revision >= self.revision:
            raise ValueError("revisions must increase")
        return self


class ValidationIssue(Contract):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    node_id: str | None = None


class PlanValidationReport(Contract):
    run_id: str
    revision: int = Field(ge=0)
    valid: bool
    issues: tuple[ValidationIssue, ...] = ()
    injected_node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_has_no_issues(self) -> "PlanValidationReport":
        if self.valid and self.issues:
            raise ValueError("a valid report carries no issues")
        if not self.valid and not self.issues:
            raise ValueError("an invalid report must explain why")
        return self
