from blastcheck.models import (
    CandidateTest,
    Intent,
    PolicyFinding,
    PolicyResult,
    RejectedTest,
    Risk,
    RunResult,
    UnexplainedChange,
    ValidationResult,
)
from blastcheck.report import build_report, verdict

SSH = "aws_vpc_security_group_ingress_rule.ssh_bastion"
SKIPPED = PolicyResult("skipped", [], "Checkov is not installed.")


def _result(name: str, outcome: str, before="pass", after="pass", message="") -> ValidationResult:
    test = CandidateTest(name, "N1", "must_not_change", "code", "SSH stays on the bastion", f'run "{name}" {{}}')
    return ValidationResult(test, RunResult(before, []), RunResult(after, [message] if message else []), outcome, "why")


def _intent(**overrides) -> Intent:
    fields = dict(summary="Adds HTTPS.", must_hold=[], must_not_change=[], risks=[], unexplained_changes=[], notes=[])
    return Intent(**(fields | overrides))


def _report(results, intent=None, rejected=(), policy=SKIPPED) -> str:
    return build_report("abc12345", intent or _intent(), list(rejected), results, policy, "tests/abc12345.tftest.hcl")


def test_verdict_names_regressions():
    assert "REGRESSION FOUND" in verdict([_result("a", "regression", after="fail")])
    assert "no regression found" in verdict([_result("a", "kept")])


def test_regression_section_shows_the_failure_message():
    report = _report([_result("n1", "regression", after="fail", message='cidr changed: expected "10.0.100.0/28"')])

    assert "## Regressions" in report
    assert 'cidr changed: expected "10.0.100.0/28"' in report


def test_no_regression_section_when_clean():
    assert "## Regressions" not in _report([_result("n1", "kept")])


def test_every_test_is_listed_with_its_outcome():
    report = _report([_result("kept_one", "kept"), _result("bad_one", "discarded", before="fail", after="fail")])

    assert "1 kept, 0 regression(s), 1 discarded after running, 0 rejected before running." in report
    assert "| `bad_one` |" in report
    assert "**discarded**" in report


def test_rejected_tests_show_reason_and_code():
    rejected = RejectedTest("taut", "H2", 'run "taut" {}', "tautology: `a == 443` ...")

    report = _report([], rejected=[rejected])

    assert "## Rejected before running" in report
    assert "tautology: `a == 443`" in report
    assert '```hcl\nrun "taut" {}\n```' in report


def test_unexplained_changes_table():
    change = UnexplainedChange(SSH, "cidr_ipv4", "10.0.100.0/28", "10.0.0.0/16", "not in the PR | description")

    report = _report([], intent=_intent(unexplained_changes=[change]))

    assert f"| `{SSH}` | `cidr_ipv4` | `10.0.100.0/28` | `10.0.0.0/16` |" in report
    assert "not in the PR \\| description" in report  # pipes can't break the table


def test_risks_are_sorted_by_severity():
    risks = [Risk("minor", SSH, "low"), Risk("major", SSH, "high")]

    report = _report([], intent=_intent(risks=risks))

    assert report.index("**high**") < report.index("**low**")


def test_notes_and_policy_findings_are_shown():
    policy = PolicyResult("ran", [PolicyFinding("CKV_AWS_24", "SSH from 0.0.0.0/0", SSH)], "1 new finding(s).")

    report = _report([], intent=_intent(notes=["Dropped 'x': aws_instance.ghost is not in the plan."]), policy=policy)

    assert "- Dropped 'x'" in report
    assert "| `CKV_AWS_24` |" in report


def test_skipped_policy_is_explained():
    assert "Skipped: Checkov is not installed." in _report([])
