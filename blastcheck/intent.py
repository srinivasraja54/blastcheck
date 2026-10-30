"""Step 3: decide what this change must do and must not break. LLM call #1 of 2.

The LLM gets the PR description, the diff and the planned values from step 2, and
answers with structured JSON (enforced by a JSON schema). Its answer is then
checked by plain code before anything uses it:

- every address and attribute must exist in the plan; anything else is dropped
  (the LLM can't make up resources);
- every "unexplained change" must be a real change in the plan;
- every unexplained change gets a must_not_change item, added by code if the LLM
  forgot it;
- a missing blast-radius item (a must_not_change on a resource outside the diff)
  is reported.

Everything dropped or added is recorded in `Intent.notes` for the report.
"""

import json
from importlib import resources
from typing import Any

from blastcheck.llm import LLM
from blastcheck.models import (
    UNKNOWN,
    Expectation,
    Intent,
    Plan,
    PlanChange,
    Risk,
    UnexplainedChange,
)
from blastcheck.plan import plan_changes

PROMPT_VERSION = "intent_v1"


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_EXPECTATION = _object(
    {
        "statement": _STRING,
        "address": _STRING,
        "attributes": {"type": "array", "items": _STRING},
        "rationale": _STRING,
    }
)

SCHEMA = _object(
    {
        "summary": _STRING,
        "must_hold": {"type": "array", "items": _EXPECTATION},
        "must_not_change": {"type": "array", "items": _EXPECTATION},
        "risks": {
            "type": "array",
            "items": _object(
                {
                    "statement": _STRING,
                    "address": _STRING,
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                }
            ),
        },
        "unexplained_changes": {
            "type": "array",
            "items": _object(
                {
                    "address": _STRING,
                    "attribute": _STRING,
                    "before": _STRING,
                    "after": _STRING,
                    "reason": _STRING,
                }
            ),
        },
    }
)


def infer_intent(llm: LLM, pr_text: str, diff_text: str, pre: Plan, post: Plan) -> Intent:
    changes = plan_changes(pre, post)
    system = resources.files("blastcheck.prompts").joinpath(f"{PROMPT_VERSION}.md").read_text()
    prompt = build_prompt(pr_text, diff_text, pre, changes)
    answer = json.loads(llm.complete("intent", system, prompt, SCHEMA))
    return check_intent(answer, pre, post, changes)


# --- prompt -------------------------------------------------------------------


def build_prompt(pr_text: str, diff_text: str, pre: Plan, changes: list[PlanChange]) -> str:
    """The user message. Deterministic for the same inputs, so recordings replay."""
    sections = [
        "## PR description\n\n" + (pr_text.strip() or "(none given)"),
        "## Diff\n\n```diff\n" + diff_text.strip() + "\n```",
        "## Planned changes (before -> after)\n\n" + _format_changes(changes),
        "## All resources as planned before the change\n\n" + format_plan(pre),
    ]
    return "\n\n".join(sections) + "\n"


def _format_changes(changes: list[PlanChange]) -> str:
    if not changes:
        return "(the plan does not change)"
    lines = []
    for change in changes:
        lines.append(f"- {change.address} ({change.action})")
        for attr, (before, after) in change.changes.items():
            lines.append(f"    {attr}: {_show(before)} -> {_show(after)}")
    return "\n".join(lines)


def format_plan(plan: Plan) -> str:
    lines = []
    for address, resource in sorted(plan.resources.items()):
        values = {k: v for k, v in resource.values.items() if v not in (None, [], {})}
        values |= {k: UNKNOWN for k in sorted(resource.unknown)}
        lines.append(f"- {address}: {json.dumps(values, sort_keys=True)}")
    return "\n".join(lines)


def _show(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


# --- checking the answer ----------------------------------------------------------


def check_intent(answer: dict, pre: Plan, post: Plan, changes: list[PlanChange]) -> Intent:
    notes: list[str] = []
    changed = {(c.address, attr) for c in changes for attr in c.changes}

    must_hold = _check_expectations(answer["must_hold"], "H", [post, pre], notes)
    must_not_change = _check_expectations(answer["must_not_change"], "N", [pre], notes)
    unexplained = _check_unexplained(answer["unexplained_changes"], changed, notes)
    must_not_change += _cover_unexplained(unexplained, must_not_change, pre, notes)

    risks = []
    for item in answer["risks"]:
        if _find(item["address"], [pre, post]) is None:
            notes.append(f"Dropped risk on unknown resource {item['address']}.")
            continue
        risks.append(Risk(item["statement"], item["address"], item["severity"]))

    changed_addresses = {c.address for c in changes}
    if not any(e.address not in changed_addresses for e in must_not_change):
        notes.append("No blast-radius item: nothing asserts that untouched resources stay the same.")

    return Intent(answer["summary"], must_hold, must_not_change, risks, unexplained, notes)


def _check_expectations(items: list[dict], prefix: str, plans: list[Plan], notes: list[str]):
    """Keep items whose address exists; keep only attributes that exist on it.

    A must_not_change item (checked against the pre plan only) needs attributes
    whose value is known before the change, otherwise there's nothing to compare.
    """
    kept = []
    for item in items:
        resource = _find(item["address"], plans)
        if resource is None:
            notes.append(f"Dropped '{item['statement']}': {item['address']} is not in the plan.")
            continue
        known = resource.values.keys() if prefix == "N" else resource.values.keys() | resource.unknown
        attributes = [a for a in item["attributes"] if a in known]
        for attr in sorted(set(item["attributes"]) - set(attributes)):
            notes.append(f"Ignored attribute {attr} on {item['address']}: not a known planned value.")
        if prefix == "N" and not attributes:
            notes.append(f"Dropped '{item['statement']}': no attribute to compare.")
            continue
        kept.append(
            Expectation(
                id=f"{prefix}{len(kept) + 1}",
                statement=item["statement"],
                address=item["address"],
                attributes=attributes,
                rationale=item["rationale"],
            )
        )
    return kept


def _check_unexplained(items: list[dict], changed: set[tuple[str, str]], notes: list[str]):
    kept = []
    for item in items:
        if (item["address"], item["attribute"]) not in changed:
            notes.append(
                f"Dropped unexplained change {item['address']}.{item['attribute']}: "
                "the plan doesn't change it."
            )
            continue
        kept.append(UnexplainedChange(**item))
    return kept


def _cover_unexplained(
    unexplained: list[UnexplainedChange],
    must_not_change: list[Expectation],
    pre: Plan,
    notes: list[str],
) -> list[Expectation]:
    """Add a must_not_change item for every unexplained change the LLM left uncovered."""
    covered = {(e.address, a) for e in must_not_change for a in e.attributes}
    added = []
    for change in unexplained:
        key = (change.address, change.attribute)
        resource = pre.resources.get(change.address)
        if key in covered or resource is None or change.attribute not in resource.values:
            continue
        added.append(
            Expectation(
                id=f"N{len(must_not_change) + len(added) + 1}",
                statement=f"{change.address}.{change.attribute} keeps its value from before the change",
                address=change.address,
                attributes=[change.attribute],
                rationale=f"Unexplained change: {change.reason}",
            )
        )
        notes.append(f"Added a must_not_change item for unexplained change {key[0]}.{key[1]}.")
    return added


def _find(address: str, plans: list[Plan]):
    for plan in plans:
        if address in plan.resources:
            return plan.resources[address]
    return None
