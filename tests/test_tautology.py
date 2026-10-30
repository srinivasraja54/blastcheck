from blastcheck.models import ChangedBlock
from blastcheck.tautology import diff_literals, references, split_and, tautological_part

HTTPS = "aws_vpc_security_group_ingress_rule.app_https"
HTTP = "aws_vpc_security_group_ingress_rule.app_http"
SSH = "aws_vpc_security_group_ingress_rule.ssh_bastion"
ADDRESSES = {HTTPS, HTTP, SSH, "aws_subnet.alb"}

# What the demo PR writes: a new rule with literal ports, and an SSH rule whose
# source changes to an expression.
BLOCKS = [
    ChangedBlock(
        HTTPS,
        "resource",
        "create",
        None,
        {"from_port": 443, "to_port": 443, "ip_protocol": "tcp", "cidr_ipv4": "${aws_subnet.alb.cidr_block}"},
    ),
    ChangedBlock(
        SSH,
        "resource",
        "update",
        {"from_port": 22, "cidr_ipv4": "${aws_subnet.bastion.cidr_block}"},
        {"from_port": 22, "cidr_ipv4": "${aws_vpc.main.cidr_block}"},
    ),
]
LITERALS = diff_literals(BLOCKS)


def _tautology(condition: str) -> str | None:
    return tautological_part(condition, LITERALS, ADDRESSES)


def test_literals_cover_created_blocks_and_changed_attributes_only():
    assert LITERALS == {
        (HTTPS, "from_port"): 443,
        (HTTPS, "to_port"): 443,
        (HTTPS, "ip_protocol"): "tcp",
    }


def test_restating_a_literal_is_a_tautology():
    assert _tautology(f"{HTTPS}.from_port == 443")


def test_comparing_two_diff_literals_is_a_tautology():
    assert _tautology(f"{HTTPS}.from_port == {HTTPS}.to_port")


def test_resolved_expression_is_not_a_tautology():
    # The diff writes `aws_subnet.alb.cidr_block`, not the CIDR itself.
    assert _tautology(f'{HTTPS}.cidr_ipv4 == "10.0.10.0/24"') is None


def test_relation_to_a_resource_outside_the_diff_is_not_a_tautology():
    assert _tautology(f"{HTTPS}.from_port != {HTTP}.from_port") is None


def test_unchanged_attribute_of_an_updated_block_is_not_a_tautology():
    assert _tautology(f"{SSH}.from_port == 22") is None


def test_tautology_cannot_hide_behind_and():
    part = _tautology(f"{HTTPS}.from_port == {HTTPS}.to_port && {HTTPS}.ip_protocol == {HTTP}.ip_protocol")
    assert part == f"{HTTPS}.from_port == {HTTPS}.to_port"


def test_or_inside_brackets_is_judged_as_a_whole():
    condition = f"({HTTPS}.to_port < {HTTP}.from_port || {HTTPS}.from_port > {HTTP}.to_port)"
    assert _tautology(condition) is None


def test_split_and_ignores_brackets_and_strings():
    assert split_and('a == "x && y" && (b || c && d) && e') == ['a == "x && y"', "(b || c && d)", "e"]


def test_references_only_match_known_addresses():
    refs = references(f"{HTTPS}.from_port == aws_instance.unknown.port", ADDRESSES)
    assert refs == {(HTTPS, "from_port")}
