import shutil
from pathlib import Path

import pytest

from blastcheck.diff import make_workspace
from blastcheck.models import UNKNOWN, Plan, PlannedResource
from blastcheck.plan import parse_plan, plan_changes, plan_module, write_provider_override
from blastcheck.terraform import DUMMY_AWS_KEY, sealed_env

DEMO = Path(__file__).parent.parent / "examples" / "vpc-sg-bucket"


def _resource(address: str, unknown: tuple[str, ...] = (), **values) -> PlannedResource:
    return PlannedResource(address, address.split(".")[0], values, frozenset(unknown))


def _plan(*resources: PlannedResource) -> Plan:
    return Plan({r.address: r for r in resources})


# --- parse_plan -------------------------------------------------------------


def test_parse_plan_reads_known_and_unknown_values():
    plan_json = {
        "resource_changes": [
            {
                "address": "aws_vpc.main",
                "mode": "managed",
                "type": "aws_vpc",
                "change": {
                    "actions": ["create"],
                    "after": {"cidr_block": "10.0.0.0/16", "id": None},
                    "after_unknown": {"id": True, "tags_all": {}},
                },
            },
            {
                "address": "data.aws_ami.ubuntu",
                "mode": "data",
                "type": "aws_ami",
                "change": {"actions": ["read"], "after": {}, "after_unknown": {}},
            },
        ]
    }

    plan = parse_plan(plan_json)

    assert list(plan.resources) == ["aws_vpc.main"]
    vpc = plan.resources["aws_vpc.main"]
    assert vpc.values == {"cidr_block": "10.0.0.0/16"}
    assert vpc.value("id") == UNKNOWN


# --- plan_changes ------------------------------------------------------------


def test_identical_plans_have_no_changes():
    plan = _plan(_resource("aws_vpc.main", cidr_block="10.0.0.0/16"))
    assert plan_changes(plan, plan) == []


def test_update_lists_only_the_changed_attributes():
    pre = _plan(_resource("aws_vpc.main", cidr_block="10.0.0.0/16", enable_dns_hostnames=True))
    post = _plan(_resource("aws_vpc.main", cidr_block="10.0.0.0/8", enable_dns_hostnames=True))

    [change] = plan_changes(pre, post)
    assert change.action == "update"
    assert change.changes == {"cidr_block": ("10.0.0.0/16", "10.0.0.0/8")}


def test_value_becoming_unknown_is_a_change():
    pre = _plan(_resource("aws_s3_bucket.a", bucket="logs"))
    post = _plan(_resource("aws_s3_bucket.a", unknown=("bucket",)))

    [change] = plan_changes(pre, post)
    assert change.changes == {"bucket": ("logs", UNKNOWN)}


def test_create_and_delete():
    pre = _plan(_resource("aws_s3_bucket.old", bucket="old"))
    post = _plan(_resource("aws_s3_bucket.new", bucket="new"))

    changes = {c.address: c for c in plan_changes(pre, post)}
    assert changes["aws_s3_bucket.new"].action == "create"
    assert changes["aws_s3_bucket.new"].changes == {"bucket": (None, "new")}
    assert changes["aws_s3_bucket.old"].action == "delete"


# --- offline provider setup ------------------------------------------------------


def test_override_covers_default_and_aliased_providers(tmp_path):
    (tmp_path / "main.tf").write_text(
        'provider "aws" {\n  region = "eu-west-1"\n}\n'
        'provider "aws" {\n  alias = "us"\n  region = "us-east-1"\n}\n'
    )

    path = write_provider_override(tmp_path)

    assert path.name == "blastcheck_override.tf"
    text = path.read_text()
    assert text.count('provider "aws"') == 2
    assert 'alias = "us"' in text
    assert "skip_credentials_validation = true" in text
    assert "region" not in text  # the module's own region is kept


def test_module_without_provider_block_gets_one(tmp_path):
    (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "a" {\n  bucket = "a"\n}\n')

    path = write_provider_override(tmp_path)

    assert path.name == "blastcheck_provider.tf"
    assert 'region = "us-east-1"' in path.read_text()


def test_sealed_env_never_passes_real_credentials(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "production")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "real-token")
    monkeypatch.setenv("TF_VAR_region", "us-west-2")

    env = sealed_env()

    assert "AWS_PROFILE" not in env
    assert "AWS_SESSION_TOKEN" not in env
    assert "TF_VAR_region" not in env
    assert env["AWS_ACCESS_KEY_ID"] == DUMMY_AWS_KEY


# --- the demo, with real terraform --------------------------------------------------


@pytest.fixture(scope="module")
def demo_plans(tmp_path_factory):
    if shutil.which("terraform") is None:
        pytest.skip("terraform not installed")
    work = tmp_path_factory.mktemp("demo")
    pre, post = make_workspace(DEMO / "module", DEMO / "pr.diff", work)
    return plan_module(pre), plan_module(post)


@pytest.mark.terraform
def test_demo_plan_shows_only_the_two_rule_changes(demo_plans):
    changes = {c.address: c for c in plan_changes(*demo_plans)}

    assert set(changes) == {
        "aws_vpc_security_group_ingress_rule.app_https",
        "aws_vpc_security_group_ingress_rule.ssh_bastion",
    }
    assert changes["aws_vpc_security_group_ingress_rule.app_https"].action == "create"


@pytest.mark.terraform
def test_demo_plan_resolves_the_sneaky_ssh_widening(demo_plans):
    changes = {c.address: c for c in plan_changes(*demo_plans)}
    ssh = changes["aws_vpc_security_group_ingress_rule.ssh_bastion"]

    assert ssh.changes == {"cidr_ipv4": ("10.0.100.0/28", "10.0.0.0/16")}
