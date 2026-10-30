from pathlib import Path

import pytest

from blastcheck.diff import DiffError, changed_blocks, make_workspace, parse_module
from blastcheck.models import ChangedBlock

DEMO = Path(__file__).parent.parent / "examples" / "vpc-sg-bucket"


def _write(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def _by_address(changes: list[ChangedBlock]) -> dict[str, ChangedBlock]:
    return {c.address: c for c in changes}


# --- the demo PR -------------------------------------------------------------


def test_demo_pr_changes_exactly_two_rules(tmp_path):
    pre, post = make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path)
    changes = _by_address(changed_blocks(pre, post))

    assert set(changes) == {
        "aws_vpc_security_group_ingress_rule.app_https",
        "aws_vpc_security_group_ingress_rule.ssh_bastion",
    }


def test_demo_pr_new_https_rule_is_a_create(tmp_path):
    pre, post = make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path)
    https = _by_address(changed_blocks(pre, post))["aws_vpc_security_group_ingress_rule.app_https"]

    assert https.action == "create"
    assert https.before is None
    assert https.after["from_port"] == 443


def test_demo_pr_sneaky_ssh_change_is_visible(tmp_path):
    pre, post = make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path)
    ssh = _by_address(changed_blocks(pre, post))["aws_vpc_security_group_ingress_rule.ssh_bastion"]

    assert ssh.action == "update"
    assert ssh.changed_attrs == ["cidr_ipv4"]
    assert ssh.before["cidr_ipv4"] == "${aws_subnet.bastion.cidr_block}"
    assert ssh.after["cidr_ipv4"] == "${aws_vpc.main.cidr_block}"


def test_workspace_leaves_the_users_module_untouched(tmp_path):
    original = (DEMO / "module" / "main.tf").read_text()
    make_workspace(DEMO / "module", DEMO / "pr.diff", tmp_path)

    assert (DEMO / "module" / "main.tf").read_text() == original


# --- general behaviour ------------------------------------------------------


def test_deleted_resource_is_reported(tmp_path):
    pre = _write(tmp_path / "pre", {"main.tf": 'resource "aws_s3_bucket" "a" {\n  bucket = "a"\n}\n'})
    post = _write(tmp_path / "post", {"main.tf": ""})

    [change] = changed_blocks(pre, post)
    assert change.address == "aws_s3_bucket.a"
    assert change.action == "delete"
    assert change.after is None


def test_block_moved_between_files_is_not_a_change(tmp_path):
    block = 'resource "aws_s3_bucket" "a" {\n  bucket = "a"\n}\n'
    pre = _write(tmp_path / "pre", {"main.tf": block})
    post = _write(tmp_path / "post", {"main.tf": "", "buckets.tf": block})

    assert changed_blocks(pre, post) == []


def test_formatting_and_comments_are_not_changes(tmp_path):
    pre = _write(tmp_path / "pre", {"main.tf": 'resource "aws_s3_bucket" "a" {\n  bucket = "a"\n}\n'})
    post = _write(tmp_path / "post", {"main.tf": '# logs\nresource "aws_s3_bucket" "a" {\n    bucket   =   "a"\n}\n'})

    assert changed_blocks(pre, post) == []


def test_nested_block_change_is_reported_by_block_name(tmp_path):
    def versioning(status: str) -> str:
        return (
            'resource "aws_s3_bucket_versioning" "v" {\n  bucket = "a"\n'
            f'  versioning_configuration {{\n    status = "{status}"\n  }}\n}}\n'
        )

    pre = _write(tmp_path / "pre", {"main.tf": versioning("Enabled")})
    post = _write(tmp_path / "post", {"main.tf": versioning("Suspended")})

    [change] = changed_blocks(pre, post)
    assert change.changed_attrs == ["versioning_configuration"]


def test_non_resource_blocks_get_terraform_style_addresses(tmp_path):
    module = _write(
        tmp_path / "m",
        {
            "main.tf": (
                'variable "region" {\n  default = "eu-west-1"\n}\n'
                'locals {\n  env = "dev"\n}\n'
                'data "aws_ami" "ubuntu" {\n  most_recent = true\n}\n'
                'output "id" {\n  value = 1\n}\n'
                'provider "aws" {\n  alias = "us"\n  region = "us-east-1"\n}\n'
            )
        },
    )

    assert set(parse_module(module)) == {
        "var.region",
        "local.env",
        "data.aws_ami.ubuntu",
        "output.id",
        "provider.aws.us",
    }


def test_diff_that_does_not_apply_raises(tmp_path):
    module = _write(tmp_path / "m", {"main.tf": 'resource "aws_s3_bucket" "a" {\n  bucket = "a"\n}\n'})
    bad_diff = tmp_path / "bad.diff"
    bad_diff.write_text("--- a/main.tf\n+++ b/main.tf\n@@ -1,1 +1,1 @@\n-nothing like this\n+x\n")

    with pytest.raises(DiffError, match="git apply failed"):
        make_workspace(module, bad_diff, tmp_path / "work")
