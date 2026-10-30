"""blastcheck command line.

    blastcheck run --module <dir> --diff <file> [--intent <file>] [--out <dir>]
                   [--model <id>] [--record <dir> | --replay <dir>] [--work <dir>]

Writes <out>/tests/<change-id>.tftest.hcl with the tests that survived validation.
Exit codes: 0 = no regression found, 1 = regression found, 2 = blastcheck error.
(Step 6, the markdown report, is not built yet; for now results are printed as JSON.)
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from blastcheck.author import author_tests
from blastcheck.diff import DiffError, changed_blocks, make_workspace
from blastcheck.intent import infer_intent
from blastcheck.llm import DEFAULT_MODEL, LLM, AnthropicLLM, LLMError, ReplayLLM
from blastcheck.plan import plan_module
from blastcheck.terraform import TerraformError
from blastcheck.validate import render_test_file, validate

EXIT_OK, EXIT_REGRESSION, EXIT_ERROR = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(Path(".env"))
    try:
        return run(args)
    except (DiffError, TerraformError, LLMError) as error:
        print(f"blastcheck: {error}", file=sys.stderr)
        return EXIT_ERROR


def run(args: argparse.Namespace) -> int:
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="blastcheck-"))
    _say(f"workspace: {work}")

    _say("1/5 diff: applying the PR to a copy of the module")
    diff_text = Path(args.diff).read_text()
    pre_dir, post_dir = make_workspace(Path(args.module), Path(args.diff), work)
    blocks = changed_blocks(pre_dir, post_dir)
    _say(f"    {len(blocks)} changed block(s): " + ", ".join(b.address for b in blocks))

    _say("2/5 plan: planning before and after (offline, dummy credentials)")
    pre, post = plan_module(pre_dir), plan_module(post_dir)

    llm = _llm(args)
    _say("3/5 intent: asking the LLM what to test")
    pr_text = Path(args.intent).read_text() if args.intent else ""
    intent = infer_intent(llm, pr_text, diff_text, pre, post)

    _say("4/5 author: writing tests")
    authored = author_tests(llm, intent, blocks, pre, post, diff_text)
    _say(f"    {len(authored.tests)} candidate test(s), {len(authored.rejected)} rejected before running")

    _say("5/5 validate: running every test before and after the change")
    results = validate(authored.tests, pre_dir, post_dir, pre, post)
    for result in results:
        _say(f"    {result.outcome:<10} {result.test.name}: {result.reason}")

    cid = change_id(diff_text)
    test_file = Path(args.out) / "tests" / f"{cid}.tftest.hcl"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(render_test_file(results, cid))
    _say(f"tests: {test_file}")

    print(json.dumps({"intent": asdict(intent), "rejected": [asdict(r) for r in authored.rejected],
                      "results": [asdict(r) for r in results]}, indent=2))
    regressions = [r for r in results if r.outcome == "regression"]
    return EXIT_REGRESSION if regressions else EXIT_OK


def change_id(diff_text: str) -> str:
    """Short, stable id for a change: the first 8 hex digits of the diff's sha256."""
    return hashlib.sha256(diff_text.encode()).hexdigest()[:8]


def _llm(args: argparse.Namespace) -> LLM:
    if args.replay:
        return ReplayLLM(Path(args.replay))
    return AnthropicLLM(model=args.model, record_dir=Path(args.record) if args.record else None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blastcheck", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="generate and validate tests for one PR")
    run_cmd.add_argument("--module", required=True, help="module directory, before the change")
    run_cmd.add_argument("--diff", required=True, help="the PR as a unified diff")
    run_cmd.add_argument("--intent", help="the PR description (markdown or text)")
    run_cmd.add_argument("--out", default=".", help="where to write tests/ (default: current directory)")
    run_cmd.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    run_cmd.add_argument("--work", help="workspace directory (default: a new temp dir)")
    llm_mode = run_cmd.add_mutually_exclusive_group()
    llm_mode.add_argument("--record", help="save LLM responses to this directory")
    llm_mode.add_argument("--replay", help="use saved LLM responses instead of the API")
    return parser


def _say(message: str) -> None:
    print(message, file=sys.stderr)


def load_dotenv(path: Path) -> None:
    """Read KEY=value lines from a .env file. Variables already set win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


if __name__ == "__main__":
    sys.exit(main())
