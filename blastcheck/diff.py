"""Step 1: turn a git diff into the list of changed Terraform blocks.

Plain code, no LLM. The diff is applied to a copy of the module, so we end up with
two directories: `pre` (the module as it is) and `post` (the module with the PR
applied). Changed blocks are then found by parsing every `.tf` file in both copies
with python-hcl2 and comparing them block by block. That is more reliable than
mapping diff hunks to HCL blocks by line number, and it also catches a block that
moved between files without changing.

Limitations (v1): one module, top-level `.tf` files only, and the diff's paths
must be relative to the module directory (`a/main.tf`, not `a/modules/x/main.tf`).
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import hcl2

from blastcheck.models import ChangedBlock

if not hasattr(hcl2, "SerializationOptions"):
    raise ImportError(
        "blastcheck needs python-hcl2 >= 8, but the installed `hcl2` module is a different "
        "package (usually bc-python-hcl2, pulled in by `pip install checkov`). Install "
        "Checkov outside this environment (pipx or brew), then run: "
        "pip install --force-reinstall python-hcl2"
    )

# Terraform's own working files never belong in a workspace copy.
_IGNORED = shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.backup", "*.tfplan")

HCL_OPTIONS = hcl2.SerializationOptions(
    strip_string_quotes=True,
    explicit_blocks=False,
    with_comments=False,
)


class DiffError(Exception):
    """The diff could not be applied to the module."""


def make_workspace(module_dir: Path, diff_file: Path, work_dir: Path) -> tuple[Path, Path]:
    """Copy the module to `work_dir/pre` and `work_dir/post`, then apply the diff to post.

    The user's module is never modified; later steps run terraform in these copies.
    """
    pre = work_dir / "pre"
    post = work_dir / "post"
    shutil.copytree(module_dir, pre, ignore=_IGNORED)
    shutil.copytree(module_dir, post, ignore=_IGNORED)
    _apply_diff(diff_file.resolve(), post)
    return pre, post


def _apply_diff(diff_file: Path, target_dir: Path) -> None:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(diff_file)],
        cwd=target_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DiffError(f"git apply failed: {result.stderr.strip()}")


def changed_blocks(pre_dir: Path, post_dir: Path) -> list[ChangedBlock]:
    """Every top-level block that differs between the two module directories."""
    before = parse_module(pre_dir)
    after = parse_module(post_dir)
    changes = []
    for address in sorted(before.keys() | after.keys()):
        old, new = before.get(address), after.get(address)
        if old == new:
            continue
        kind = (old or new)[0]
        changes.append(
            ChangedBlock(
                address=address,
                kind=kind,
                action=_action(old, new),
                before=old[1] if old else None,
                after=new[1] if new else None,
            )
        )
    return changes


def _action(old: Any, new: Any) -> str:
    if old is None:
        return "create"
    if new is None:
        return "delete"
    return "update"


def parse_module(module_dir: Path) -> dict[str, tuple[str, Any]]:
    """Map every block address in the module to (kind, body)."""
    blocks: dict[str, tuple[str, Any]] = {}
    for tf_file in sorted(module_dir.glob("*.tf")):
        parsed = hcl2.loads(tf_file.read_text(), serialization_options=HCL_OPTIONS)
        for address, kind, body in _iter_blocks(parsed):
            blocks[address] = (kind, body)
    return blocks


def _iter_blocks(parsed: dict[str, list[dict]]):
    """Yield (address, kind, body) for each top-level block in one parsed file.

    Block types not listed here (moved, import, check, removed) are skipped:
    their effects show up in the plan, which step 2 reads.
    """
    for block_type, entries in parsed.items():
        for entry in entries:
            if block_type in ("resource", "data"):
                prefix = "data." if block_type == "data" else ""
                for res_type, named in entry.items():
                    for name, body in named.items():
                        yield f"{prefix}{res_type}.{name}", block_type, body
            elif block_type in ("variable", "output", "module"):
                prefix = "var" if block_type == "variable" else block_type
                for name, body in entry.items():
                    yield f"{prefix}.{name}", block_type, body
            elif block_type == "locals":
                for name, value in entry.items():
                    yield f"local.{name}", "local", {"value": value}
            elif block_type == "provider":
                for name, body in entry.items():
                    alias = body.get("alias")
                    address = f"provider.{name}.{alias}" if alias else f"provider.{name}"
                    yield address, "provider", body
            elif block_type == "terraform":
                yield "terraform", "terraform", entry
