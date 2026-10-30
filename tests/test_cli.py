import shutil
from pathlib import Path

import pytest

from blastcheck.cli import EXIT_ERROR, EXIT_REGRESSION, change_id, main

DEMO = Path(__file__).parent.parent / "examples" / "vpc-sg-bucket"


def _args(out: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--module", str(DEMO / "module"),
        "--diff", str(DEMO / "pr.diff"),
        "--intent", str(DEMO / "pr.md"),
        "--replay", str(DEMO / "recordings"),
        "--out", str(out),
        *extra,
    ]


@pytest.mark.terraform
def test_demo_end_to_end(tmp_path, capsys):
    if shutil.which("terraform") is None:
        pytest.skip("terraform not installed")

    exit_code = main(_args(tmp_path / "out", "--work", str(tmp_path / "work")))

    cid = change_id((DEMO / "pr.diff").read_text())
    report = (tmp_path / "out" / "blastcheck-report.md").read_text()
    tests = (tmp_path / "out" / "tests" / f"{cid}.tftest.hcl").read_text()
    assert exit_code == EXIT_REGRESSION
    assert "REGRESSION FOUND" in capsys.readouterr().out
    assert "n1_ssh_bastion_unchanged" in report and "tautology" in report
    assert 'run "n1_ssh_bastion_unchanged"' in tests
    assert "n4_app_unchanged" not in tests


def test_bad_diff_is_a_blastcheck_error(tmp_path, capsys):
    bad = tmp_path / "bad.diff"
    bad.write_text("--- a/main.tf\n+++ b/main.tf\n@@ -1,1 +1,1 @@\n-nothing like this\n+x\n")
    args = _args(tmp_path / "out", "--work", str(tmp_path / "work"))
    args[args.index("--diff") + 1] = str(bad)

    assert main(args) == EXIT_ERROR
    assert "git apply failed" in capsys.readouterr().err
