"""Step 4: write the tests. LLM call #2 of 2 (only for must-hold tests).

Two kinds of test come out of this step:

- **must_not_change guards** are written by plain code. Each one pins the chosen
  attributes to their value in the pre-change plan. The LLM decided *what* to guard
  (step 3); the expected values come straight from terraform, so they can't be wrong
  or made up.
- **must_hold tests** need judgment, so the LLM writes them. Each one is checked before
  it's kept: it must be valid HCL, a single plan-only `run` block, tied to a real
  expectation, and not a tautology (see tautology.py). Rejected tests are kept with
  the reason, for the report.

Nothing here runs terraform; step 5 (validate) does.
"""

import json
import re
from importlib import resources
from typing import Any

import hcl2

from blastcheck.diff import HCL_OPTIONS
from blastcheck.intent import format_plan
from blastcheck.llm import LLM
from blastcheck.models import (
    AuthorResult,
    CandidateTest,
    ChangedBlock,
    Expectation,
    Intent,
    Plan,
    RejectedTest,
)
from blastcheck.tautology import Literals, diff_literals, references, tautological_part

PROMPT_VERSION = "author_v1"

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

SCHEMA = {
    "type": "object",
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "expectation_id": {"type": "string"},
                    "name": {"type": "string"},
                    "hcl": {"type": "string"},
                },
                "required": ["expectation_id", "name", "hcl"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tests"],
    "additionalProperties": False,
}


def author_tests(
    llm: LLM,
    intent: Intent,
    blocks: list[ChangedBlock],
    pre: Plan,
    post: Plan,
    diff_text: str,
) -> AuthorResult:
    guards = [guard_test(e, pre) for e in intent.must_not_change]
    if not intent.must_hold:
        return AuthorResult(guards, [])

    system = resources.files("blastcheck.prompts").joinpath(f"{PROMPT_VERSION}.md").read_text()
    prompt = build_prompt(intent.must_hold, diff_text, post)
    answer = json.loads(llm.complete("author", system, prompt, SCHEMA))

    checker = _Checker(intent, blocks, pre, post, taken={t.name for t in guards})
    tests, rejected = [], []
    for item in answer["tests"]:
        reason = checker.problem(item)
        if reason:
            rejected.append(RejectedTest(item["name"], item["expectation_id"], item["hcl"].strip(), reason))
            continue
        expectation = checker.expectations[item["expectation_id"]]
        tests.append(
            CandidateTest(
                name=item["name"],
                expectation_id=expectation.id,
                kind="must_hold",
                source="llm",
                statement=expectation.statement,
                hcl=item["hcl"].strip(),
            )
        )
    return AuthorResult(guards + tests, rejected)


# --- must_not_change guards (plain code) -------------------------------------------------


def guard_test(expectation: Expectation, pre: Plan) -> CandidateTest:
    """A run block asserting each attribute keeps its pre-change planned value."""
    resource = pre.resources[expectation.address]
    short_name = re.sub(r"[^a-z0-9_]", "_", expectation.address.split(".")[-1].lower())
    name = f"{expectation.id.lower()}_{short_name}_unchanged"

    asserts = []
    for attr in expectation.attributes:
        value = resource.values[attr]
        ref = f"{expectation.address}.{attr}"
        if isinstance(value, dict | list):
            condition = f"jsonencode({ref}) == jsonencode({hcl_literal(value)})"
        else:
            condition = f"{ref} == {hcl_literal(value)}"
        message = hcl_literal(f"{ref} changed: expected {json.dumps(value)}")
        asserts.append(
            "  assert {\n"
            f"    condition     = {condition}\n"
            f"    error_message = {message}\n"
            "  }"
        )

    hcl = f'run "{name}" {{\n  command = plan\n\n' + "\n\n".join(asserts) + "\n}"
    return CandidateTest(name, expectation.id, "must_not_change", "code", expectation.statement, hcl)


def hcl_literal(value: Any) -> str:
    """A JSON value written as an HCL expression (JSON syntax is valid HCL,
    except that `${` and `%{` start templates inside strings)."""
    return json.dumps(value).replace("${", "$${").replace("%{", "%%{")


# --- must_hold tests (LLM) --------------------------------------------------------------


def build_prompt(must_hold: list[Expectation], diff_text: str, post: Plan) -> str:
    expectations = []
    for e in must_hold:
        expectations.append(
            f"- {e.id}: {e.statement}\n"
            f"    resource: {e.address}; attributes: {', '.join(e.attributes) or '(any)'}\n"
            f"    why: {e.rationale}"
        )
    sections = [
        "## Expectations to test\n\n" + "\n".join(expectations),
        "## Diff\n\n```diff\n" + diff_text.strip() + "\n```",
        "## All resources as planned after the change\n\n" + format_plan(post),
    ]
    return "\n\n".join(sections) + "\n"


class _Checker:
    """Decides whether one LLM-written test is usable, and if not, why."""

    def __init__(self, intent: Intent, blocks: list[ChangedBlock], pre: Plan, post: Plan, taken: set[str]):
        self.expectations = {e.id: e for e in intent.must_hold}
        self.literals: Literals = diff_literals(blocks)
        self.addresses = set(pre.resources) | set(post.resources)
        self.taken = taken

    def problem(self, item: dict) -> str | None:
        name = item["name"]
        if item["expectation_id"] not in self.expectations:
            return f"refers to unknown expectation {item['expectation_id']!r}"
        if not _NAME.match(name):
            return f"invalid run name {name!r}"
        if name in self.taken:
            return f"duplicate run name {name!r}"

        try:
            parsed = hcl2.loads(item["hcl"], serialization_options=HCL_OPTIONS)
        except Exception as error:  # python-hcl2 raises several lark error types
            return f"not valid HCL: {str(error).splitlines()[0]}"
        runs = parsed.get("run", [])
        if set(parsed) != {"run"} or len(runs) != 1 or list(runs[0]) != [name]:
            return f'must be exactly one run block named "{name}"'

        body = runs[0][name]
        if body.get("command") != "plan":
            return "must use command = plan (v1 runs plan-only tests)"
        asserts = body.get("assert", [])
        if not asserts:
            return "has no assert"
        for block in asserts:
            reason = self._assert_problem(_unwrap(block.get("condition", "")))
            if reason:
                return reason

        self.taken.add(name)
        return None

    def _assert_problem(self, condition: str) -> str | None:
        refs = references(condition, self.addresses)
        if not refs:
            return f"assert `{condition}` doesn't reference any resource in the plan"
        part = tautological_part(condition, self.literals, self.addresses)
        if part is not None:
            set_by_diff = ", ".join(
                f"{addr.split('.')[-1]}.{attr} = {json.dumps(self.literals[(addr, attr)])}"
                for addr, attr in sorted(references(part, self.addresses))
            )
            return f"tautology: `{part}` only checks values the diff sets literally ({set_by_diff})"
        return None


def _unwrap(condition: Any) -> str:
    """python-hcl2 returns expressions as "${...}"; show them as written."""
    text = str(condition)
    return text[2:-1] if text.startswith("${") and text.endswith("}") else text

