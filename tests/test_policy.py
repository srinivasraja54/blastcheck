import json

import pytest

from blastcheck import policy
from blastcheck.models import PolicyFinding
from blastcheck.policy import PolicyError, parse_checkov, run_checkov

SSH = "aws_vpc_security_group_ingress_rule.ssh_bastion"


def _checkov_output(*findings: tuple[str, str]) -> str:
    failed = [{"check_id": cid, "check_name": f"name {cid}", "resource": res} for cid, res in findings]
    return json.dumps({"results": {"failed_checks": failed}})


def test_parse_reads_failed_checks():
    assert parse_checkov(_checkov_output(("CKV_AWS_24", SSH))) == [PolicyFinding("CKV_AWS_24", "name CKV_AWS_24", SSH)]


def test_parse_handles_multiple_reports_and_empty_output():
    output = json.dumps([json.loads(_checkov_output(("A", "x"))), json.loads(_checkov_output(("B", "y")))])

    assert [f.check_id for f in parse_checkov(output)] == ["A", "B"]
    assert parse_checkov("") == []


def test_parse_rejects_garbage():
    with pytest.raises(PolicyError):
        parse_checkov("not json")


def test_missing_checkov_is_skipped_with_a_note(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "find_checkov", lambda: None)

    result = run_checkov(tmp_path, tmp_path)

    assert result.status == "skipped"
    assert "not installed" in result.note


def test_checkov_in_an_unactivated_venv_is_found(monkeypatch, tmp_path):
    (tmp_path / "checkov").write_text("")
    monkeypatch.setattr(policy.shutil, "which", lambda _: None)
    monkeypatch.setattr(policy.sys, "executable", str(tmp_path / "python"))

    assert policy.find_checkov() == str(tmp_path / "checkov")


def test_only_findings_new_after_the_change_are_reported(monkeypatch, tmp_path):
    pre_dir, post_dir = tmp_path / "pre", tmp_path / "post"
    old = PolicyFinding("OLD", "", "aws_s3_bucket.logs")
    new = PolicyFinding("NEW", "", SSH)
    monkeypatch.setattr(policy, "find_checkov", lambda: "/usr/bin/checkov")
    monkeypatch.setattr(policy, "failed_checks", lambda _, d: [old] if d == pre_dir else [old, new])

    result = run_checkov(pre_dir, post_dir)

    assert result.status == "ran"
    assert result.new_findings == [new]
    assert "1 finding(s) were already present" in result.note
