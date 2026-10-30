"""Step 5: run every candidate test before and after the change, and decide its fate.

Plain code, no LLM. Each test is written to its own file (in terraform test, an error
in one `run` block skips every later block in the same file) and the whole set is run
with `terraform test -json` once on the pre copy and once on the post copy.

| Kind            | Before          | After | Outcome                          |
|-----------------|-----------------|-------|----------------------------------|
| must_not_change | pass            | pass  | kept                             |
| must_not_change | pass            | fail  | **regression**                   |
| must_hold       | pass            | pass  | kept                             |
| must_hold       | fail            | pass  | kept (confirms the change)       |
| must_hold       | new-resource ref| pass  | kept (confirms the change)       |
| must_hold       | pass            | fail  | **regression**                   |
| anything else   |                 |       | discarded: a bad test            |

"New-resource ref" means the only errors are Terraform's "Reference to undeclared
resource" for resources the change creates. A must_not_change guard on a resource the
change deletes fails the same way after the change, and counts as a regression.
"""

import json
import re
import shutil
from pathlib import Path

from blastcheck.models import CandidateTest, Outcome, Plan, RunResult, ValidationResult
from blastcheck.terraform import terraform

TEST_DIR = "blastcheck-tests"

_UNDECLARED = re.compile(r'A managed resource "([^"]+)" "([^"]+)" has not been declared')


def validate(
    tests: list[CandidateTest],
    pre_dir: Path,
    post_dir: Path,
    pre: Plan,
    post: Plan,
) -> list[ValidationResult]:
    pre_runs = run_tests(tests, pre_dir)
    post_runs = run_tests(tests, post_dir)
    created = set(post.resources) - set(pre.resources)
    deleted = set(pre.resources) - set(post.resources)
    results = []
    for test in tests:
        before, after = pre_runs[test.name], post_runs[test.name]
        outcome, reason = classify(test, before, after, created, deleted)
        results.append(ValidationResult(test, before, after, outcome, reason))
    return results


def run_tests(tests: list[CandidateTest], module_dir: Path) -> dict[str, RunResult]:
    """Run each test in its own file in `module_dir`; return results by run name."""
    test_dir = module_dir / TEST_DIR
    shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir()
    for test in tests:
        (test_dir / f"{test.name}.tftest.hcl").write_text(f'mock_provider "aws" {{}}\n\n{test.hcl}\n')

    output = terraform(
        ["test", "-json", "-no-color", f"-test-directory={TEST_DIR}"], module_dir, check=False
    )
    return parse_test_output(output, [t.name for t in tests])


def parse_test_output(output: str, names: list[str]) -> dict[str, RunResult]:
    """Read `terraform test -json` output. A test that never reported is an error."""
    statuses: dict[str, str] = {}
    messages: dict[str, list[str]] = {name: [] for name in names}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "test_run" and event["test_run"].get("progress") == "complete":
            statuses[event["test_run"]["run"]] = event["test_run"]["status"]
        elif event.get("type") == "diagnostic" and event["diagnostic"].get("severity") == "error":
            name = event.get("@testrun") or Path(event.get("@testfile", "")).name.removesuffix(".tftest.hcl")
            if name in messages:
                diagnostic = event["diagnostic"]
                messages[name].append(f"{diagnostic['summary']}: {diagnostic.get('detail', '')}".strip())

    results = {}
    for name in names:
        status = statuses.get(name, "error")
        found = messages[name] or ([] if name in statuses else ["terraform did not run this test"])
        results[name] = RunResult(status, found)
    return results


def classify(
    test: CandidateTest,
    before: RunResult,
    after: RunResult,
    created: set[str],
    deleted: set[str],
) -> tuple[Outcome, str]:
    if test.kind == "must_not_change":
        return _classify_guard(before, after, deleted)
    return _classify_must_hold(before, after, created)


def _classify_guard(before: RunResult, after: RunResult, deleted: set[str]) -> tuple[Outcome, str]:
    if before.status != "pass":
        return "discarded", f"bad test: fails before the change ({_first(before)})"
    if after.status == "pass":
        return "kept", "holds before and after the change"
    if after.status == "fail":
        return "regression", f"passes before the change, fails after it: {_first(after)}"
    if only_missing(after, deleted):
        return "regression", f"the change removes a resource this guard covers: {_first(after)}"
    return "discarded", f"bad test: errors after the change ({_first(after)})"


def _classify_must_hold(before: RunResult, after: RunResult, created: set[str]) -> tuple[Outcome, str]:
    if after.status == "pass":
        if before.status == "pass":
            return "kept", "holds before and after the change"
        if before.status == "fail":
            return "kept", "fails before the change, passes after it: confirms the change"
        if only_missing(before, created):
            return "kept", "refers to resources the change creates; passes after the change"
        return "discarded", f"bad test: errors before the change ({_first(before)})"
    if before.status == "pass" and after.status == "fail":
        return "regression", f"held before the change, fails after it: {_first(after)}"
    return "discarded", f"bad test: fails after the change ({_first(after)})"


def only_missing(result: RunResult, addresses: set[str]) -> bool:
    """True when every error is "Reference to undeclared resource" for one of `addresses`."""
    if result.status != "error" or not result.messages:
        return False
    for message in result.messages:
        match = _UNDECLARED.search(message)
        if not message.startswith("Reference to undeclared resource") or not match:
            return False
        if f"{match.group(1)}.{match.group(2)}" not in addresses:
            return False
    return True


def _first(result: RunResult) -> str:
    return result.messages[0] if result.messages else result.status


# --- the output test file ------------------------------------------------------------------


def render_test_file(results: list[ValidationResult], change_id: str) -> str:
    """The tests that survived validation: kept ones, and regressions (marked)."""
    parts = [
        f"# Generated by blastcheck for change {change_id}.\n"
        "# Every test below passed validation. Review before committing.\n\n"
        'mock_provider "aws" {}'
    ]
    for result in results:
        if result.outcome == "discarded":
            continue
        test = result.test
        statement = " ".join(test.statement.split())
        header = f"# {test.expectation_id} ({test.kind}, written by {test.source}): {statement}"
        if result.outcome == "regression":
            header += f"\n# REGRESSION: fails on this change. {result.reason}"
        parts.append(f"{header}\n{test.hcl}")
    return "\n\n".join(parts) + "\n"
