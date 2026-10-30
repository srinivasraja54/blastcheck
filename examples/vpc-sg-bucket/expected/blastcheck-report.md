# blastcheck report: change 2d20d867

**Verdict: REGRESSION FOUND.** 1 test(s) pass before this change and fail after it.

## What the change is meant to do

Adds an ingress rule on the app security group allowing TCP 443 from the ALB subnet, so the app servers can accept TLS passthrough traffic alongside the existing 8080 rule. The PR claims no other behaviour changes.

## Changes the PR description doesn't explain

| Resource | Attribute | Before | After | Why it's flagged |
|---|---|---|---|---|
| `aws_vpc_security_group_ingress_rule.ssh_bastion` | `cidr_ipv4` | `10.0.100.0/28` | `10.0.0.0/16` | The PR describes only adding the HTTPS rule plus a 'small tidy-up' with 'no other behaviour changes intended'. However, this change widens the SSH source from the bastion subnet to the entire VPC, which is a material access change. |

## Regressions

### `n1_ssh_bastion_unchanged`

**Expectation N1:** The SSH rule's source CIDR must remain the bastion subnet (10.0.100.0/28) and must not be widened to the whole VPC (10.0.0.0/16).

Passes before the change, fails after it:

```
Test assertion failed: aws_vpc_security_group_ingress_rule.ssh_bastion.cidr_ipv4 changed: expected "10.0.100.0/28"
```

## Tests

4 kept, 1 regression(s), 1 discarded after running, 1 rejected before running.

| Test | Expectation | Kind | Before | After | Outcome | Reason |
|---|---|---|---|---|---|---|
| `n1_ssh_bastion_unchanged` | N1 | must_not_change, by code | pass | fail | **regression** | passes before the change, fails after it: Test assertion failed: aws_vpc_security_group_ingress_rule.ssh_bastion.cidr_ipv4 changed: expected "10.0.100.0/28" |
| `n2_ssh_bastion_unchanged` | N2 | must_not_change, by code | pass | pass | **kept** | holds before and after the change |
| `n3_app_http_unchanged` | N3 | must_not_change, by code | pass | pass | **kept** | holds before and after the change |
| `n4_app_unchanged` | N4 | must_not_change, by code | fail | fail | **discarded** | bad test: fails before the change (Test assertion failed: aws_security_group.app.revoke_rules_on_delete changed: expected false) |
| `n5_bastion_unchanged` | N5 | must_not_change, by code | pass | pass | **kept** | holds before and after the change |
| `app_https_source_is_alb_subnet` | H1 | must_hold, by llm | error | pass | **kept** | refers to resources the change creates; passes after the change |

Kept tests and regressions are in `tests/2d20d867.tftest.hcl`. Discarded tests are not: a test that fails on the unchanged module (or errors) is a bad test, not a finding.

## Rejected before running

These LLM-written tests were thrown out without being run.

### `app_https_single_port_no_overlap` (H2)

tautology: `aws_vpc_security_group_ingress_rule.app_https.from_port == aws_vpc_security_group_ingress_rule.app_https.to_port` only checks values the diff sets literally (app_https.from_port = 443, app_https.to_port = 443)

```hcl
run "app_https_single_port_no_overlap" {
  command = plan

  assert {
    condition     = aws_vpc_security_group_ingress_rule.app_https.from_port == aws_vpc_security_group_ingress_rule.app_https.to_port && aws_vpc_security_group_ingress_rule.app_https.ip_protocol == aws_vpc_security_group_ingress_rule.app_http.ip_protocol
    error_message = "The HTTPS rule opens a port range instead of a single port, or does not use the same tcp protocol as the app_http rule."
  }

  assert {
    condition     = (aws_vpc_security_group_ingress_rule.app_https.to_port < aws_vpc_security_group_ingress_rule.app_http.from_port || aws_vpc_security_group_ingress_rule.app_https.from_port > aws_vpc_security_group_ingress_rule.app_http.to_port) && (aws_vpc_security_group_ingress_rule.app_https.to_port < aws_vpc_security_group_ingress_rule.ssh_bastion.from_port || aws_vpc_security_group_ingress_rule.app_https.from_port > aws_vpc_security_group_ingress_rule.ssh_bastion.to_port)
    error_message = "The HTTPS rule's port range overlaps the 8080 HTTP rule or the port 22 SSH rule."
  }
}
```

## Risks to consider

From the LLM's reading of the change; not tested.

- **high** `aws_vpc_security_group_ingress_rule.ssh_bastion`: SSH (22) on the app servers becomes reachable from every subnet in the VPC (10.0.0.0/16), including the ALB subnet and the app subnet itself, rather than only from the bastion. This removes the bastion as the single SSH entry point and enables lateral movement. It is hidden under a 'tidy-up' label.
- **medium** `aws_vpc_security_group_ingress_rule.app_https`: With TLS passthrough, the app must actually listen on 443 with valid certificates. Otherwise the ALB health checks or listeners targeting 443 will fail once traffic is switched.
- **low** `aws_vpc_security_group_ingress_rule.ssh_bastion`: Changing cidr_ipv4 on aws_vpc_security_group_ingress_rule may be applied as an in-place update or a replace. During apply there could be a brief window where bastion SSH access is interrupted.

## Checks on the LLM's answer

Nothing dropped or added: every resource and attribute the LLM named exists in the plan.

## Policy checks (Checkov)

Skipped: Checkov is not installed (pip install checkov).

---

Generated by blastcheck. Tests run with `terraform test` (plan-only, mocked AWS provider); no LLM decides any outcome. A human merges.
