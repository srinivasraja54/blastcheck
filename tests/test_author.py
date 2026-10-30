import json
import shutil
from pathlib import Path

import hcl2
import pytest

from blastcheck.author import author_tests, guard_test, hcl_literal
from blastcheck.diff import HCL_OPTIONS, changed_blocks, make_workspace
from blastcheck.intent import infer_intent
from blastcheck.llm import ReplayLLM
from blastcheck.models import ChangedBlock, Expectation, Intent, Plan, PlannedResource
from blastcheck.plan import plan_module

DEMO = Path(__file__).parent.parent / "examples" / "vpc-sg-bucket"
HTTPS = "aws_vpc_security_group_ingress_rule.app_https"
HTTP = "aws_vpc_security_group_ingress_rule.app_http"


def _resource(address: str, **values) -> PlannedResource:
    return PlannedResource(address, address.split(".")[0], values, frozenset({"id"}))


PRE = Plan({HTTP: _resource(HTTP, from_port=8080, cidr_ipv4="10.0.10.0/24", tags={"Name": "x"})})
POST = Plan(PRE.resources | {HTTPS: _resource(HTTPS, from_port=443, to_port=443, cidr_ipv4="10.0.10.0/24")})
BLOCKS = [ChangedBlock(HTTPS, "resource", "create", None, {"from_port": 443, "to_port": 443})]


def _expectation(id: str, address: str, attributes: list[str]) -> Expectation:
    return Expectation(id, f"statement {id}", address, attributes, "because")


def _intent(must_hold=(), must_not_change=()) -> Intent:
    return Intent("s", list(must_hold), list(must_not_change), [], [], [])


def _run(name: str, condition: str, command: str = "plan") -> str:
    return (
        f'run "{name}" {{\n  command = {command}\n'
        f'  assert {{\n    condition     = {condition}\n    error_message = "m"\n  }}\n}}'
    )


class FakeLLM:
    def __init__(self, tests: list[dict]):
        self.tests = tests
        self.calls = 0

    def complete(self, step, system, prompt, schema=None):
        self.calls += 1
        assert step == "author"
        return json.dumps({"tests": self.tests})


def _author(tests: list[dict], must_hold=None):
    must_hold = must_hold or [_expectation("H1", HTTPS, ["cidr_ipv4"])]
    return author_tests(FakeLLM(tests), _intent(must_hold=must_hold), BLOCKS, PRE, POST, "diff")


# --- guards written by code ------------------------------------------------------------


def test_guard_pins_pre_change_values():
    test = guard_test(_expectation("N1", HTTP, ["from_port", "cidr_ipv4"]), PRE)

    assert test.name == "n1_app_http_unchanged"
    assert test.source == "code"
    assert f"{HTTP}.from_port == 8080" in test.hcl
    assert f'{HTTP}.cidr_ipv4 == "10.0.10.0/24"' in test.hcl


def test_guard_compares_collections_via_jsonencode():
    test = guard_test(_expectation("N1", HTTP, ["tags"]), PRE)

    assert f'jsonencode({HTTP}.tags) == jsonencode({{"Name": "x"}})' in test.hcl


def test_guard_is_valid_hcl():
    test = guard_test(_expectation("N1", HTTP, ["from_port", "tags"]), PRE)

    parsed = hcl2.loads(test.hcl, serialization_options=HCL_OPTIONS)
    assert list(parsed["run"][0]) == ["n1_app_http_unchanged"]


def test_hcl_literal_escapes_template_markers():
    assert hcl_literal("${var.x}") == '"$${var.x}"'


# --- checking LLM-written tests ------------------------------------------------------------


def test_good_llm_test_is_kept():
    result = _author([{"expectation_id": "H1", "name": "same_source", "hcl": _run("same_source", f"{HTTPS}.cidr_ipv4 == {HTTP}.cidr_ipv4")}])

    assert [t.name for t in result.tests] == ["same_source"]
    assert result.rejected == []


@pytest.mark.parametrize(
    "item, reason",
    [
        ({"expectation_id": "H9", "name": "a", "hcl": _run("a", f"{HTTPS}.cidr_ipv4 != null")}, "unknown expectation"),
        ({"expectation_id": "H1", "name": "Bad-Name", "hcl": _run("Bad-Name", "true")}, "invalid run name"),
        ({"expectation_id": "H1", "name": "a", "hcl": "run \"a\" {"}, "not valid HCL"),
        ({"expectation_id": "H1", "name": "a", "hcl": _run("b", f"{HTTPS}.cidr_ipv4 != null")}, "exactly one run block"),
        ({"expectation_id": "H1", "name": "a", "hcl": _run("a", f"{HTTPS}.cidr_ipv4 != null", "apply")}, "command = plan"),
        ({"expectation_id": "H1", "name": "a", "hcl": _run("a", "1 == 1")}, "doesn't reference any resource"),
        ({"expectation_id": "H1", "name": "a", "hcl": _run("a", f"{HTTPS}.from_port == 443")}, "tautology"),
    ],
)
def test_bad_llm_tests_are_rejected_with_a_reason(item, reason):
    result = _author([item])

    assert [t.source for t in result.tests] == []
    assert reason in result.rejected[0].reason


def test_duplicate_names_are_rejected():
    hcl = _run("same", f"{HTTPS}.cidr_ipv4 == {HTTP}.cidr_ipv4")
    result = _author([{"expectation_id": "H1", "name": "same", "hcl": hcl}] * 2)

    assert len(result.tests) == 1
    assert "duplicate" in result.rejected[0].reason


def test_no_must_hold_means_no_llm_call():
    llm = FakeLLM([])
    intent = _intent(must_not_change=[_expectation("N1", HTTP, ["from_port"])])

    result = author_tests(llm, intent, BLOCKS, PRE, POST, "diff")

    assert llm.calls == 0
    assert [t.name for t in result.tests] == ["n1_app_http_unchanged"]


# --- the demo, replayed ----------------------------------------------------------------------


@pytest.mark.terraform
def test_demo_author_keeps_the_ssh_guard_and_rejects_the_tautology(tmp_path):
    if shutil.which("terraform") is None:
        pytest.skip("terraform not installed")
    pre_dir, post_dir = make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path)
    blocks = changed_blocks(pre_dir, post_dir)
    pre, post = plan_module(pre_dir), plan_module(post_dir)
    diff_text = (DEMO / "pr.diff").read_text()
    llm = ReplayLLM(DEMO / "recordings")

    intent = infer_intent(llm, (DEMO / "pr.md").read_text(), diff_text, pre, post)
    result = author_tests(llm, intent, blocks, pre, post, diff_text)

    ssh_guard = next(t for t in result.tests if t.name == "n1_ssh_bastion_unchanged")
    assert 'ssh_bastion.cidr_ipv4 == "10.0.100.0/28"' in ssh_guard.hcl
    assert any(r.reason.startswith("tautology") for r in result.rejected)
