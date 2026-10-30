"""Step 2: plan the pre and post copies of the module and compare the planned values.

Plain code, no LLM. For each workspace copy we:

1. write a provider override with dummy credentials and the `skip_*` flags, so
   `terraform plan` works offline and can never touch a real AWS account;
2. run `terraform init`, `plan` and `show -json`;
3. read the planned values of every managed resource.

Plans are made without any state, so every resource is planned as a create. That
is fine: we never compare a plan with reality, only the pre plan with the post plan.

Limitations (v1): AWS provider only, no backend, every variable needs a default,
and data sources that call AWS will fail (the credentials are fake on purpose).
"""

import json
from pathlib import Path
from typing import Any

from blastcheck.diff import parse_module
from blastcheck.models import Plan, PlanChange, PlannedResource
from blastcheck.terraform import DUMMY_AWS_KEY, terraform

PLAN_FILE = "blastcheck.tfplan"
PLAN_JSON = "blastcheck-plan.json"

_OFFLINE_SETTINGS = f"""\
  access_key                  = "{DUMMY_AWS_KEY}"
  secret_key                  = "{DUMMY_AWS_KEY}"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
"""


def plan_module(module_dir: Path) -> Plan:
    """Plan one workspace copy of the module. Writes files into `module_dir`."""
    write_provider_override(module_dir)
    terraform(["init", "-backend=false", "-input=false", "-no-color"], module_dir)
    terraform(
        ["plan", "-input=false", "-lock=false", "-refresh=false", "-no-color", f"-out={PLAN_FILE}"],
        module_dir,
    )
    raw = terraform(["show", "-json", PLAN_FILE], module_dir)
    (module_dir / PLAN_JSON).write_text(raw)
    return parse_plan(json.loads(raw))


def write_provider_override(module_dir: Path) -> Path:
    """Make every `provider "aws"` block use dummy credentials and skip AWS lookups.

    Terraform merges `*_override.tf` files into the matching provider blocks, so the
    module's own settings (region, default_tags, ...) are kept. A module without a
    provider block gets a plain provider block instead (an override needs a base).
    """
    aliases = [
        address.removeprefix("provider.aws").lstrip(".")
        for address in parse_module(module_dir)
        if address == "provider.aws" or address.startswith("provider.aws.")
    ]
    if not aliases:
        path = module_dir / "blastcheck_provider.tf"
        path.write_text(_provider_block(alias="", region="us-east-1"))
        return path

    path = module_dir / "blastcheck_override.tf"
    path.write_text("\n".join(_provider_block(alias) for alias in aliases))
    return path


def _provider_block(alias: str, region: str = "") -> str:
    lines = ['provider "aws" {']
    if alias:
        lines.append(f'  alias = "{alias}"')
    if region:
        lines.append(f'  region = "{region}"')
    return "\n".join(lines) + "\n" + _OFFLINE_SETTINGS + "}\n"


def parse_plan(plan_json: dict[str, Any]) -> Plan:
    """Read planned values of every managed resource from `terraform show -json`."""
    resources = {}
    for rc in plan_json.get("resource_changes", []):
        if rc["mode"] != "managed" or rc["change"]["actions"] == ["delete"]:
            continue
        after = rc["change"]["after"] or {}
        after_unknown = rc["change"].get("after_unknown") or {}
        unknown = frozenset(k for k, v in after_unknown.items() if v is True)
        resources[rc["address"]] = PlannedResource(
            address=rc["address"],
            type=rc["type"],
            values={k: v for k, v in after.items() if k not in unknown},
            unknown=unknown,
        )
    return Plan(resources=resources)


def plan_changes(pre: Plan, post: Plan) -> list[PlanChange]:
    """Resources whose planned values differ between the two plans."""
    changes = []
    for address in sorted(pre.resources.keys() | post.resources.keys()):
        before, after = pre.resources.get(address), post.resources.get(address)
        if before is None:
            changes.append(PlanChange(address, "create", _attr_changes(None, after)))
        elif after is None:
            changes.append(PlanChange(address, "delete", _attr_changes(before, None)))
        elif attrs := _attr_changes(before, after):
            changes.append(PlanChange(address, "update", attrs))
    return changes


def _attr_changes(
    before: PlannedResource | None, after: PlannedResource | None
) -> dict[str, tuple[Any, Any]]:
    attrs: set[str] = set()
    for resource in (before, after):
        if resource is not None:
            attrs |= resource.values.keys() | resource.unknown

    changes = {}
    for attr in sorted(attrs):
        old = before.value(attr) if before else None
        new = after.value(attr) if after else None
        if old != new:
            changes[attr] = (old, new)
    return changes
