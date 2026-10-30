"""Thin wrapper around the terraform CLI.

Every terraform call goes through `terraform()`, so there is one place that decides
the environment it runs in. That environment is deliberately sealed off from the
user's shell:

- all `AWS_*` variables are dropped and replaced with dummy credentials, and the AWS
  config/credentials files are pointed at /dev/null, so a real profile can never be
  picked up by accident;
- `TF_VAR_*` / `TF_CLI_ARGS*` / `TF_WORKSPACE` are dropped, so the plan only depends
  on the module itself;
- providers are downloaded once into a shared plugin cache. The cache is used even
  for modules without a lock file; that is safe because terraform only ever runs in
  throwaway workspace copies, so the lock file it writes never reaches the user's repo.
"""

import os
import subprocess
from pathlib import Path

PLUGIN_CACHE = Path.home() / ".terraform.d" / "plugin-cache"
DUMMY_AWS_KEY = "blastcheck-dummy"

_DROPPED_PREFIXES = ("AWS_", "TF_VAR_", "TF_CLI_ARGS", "TF_WORKSPACE", "ANTHROPIC_")


class TerraformError(Exception):
    """A terraform command exited non-zero."""


def terraform(args: list[str], cwd: Path, timeout: int = 600, check: bool = True) -> str:
    """Run `terraform <args>` in `cwd` and return stdout.

    Raise TerraformError on a non-zero exit, unless `check` is False (`terraform test`
    exits non-zero whenever a test fails, which for blastcheck is a normal result).
    """
    result = subprocess.run(
        ["terraform", *args],
        cwd=cwd,
        env=sealed_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise TerraformError(f"terraform {args[0]} failed in {cwd}:\n{output}")
    return result.stdout


def sealed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_DROPPED_PREFIXES)}
    PLUGIN_CACHE.mkdir(parents=True, exist_ok=True)
    env.update(
        TF_IN_AUTOMATION="1",
        TF_INPUT="0",
        TF_PLUGIN_CACHE_DIR=str(PLUGIN_CACHE),
        TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE="true",
        AWS_ACCESS_KEY_ID=DUMMY_AWS_KEY,
        AWS_SECRET_ACCESS_KEY=DUMMY_AWS_KEY,
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        AWS_EC2_METADATA_DISABLED="true",
    )
    return env
