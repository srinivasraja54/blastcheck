"""Policy checks: call Checkov, never reimplement it. Plain code, no LLM.

Checkov runs on the pre and post copies of the module, and only findings that are new
after the change are reported, so the report stays about the change, not the module.
If Checkov isn't installed, the report says so and moves on.
"""

import json
import shutil
import subprocess
from pathlib import Path

from blastcheck.models import PolicyFinding, PolicyResult
from blastcheck.terraform import sealed_env


class PolicyError(Exception):
    """Checkov ran but its output couldn't be read."""


def find_checkov() -> str | None:
    """Checkov on PATH.

    Install it in its own environment (`pipx install --python python3.12 checkov`),
    never in blastcheck's: Checkov depends on bc-python-hcl2, which installs under the
    same `hcl2` import name and replaces the python-hcl2 that blastcheck needs.
    """
    return shutil.which("checkov")


def run_checkov(pre_dir: Path, post_dir: Path) -> PolicyResult:
    checkov = find_checkov()
    if checkov is None:
        return PolicyResult(
            "skipped", [], "Checkov is not on PATH (install it with `pipx install --python python3.12 checkov`)."
        )
    try:
        before = set(failed_checks(checkov, pre_dir))
        after = failed_checks(checkov, post_dir)
    except (PolicyError, subprocess.TimeoutExpired) as error:
        return PolicyResult("error", [], f"Checkov failed: {error}")
    new = [finding for finding in after if finding not in before]
    note = (
        f"{len(new)} new finding(s) after the change "
        f"({len(before)} finding(s) were already present before it and are not about this change)."
    )
    return PolicyResult("ran", new, note)


def failed_checks(checkov: str, module_dir: Path) -> list[PolicyFinding]:
    result = subprocess.run(
        [checkov, "--directory", str(module_dir), "--framework", "terraform",
         "--output", "json", "--quiet", "--compact", "--skip-download",
         "--skip-path", r"\.terraform"],
        env=sealed_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )  # checkov exits 1 when checks fail, which is a normal result here
    return parse_checkov(result.stdout)


def parse_checkov(output: str) -> list[PolicyFinding]:
    if not output.strip():
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError as error:
        raise PolicyError(f"unreadable output ({error})") from error
    reports = data if isinstance(data, list) else [data]
    findings = []
    for report in reports:
        for check in report.get("results", {}).get("failed_checks", []):
            findings.append(PolicyFinding(check["check_id"], check.get("check_name", ""), check.get("resource", "")))
    return findings
