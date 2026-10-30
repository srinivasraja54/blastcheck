# Allow HTTPS from the ALB subnet

The load balancer is moving to TLS passthrough, so the app servers need to accept
HTTPS (443) from the ALB subnet in addition to the existing 8080 rule.

- Adds `aws_vpc_security_group_ingress_rule.app_https` (443 from the ALB subnet)
- Small tidy-up in the security group rules

No other behaviour changes intended.
