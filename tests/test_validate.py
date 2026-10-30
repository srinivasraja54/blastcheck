import json
import shutil
from pathlib import Path

import pytest

from blastcheck.author import author_tests
from blastcheck.diff import changed_blocks, make_workspace
from blastcheck.intent import infer_intent
from blastcheck.llm import ReplayLLM
from blastcheck.models import CandidateTest, RunResult
from blastcheck.plan import plan_module
from blastcheck.validate import classify, only_missing, parse_test_output, render_test_file, validate

DEMO = Path(__file__).parent.parent / "examples" / "vpc-sg-bucket"
NEW = "aws_vpc_security_group_ingress_rule.app_https"
UNDECLARED = (
    "Reference to undeclared resource: A managed resource "
    '"aws_vpc_security_group_ingress_rule" "app_https" has not been declared in the root module.'
)

PASS = RunResult("pass", [])
FAIL = RunResult("fail", ["Test assertion failed: x changed"])
NEW_REF = RunResult("error", [UNDECLARED])
OTHER_ERROR = RunResult("error", ["Unsupported attribute: no such attribute"])


def _test(kind: str) -> CandidateTest:
    return CandidateTest("t", "X1", kind, "code", "statement", 'run "t" {}')


def _outcome(kind: str, before: RunResult, after: RunResult, created=(NEW,), deleted=()) -> str:
    return classify(_test(kind), before, after, set(created), set(deleted))[0]


# --- the outcome table -----------------------------------------------------------------


@pytest.mark.parametrize(
    "before, after, outcome",
    [
        (PASS, PASS, "kept"),
        (PASS, FAIL, "regression"),
        (FAIL, PASS, "discarded"),  # a guard must hold on the unchanged module
        (FAIL, FAIL, "discarded"),
        (PASS, OTHER_ERROR, "discarded"),
        (NEW_REF, PASS, "discarded"),
    ],
)
def test_must_not_change_outcomes(before, after, outcome):
    assert _outcome("must_not_change", before, after) == outcome


def test_guard_on_a_deleted_resource_is_a_regression():
    assert _outcome("must_not_change", PASS, NEW_REF, created=(), deleted=(NEW,)) == "regression"


@pytest.mark.parametrize(
    "before, after, outcome",
    [
        (PASS, PASS, "kept"),
        (FAIL, PASS, "kept"),
        (NEW_REF, PASS, "kept"),
        (PASS, FAIL, "regression"),
        (FAIL, FAIL, "discarded"),
        (OTHER_ERROR, PASS, "discarded"),
        (NEW_REF, FAIL, "discarded"),
    ],
)
def test_must_hold_outcomes(before, after, outcome):
    assert _outcome("must_hold", before, after) == outcome


def test_undeclared_reference_only_counts_for_resources_the_change_creates():
    assert only_missing(NEW_REF, {NEW})
    assert not only_missing(NEW_REF, {"aws_s3_bucket.other"})
    assert not only_missing(RunResult("error", [UNDECLARED, "Unsupported attribute: x"]), {NEW})
    assert not only_missing(FAIL, {NEW})


# --- reading terraform test -json --------------------------------------------------------


def _event(**fields) -> str:
    return json.dumps(fields)


def test_parse_reads_status_and_error_messages():
    output = "\n".join(
        [
            _event(type="test_run", test_run={"run": "a", "progress": "complete", "status": "pass"}),
            _event(type="test_run", test_run={"run": "b", "progress": "starting"}),
            _event(type="test_run", test_run={"run": "b", "progress": "complete", "status": "fail"}),
            _event(type="diagnostic", diagnostic={"severity": "error", "summary": "Test assertion failed", "detail": "x changed"}, **{"@testrun": "b"}),
            _event(type="diagnostic", diagnostic={"severity": "warning", "summary": "ignored"}, **{"@testrun": "b"}),
            "not json",
        ]
    )

    results = parse_test_output(output, ["a", "b"])

    assert results["a"] == PASS
    assert results["b"] == RunResult("fail", ["Test assertion failed: x changed"])


def test_file_level_errors_are_attributed_by_file_name():
    output = _event(
        type="diagnostic",
        diagnostic={"severity": "error", "summary": "Invalid block", "detail": "bad"},
        **{"@testfile": "blastcheck-tests/c.tftest.hcl"},
    )

    assert parse_test_output(output, ["c"])["c"] == RunResult("error", ["Invalid block: bad"])


def test_a_test_that_never_reported_is_an_error():
    assert parse_test_output("", ["d"])["d"].status == "error"


# --- the demo, end to end with real terraform ------------------------------------------------


@pytest.fixture(scope="module")
def demo_results(tmp_path_factory):
    if shutil.which("terraform") is None:
        pytest.skip("terraform not installed")
    pre_dir, post_dir = make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path_factory.mktemp("demo"))
    blocks = changed_blocks(pre_dir, post_dir)
    pre, post = plan_module(pre_dir), plan_module(post_dir)
    diff_text = (DEMO / "pr.diff").read_text()
    llm = ReplayLLM(DEMO / "recordings")
    intent = infer_intent(llm, (DEMO / "pr.md").read_text(), diff_text, pre, post)
    authored = author_tests(llm, intent, blocks, pre, post, diff_text)
    return {r.test.name: r for r in validate(authored.tests, pre_dir, post_dir, pre, post)}


@pytest.mark.terraform
def test_demo_catches_the_sneaky_ssh_widening(demo_results):
    ssh = demo_results["n1_ssh_bastion_unchanged"]

    assert ssh.outcome == "regression"
    assert '"10.0.100.0/28"' in ssh.post.messages[0]


@pytest.mark.terraform
def test_demo_keeps_the_new_https_rule_test(demo_results):
    assert demo_results["app_https_source_is_alb_subnet"].outcome == "kept"


@pytest.mark.terraform
def test_demo_discards_a_guard_that_fails_before_the_change(demo_results):
    assert demo_results["n4_app_unchanged"].outcome == "discarded"


@pytest.mark.terraform
def test_demo_test_file_holds_only_validated_tests(demo_results):
    text = render_test_file(list(demo_results.values()), "abc")

    assert "n4_app_unchanged" not in text
    assert "# REGRESSION: fails on this change." in text
    assert 'run "app_https_source_is_alb_subnet"' in text
