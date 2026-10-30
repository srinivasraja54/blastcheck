# blastcheck

**Change-scoped test generation for Terraform.**

Today a human eyeballs `terraform plan` and hopes. blastcheck reads a PR's diff, works
out what the change is *meant* to do, and generates `terraform test` tests for what the
change must do and what it must not break. The tests are then run deterministically
with `terraform test`, so a sneaky regression gets caught before merge.

It is not a policy scanner (it calls Checkov for that), it doesn't test your whole
module, it never touches a real cloud account, and it doesn't merge anything.

## 30-second demo

The demo PR says it adds an HTTPS rule. It also changes one line in the SSH rule:

```diff
-  cidr_ipv4         = aws_subnet.bastion.cidr_block
+  cidr_ipv4         = aws_vpc.main.cidr_block
```

That reads like a refactor. It actually opens SSH to the whole VPC (`10.0.0.0/16`)
instead of the bastion subnet (`10.0.100.0/28`), and no policy scanner flags it because
it's still private address space. The PR description calls it a "small tidy-up".

```sh
python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/blastcheck run --module examples/vpc-sg-bucket/module \
                         --diff   examples/vpc-sg-bucket/pr.diff \
                         --intent examples/vpc-sg-bucket/pr.md \
                         --replay examples/vpc-sg-bucket/recordings \
                         --out    out/
```

```
5/6 validate: running every test before and after the change
    regression n1_ssh_bastion_unchanged
    kept       n2_ssh_bastion_unchanged
    kept       n3_app_http_unchanged
    discarded  n4_app_unchanged
    kept       n5_bastion_unchanged
    kept       app_https_source_is_alb_subnet
6/6 report
**Verdict: REGRESSION FOUND.** 1 test(s) pass before this change and fail after it.
```

`--replay` uses the recorded LLM answers that ship with the demo, so this needs no API
key and gives the same result every time. It does need Terraform 1.7+; the first run
downloads the AWS provider. The output is in
[`examples/vpc-sg-bucket/expected/`](examples/vpc-sg-bucket/expected): the
[report](examples/vpc-sg-bucket/expected/blastcheck-report.md) and the
[generated tests](examples/vpc-sg-bucket/expected/tests/2d20d867.tftest.hcl).

The report shows all four ways a test can end:

- **regression**: the SSH guard passes before the change and fails after it;
- **kept**: blast-radius guards on untouched resources, and a test for the new rule;
- **discarded**: a guard that fails even on the unchanged module (a bad test);
- **rejected before running**: an LLM-written tautology (`from_port == to_port`, both
  set to `443` by the diff).

## How it works: exact vs judgment

| Part | Who does it | Why |
|---|---|---|
| Apply the diff, find changed blocks | plain code (`diff.py`) | must be exact |
| Plan before and after, compare values | plain code (`plan.py`) | must be exact |
| Decide what the change must do and must not break | **LLM** (`intent.py`) | needs judgment |
| Check the LLM's answer against the plan | plain code (`intent.py`) | must be exact |
| Write must_not_change guards, values from the plan | plain code (`author.py`) | must be exact |
| Write must_hold tests | **LLM** (`author.py`) | needs judgment |
| Reject tautologies and malformed tests | plain code (`tautology.py`) | must be exact |
| Run tests before/after, decide each outcome | plain code (`validate.py`) | must be reproducible |
| Policy checks | Checkov (`policy.py`) | already solved |
| Report and verdict | plain code (`report.py`) | must be reproducible |

The rule: if something can be done without the LLM, it is. There are exactly two LLM
calls per run, and no LLM decides whether a test passes, fails, or counts.

### Step 1: diff

blastcheck never edits your module. It copies it twice, to `pre/` and `post/`, and
applies the diff to `post/` with `git apply`. It then parses every `.tf` file in both
copies with [python-hcl2](https://github.com/amplify-education/python-hcl2) and compares
them block by block. The result is a list of changed blocks with Terraform-style
addresses (`aws_vpc_security_group_ingress_rule.ssh_bastion`), an action
(create / update / delete) and the attributes that changed.

Comparing parsed blocks instead of diff lines means reformatting, comments and moving
a block to another file don't count as changes.

### Step 2: plan

Step 1 sees expressions (`cidr_ipv4 = aws_vpc.main.cidr_block`). Step 2 sees values.
blastcheck runs `terraform plan` and `terraform show -json` on both copies and compares
the planned values of every resource:

```
update aws_vpc_security_group_ingress_rule.ssh_bastion
    cidr_ipv4: 10.0.100.0/28 -> 10.0.0.0/16
```

This also catches resources changed *indirectly*, for example through a variable
default or a local.

**Nothing touches AWS.** blastcheck writes a provider override with dummy credentials
and the `skip_*` flags into each copy, and runs terraform with a sealed environment:
your `AWS_*` variables, profiles and `~/.aws` files are never visible to it. Plans are
made without state, so every resource is planned as a create. That's fine, because
blastcheck only compares the before plan with the after plan, never a plan with reality.

The AWS provider is downloaded once into `~/.terraform.d/plugin-cache` (~700 MB) and
reused after that.

### Step 3: intent (LLM call 1 of 2)

The LLM reads the PR description, the diff and the planned changes, and answers in
JSON that a schema enforces:

- **must_hold**: what the change must achieve;
- **must_not_change**: resources and attributes that must be the same afterwards,
  including at least one resource *outside* the diff (blast radius);
- **unexplained_changes**: planned changes the PR description doesn't account for;
- **risks**: anything else a reviewer should know.

The answer isn't trusted as-is. Plain code checks it against the plan: made-up
resources and attributes are dropped, "unexplained changes" must be real plan changes,
and each unexplained change is guaranteed a must_not_change item. Anything dropped or
added is listed in the report.

On the demo PR, which says "small tidy-up, no other behaviour changes intended", the
LLM flags the SSH change:

```
unexplained: aws_vpc_security_group_ingress_rule.ssh_bastion.cidr_ipv4
             10.0.100.0/28 -> 10.0.0.0/16
must_not_change N1: the SSH rule's source must remain the bastion subnet
```

### Step 4: author (LLM call 2 of 2)

Two kinds of test are written, by two different authors:

| Test | Written by | Expected values come from |
|---|---|---|
| **must_not_change** guards | plain code | the pre-change plan |
| **must_hold** tests | the LLM | the LLM's reading of the change |

The guards don't need judgment. Once step 3 has chosen *what* to guard, pinning it to
its pre-change value is exact work. This also means the test that catches the demo's
sneaky change can't be weakened by the LLM:

```hcl
run "n1_ssh_bastion_unchanged" {
  command = plan

  assert {
    condition     = aws_vpc_security_group_ingress_rule.ssh_bastion.cidr_ipv4 == "10.0.100.0/28"
    error_message = "...ssh_bastion.cidr_ipv4 changed: expected \"10.0.100.0/28\""
  }
}
```

Each LLM-written test is checked before it's kept. It must be valid HCL, a single
plan-only `run` block, tied to a real expectation, and **not a tautology**. An assert is
a tautology when every attribute it reads was set to a literal by the diff; parts joined
with `&&` are checked one by one. On the demo, the LLM wrote:

```
from_port == to_port && ip_protocol == app_http.ip_protocol
```

The diff sets both ports to `443`, so the first part can never fail. blastcheck rejects
the test and gives the reason in the report.

### Step 5: validate

A generated test only counts once it has been run. blastcheck runs every test with
`terraform test` (plan-only, mocked AWS provider) on the module **before** and **after**
the change. Each test is in its own file, so one broken test can't stop the others.

| Kind | Before | After | Outcome |
|---|---|---|---|
| must_not_change | pass | pass | kept |
| must_not_change | pass | fail | **regression** |
| must_hold | pass | pass | kept |
| must_hold | fail, or refers to a resource the change creates | pass | kept: confirms the change |
| must_hold | pass | fail | **regression** |
| anything else | | | discarded: a bad test |

"Refers to a resource the change creates" is read from Terraform's own diagnostic
("Reference to undeclared resource") and matched against the resources the plan
creates. No LLM is involved in deciding a test's outcome.

On the demo:

```
regression n1_ssh_bastion_unchanged: passes before the change, fails after it:
           ssh_bastion.cidr_ipv4 changed: expected "10.0.100.0/28"
kept       n2_ssh_bastion_unchanged, n3_app_http_unchanged, n5_bastion_unchanged
kept       app_https_source_is_alb_subnet: refers to resources the change creates
discarded  n4_app_unchanged: bad test: fails before the change
           (aws_security_group.app.revoke_rules_on_delete changed: expected false)
```

N4 shows why validation is needed. Its expected value (`false`) is the real provider's
default from the plan, but under the mocked provider the value is different. The test
fails even on the unchanged module, so it's a bad test, and blastcheck discards it.

The surviving tests, regressions included and marked, are written to
`tests/<change-id>.tftest.hcl`. blastcheck exits with code 1 when it finds a regression.

### Step 6: report

`blastcheck-report.md` leads with the verdict and the regressions. It then lists the
changes the PR description doesn't explain, every test with its outcome and the
reason, the tests rejected before running (with their code), the risks the LLM raised,
anything blastcheck dropped from the LLM's answer, and Checkov's results.

Checkov runs on both copies, and only findings that are **new after the change** are
reported. If Checkov isn't on `PATH`, the report says so.

Install Checkov in its own environment (`pipx install checkov` or `brew install checkov`),
**not** in blastcheck's virtualenv. Checkov depends on `bc-python-hcl2`, which installs
under the same `hcl2` import name and replaces the python-hcl2 that blastcheck needs.
blastcheck stops with a clear error if that happens.

## Running it

```sh
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env     # or export it; .env is git-ignored

blastcheck run --module examples/vpc-sg-bucket/module \
               --diff   examples/vpc-sg-bucket/pr.diff \
               --intent examples/vpc-sg-bucket/pr.md \
               --out    out/
```

The default model is `claude-opus-5-5`; change it with `--model`.

| Exit code | Meaning |
|---|---|
| 0 | No regression found |
| 1 | At least one regression found |
| 2 | blastcheck itself failed (bad diff, terraform error, LLM error) |

**Replay mode.** `--record <dir>` saves the LLM's answers and `--replay <dir>` uses
them instead of calling the API. The demo ships with a recording, so it runs without an
API key and gives the same result every time. A recording only matches the exact prompt
it was made for: if the module, the diff or a prompt template changes, replay refuses
it and you re-record. `--record` reuses recordings that still match, so re-recording
only calls the API for the steps that changed.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Requires Python 3.12+, Terraform 1.7+ and `git`. Tests marked `terraform` run the real
terraform CLI (offline); skip them with `pytest -m 'not terraform'`.

## Limitations

- v1 is AWS-only, plan-only and offline. No real cloud runs, no CI integration yet.
- One module at a time; only top-level `.tf` files.
- Diff paths must be relative to the module directory (`a/main.tf`).
- `moved`, `import`, `check` and `removed` blocks aren't diffed directly; their
  effects are picked up from the plan in step 2.
- AWS provider only. No backend, and every variable needs a default.
- Data sources that call AWS fail, because the credentials are fake on purpose.
- The tautology check is syntactic. It catches asserts decided by the diff's literals,
  but not cleverer restatements (for example via a local that copies a literal).
- One tautological assert rejects the whole test, including any good asserts in it.
- Guards on provider-computed defaults can be discarded, because the mocked provider
  fills them in differently from the real one. Validation catches this; it isn't silent.
- The LLM decides what is worth testing. If it misses something, no test covers it.
  The report's "unexplained changes" and risks make its reasoning visible, but they
  don't replace a reviewer.
