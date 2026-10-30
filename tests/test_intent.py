import json
import shutil
from pathlib import Path

import pytest

from blastcheck.diff import make_workspace
from blastcheck.intent import build_prompt, check_intent, infer_intent
from blastcheck.llm import ReplayLLM
from blastcheck.models import Plan, PlannedResource
from blastcheck.plan import plan_changes, plan_module

DEMO = Path(__file__).parent.parent / "examples" / "vpc-sg-bucket"
SSH = "aws_vpc_security_group_ingress_rule.ssh_bastion"


def _resource(address: str, **values) -> PlannedResource:
    return PlannedResource(address, address.split(".")[0], values, frozenset({"id"}))


# A tiny before/after pair: the SSH rule is widened, nothing else changes.
PRE = Plan(
    {
        SSH: _resource(SSH, cidr_ipv4="10.0.100.0/28", from_port=22),
        "aws_vpc.main": _resource("aws_vpc.main", cidr_block="10.0.0.0/16"),
    }
)
POST = Plan(
    {
        SSH: _resource(SSH, cidr_ipv4="10.0.0.0/16", from_port=22),
        "aws_vpc.main": _resource("aws_vpc.main", cidr_block="10.0.0.0/16"),
    }
)
CHANGES = plan_changes(PRE, POST)


def _answer(**overrides) -> dict:
    answer = {
        "summary": "s",
        "must_hold": [],
        "must_not_change": [
            {"statement": "VPC stays", "address": "aws_vpc.main", "attributes": ["cidr_block"], "rationale": "r"}
        ],
        "risks": [],
        "unexplained_changes": [],
    }
    return answer | overrides


def _change(address=SSH, attribute="cidr_ipv4"):
    return {"address": address, "attribute": attribute, "before": "a", "after": "b", "reason": "r"}


# --- checking the LLM's answer --------------------------------------------------------


def test_items_on_made_up_resources_are_dropped():
    answer = _answer(
        must_hold=[{"statement": "x", "address": "aws_instance.ghost", "attributes": [], "rationale": "r"}]
    )

    intent = check_intent(answer, PRE, POST, CHANGES)

    assert intent.must_hold == []
    assert any("aws_instance.ghost is not in the plan" in n for n in intent.notes)


def test_must_not_change_needs_a_known_attribute():
    answer = _answer(
        must_not_change=[{"statement": "x", "address": "aws_vpc.main", "attributes": ["id", "nope"], "rationale": "r"}]
    )

    intent = check_intent(answer, PRE, POST, CHANGES)

    assert intent.must_not_change == []
    assert any("no attribute to compare" in n for n in intent.notes)


def test_unexplained_change_must_be_real():
    answer = _answer(unexplained_changes=[_change(address="aws_vpc.main", attribute="cidr_block")])

    intent = check_intent(answer, PRE, POST, CHANGES)

    assert intent.unexplained_changes == []
    assert any("the plan doesn't change it" in n for n in intent.notes)


def test_uncovered_unexplained_change_gets_a_must_not_change_item():
    intent = check_intent(_answer(unexplained_changes=[_change()]), PRE, POST, CHANGES)

    added = [e for e in intent.must_not_change if e.address == SSH]
    assert len(added) == 1
    assert added[0].attributes == ["cidr_ipv4"]
    assert added[0].id == "N2"


def test_missing_blast_radius_is_reported():
    answer = _answer(
        must_not_change=[{"statement": "ssh", "address": SSH, "attributes": ["cidr_ipv4"], "rationale": "r"}]
    )

    intent = check_intent(answer, PRE, POST, CHANGES)

    assert any("No blast-radius item" in n for n in intent.notes)


def test_ids_are_assigned_by_code():
    answer = _answer(
        must_hold=[
            {"statement": "a", "address": SSH, "attributes": ["from_port"], "rationale": "r"},
            {"statement": "b", "address": SSH, "attributes": ["id"], "rationale": "r"},
        ]
    )

    intent = check_intent(answer, PRE, POST, CHANGES)

    assert [e.id for e in intent.must_hold] == ["H1", "H2"]


def test_prompt_shows_resolved_values_and_is_deterministic():
    prompt = build_prompt("Add HTTPS", "--- a/main.tf", PRE, CHANGES)

    assert f"- {SSH} (update)" in prompt
    assert "cidr_ipv4: 10.0.100.0/28 -> 10.0.0.0/16" in prompt
    assert prompt == build_prompt("Add HTTPS", "--- a/main.tf", PRE, CHANGES)


def test_infer_intent_parses_the_llm_answer():
    class FakeLLM:
        def complete(self, step, system, prompt, schema=None):
            assert step == "intent" and "You review Terraform pull requests" in system
            return json.dumps(_answer(unexplained_changes=[_change()]))

    intent = infer_intent(FakeLLM(), "PR", "diff", PRE, POST)

    assert intent.unexplained_changes[0].address == SSH


# --- the demo, replayed from the recorded LLM answer ------------------------------


@pytest.mark.terraform
def test_demo_intent_flags_the_sneaky_ssh_widening(tmp_path):
    if shutil.which("terraform") is None:
        pytest.skip("terraform not installed")
    pre_dir, post_dir = make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path)
    pre, post = plan_module(pre_dir), plan_module(post_dir)

    intent = infer_intent(
        ReplayLLM(DEMO / "recordings"),
        (DEMO / "pr.md").read_text(),
        (DEMO / "pr.diff").read_text(),
        pre,
        post,
    )

    assert [(c.address, c.attribute) for c in intent.unexplained_changes] == [(SSH, "cidr_ipv4")]
    assert any(SSH == e.address and "cidr_ipv4" in e.attributes for e in intent.must_not_change)
    assert intent.notes == []
