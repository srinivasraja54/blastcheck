"""Reject tautology tests. Plain code, no LLM.

A tautology test asserts something the diff literally says. If the diff sets
`from_port = 443`, then `assert { condition = ...from_port == 443 }` can never tell a
reviewer anything: it passes exactly when the diff says what it says.

The rule used here: a condition is a tautology when **every** resource attribute it
references was set to a literal value by the diff (in a created block, or as a changed
attribute of an updated block). Its outcome is then fully decided by the diff's text.

Conditions joined with `&&` are checked part by part, so a tautology can't hide next to
a real check: in `a.from_port == a.to_port && a.cidr == b.cidr`, the first part is
decided by the diff alone and the assert is rejected.

Attributes set to an expression (`cidr_ipv4 = aws_subnet.alb.cidr_block`) are not
literals: asserting their resolved value checks something the diff doesn't spell out,
so those asserts are allowed.
"""

import re
from typing import Any

from blastcheck.models import ChangedBlock

Literals = dict[tuple[str, str], Any]


def diff_literals(blocks: list[ChangedBlock]) -> Literals:
    """(address, attribute) -> literal value, for every literal the diff writes."""
    literals: Literals = {}
    for block in blocks:
        if block.kind != "resource" or block.after is None:
            continue
        attrs = block.after.keys() if block.action == "create" else block.changed_attrs
        for attr in attrs:
            value = block.after.get(attr)
            if _is_literal(value):
                literals[(block.address, attr)] = value
    return literals


def _is_literal(value: Any) -> bool:
    if isinstance(value, bool | int | float):
        return True
    return isinstance(value, str) and "${" not in value


def references(condition: str, addresses: set[str]) -> set[tuple[str, str]]:
    """(address, attribute) pairs a condition refers to, for known resource addresses."""
    refs = set()
    for address in addresses:
        for match in re.finditer(re.escape(address) + r"\.([A-Za-z_][A-Za-z0-9_]*)", condition):
            refs.add((address, match.group(1)))
    return refs


def tautological_part(condition: str, literals: Literals, addresses: set[str]) -> str | None:
    """The first `&&`-joined part of the condition that is a tautology, if any."""
    for part in split_and(condition):
        refs = references(part, addresses)
        if refs and all(ref in literals for ref in refs):
            return part
    return None


def split_and(condition: str) -> list[str]:
    """Split on top-level `&&`: not inside brackets or string literals."""
    parts, depth, start, i = [], 0, 0, 0
    in_string = False
    while i < len(condition):
        char = condition[i]
        if in_string:
            if char == "\\":
                i += 1  # skip the escaped character
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and condition.startswith("&&", i):
            parts.append(condition[start:i].strip())
            start = i + 2
            i += 1
        i += 1
    parts.append(condition[start:].strip())
    return parts
