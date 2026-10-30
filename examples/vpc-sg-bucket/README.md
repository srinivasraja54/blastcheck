# Demo: vpc-sg-bucket

A small AWS module (VPC, three subnets, an app security group, a log bucket) and a PR
against it.

| File | What it is |
|---|---|
| `module/` | The module **before** the PR |
| `pr.diff` | The PR, as a unified diff (paths relative to `module/`) |
| `pr.md` | The PR description: the author's stated intent |

## What the PR does

1. **Stated:** adds an HTTPS (443) ingress rule from the ALB subnet.
2. **Not stated:** changes the SSH rule's source from `aws_subnet.bastion.cidr_block`
   (`10.0.100.0/28`) to `aws_vpc.main.cidr_block` (`10.0.0.0/16`). That opens SSH to
   the whole VPC instead of only the bastion. The diff looks like a one-line refactor, and
   the rule's description still says "bastion subnet only".

The widening stays inside private address space, so policy scanners that look for
`0.0.0.0/0` don't flag it. blastcheck should catch it because the change isn't part
of the PR's stated intent.
