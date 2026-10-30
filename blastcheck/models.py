"""Data passed between blastcheck steps.

Each step takes plain data in and returns plain data out, so every step can be
tested on its own. Types are added here as the steps that need them are built.
"""

from dataclasses import dataclass
from typing import Any, Literal

Action = Literal["create", "update", "delete"]


@dataclass(frozen=True)
class ChangedBlock:
    """One top-level Terraform block that the diff created, updated or deleted.

    `address` follows Terraform's own naming: `aws_vpc.main`, `data.aws_ami.x`,
    `var.region`, `local.tags`, `module.net`, `output.id`, `provider.aws`.
    `before` / `after` are the parsed block bodies (None when absent).
    """

    address: str
    kind: str
    action: Action
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    @property
    def changed_attrs(self) -> list[str]:
        """Top-level attributes (or nested blocks) whose value differs."""
        before = self.before or {}
        after = self.after or {}
        keys = sorted(before.keys() | after.keys())
        return [k for k in keys if before.get(k) != after.get(k)]


# Shown in place of a value terraform can only know after apply (ids, ARNs, ...).
UNKNOWN = "(known after apply)"


@dataclass(frozen=True)
class PlannedResource:
    """One managed resource as `terraform plan` sees it.

    `values` holds the attributes known at plan time; `unknown` names the ones
    that are only known after apply.
    """

    address: str
    type: str
    values: dict[str, Any]
    unknown: frozenset[str]

    def value(self, attr: str) -> Any:
        return UNKNOWN if attr in self.unknown else self.values.get(attr)


@dataclass(frozen=True)
class Plan:
    """All planned resources of one module copy, keyed by address."""

    resources: dict[str, PlannedResource]


@dataclass(frozen=True)
class PlanChange:
    """A resource whose planned values differ between the pre and post plans.

    `changes` maps attribute -> (before, after). Unlike ChangedBlock, these are
    resolved values (`10.0.0.0/16`), not expressions (`aws_vpc.main.cidr_block`),
    and they include resources changed indirectly via variables or locals.
    """

    address: str
    action: Action
    changes: dict[str, tuple[Any, Any]]


@dataclass(frozen=True)
class Expectation:
    """One thing the change must do (`must_hold`) or must leave alone (`must_not_change`).

    `id` is assigned by blastcheck (H1, H2, ... / N1, N2, ...), not by the LLM.
    """

    id: str
    statement: str
    address: str
    attributes: list[str]
    rationale: str


@dataclass(frozen=True)
class Risk:
    statement: str
    address: str
    severity: Literal["low", "medium", "high"]


@dataclass(frozen=True)
class UnexplainedChange:
    """A change in the plan that the PR description doesn't account for."""

    address: str
    attribute: str
    before: str
    after: str
    reason: str


@dataclass(frozen=True)
class Intent:
    """Step 3 output: what the change is for, and what is worth testing."""

    summary: str
    must_hold: list[Expectation]
    must_not_change: list[Expectation]
    risks: list[Risk]
    unexplained_changes: list[UnexplainedChange]
    # What blastcheck dropped or added after checking the LLM's answer, and why.
    # Shown in the report.
    notes: list[str]


@dataclass(frozen=True)
class CandidateTest:
    """One `run` block, not yet validated.

    `source` says who wrote it: "code" (must_not_change guards, from pre-plan values)
    or "llm" (must_hold tests).
    """

    name: str
    expectation_id: str
    kind: Literal["must_hold", "must_not_change"]
    source: Literal["code", "llm"]
    statement: str
    hcl: str


@dataclass(frozen=True)
class RejectedTest:
    """A test the LLM wrote that blastcheck threw out before running it."""

    name: str
    expectation_id: str
    hcl: str
    reason: str


@dataclass(frozen=True)
class AuthorResult:
    """Step 4 output."""

    tests: list[CandidateTest]
    rejected: list[RejectedTest]


@dataclass(frozen=True)
class RunResult:
    """How one test did on one side (pre or post), from `terraform test -json`."""

    status: Literal["pass", "fail", "error", "skip"]
    messages: list[str]  # error diagnostics, "summary: detail"


Outcome = Literal["kept", "regression", "discarded"]


@dataclass(frozen=True)
class ValidationResult:
    """Step 5 output for one test."""

    test: CandidateTest
    pre: RunResult
    post: RunResult
    outcome: Outcome
    reason: str


@dataclass(frozen=True)
class PolicyFinding:
    check_id: str
    check_name: str
    resource: str


@dataclass(frozen=True)
class PolicyResult:
    """Checkov findings that are new after the change (or why Checkov didn't run)."""

    status: Literal["ran", "skipped", "error"]
    new_findings: list[PolicyFinding]
    note: str
