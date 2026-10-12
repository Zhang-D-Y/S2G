"""Edit-region location, patch-scope validation and the frozen transformation class.

A region is the text between a unique anchor match (regex) and the next
terminator (`;`). A candidate may differ from its parent only inside that span.
"""
import difflib
import re
from collections import Counter

# Constructs a candidate may never introduce (simulation/synthesis divergence, scope escape).
FORBIDDEN = [r"`", r"\(\*", r"\btranslate_(on|off)\b", r"\bsynthesis\b", r"\bkeep\b", r"\bdont_touch\b",
             r"\bblackbox\b", r"\bfull_case\b", r"\bparallel_case\b", r"'[sS]?[bBoOdDhH]\s*[0-9a-fA-F_]*[xXzZ?]",
             r"\b[xXzZ]\b", r"//", r"/\*", r";", r"\bbegin\b", r"\bend\b", r"\bassign\b"]


def locate(text, region):
    starts = [m.end() for m in re.finditer(region["anchor_regex"], text)]
    if len(starts) != 1:
        raise ValueError(f"anchor /{region['anchor_regex']}/ found {len(starts)} times (need exactly 1)")
    end = text.find(region["terminator"], starts[0])
    if end < 0:
        raise ValueError("terminator not found after anchor")
    return starts[0], end


def body(text, region):
    s, e = locate(text, region)
    return text[s:e]


def replace(text, region, new_body):
    s, e = locate(text, region)
    old = text[s:e]
    lead = old[: len(old) - len(old.lstrip())]
    return text[:s] + lead + new_body.strip() + text[e:]


def scope_check(parent_text, cand_text, region):
    """Everything outside the region must be byte-identical to the parent."""
    try:
        ps, pe = locate(parent_text, region)
        cs, ce = locate(cand_text, region)
    except ValueError as e:
        return False, str(e)
    if parent_text[:ps] != cand_text[:cs] or parent_text[pe:] != cand_text[ce:]:
        return False, "change outside the declared region"
    new = cand_text[cs:ce]
    for pat in FORBIDDEN:
        if re.search(pat, new):
            return False, f"forbidden construct /{pat}/ in region"
    return True, "ok"


_ATOM = r"[A-Za-z_][A-Za-z0-9_$]*(?:\s*\[[^\]\[]+\])*"
_TOKEN = re.compile(rf"\s*(?:(?P<atom>{_ATOM})|(?P<op>\+)|(?P<lp>\()|(?P<rp>\)))")


def tokenize(expr):
    pos, toks = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"unexpected text at {expr[pos:pos + 20]!r}")
        kind = m.lastgroup
        toks.append((kind, re.sub(r"\s+", "", m.group(kind))))
        pos = m.end()
    return toks


def parse_sum(expr):
    """Grammar: E := T ('+' T)* ; T := atom | '(' E ')'. Verilog `+` is left-associative,
    so `a + b + c` parses as ((a + b) + c). Returns a tree: str | (left, right)."""
    toks, i = tokenize(expr), 0

    def expr_():
        nonlocal i
        node = term()
        while i < len(toks) and toks[i][0] == "op":
            i += 1
            node = (node, term())
        return node

    def term():
        nonlocal i
        if i >= len(toks):
            raise ValueError("unexpected end")
        k, v = toks[i]
        if k == "atom":
            i += 1
            return v
        if k == "lp":
            i += 1
            node = expr_()
            if i >= len(toks) or toks[i][0] != "rp":
                raise ValueError("unbalanced parenthesis")
            i += 1
            return node
        raise ValueError(f"unexpected token {v!r}")

    tree = expr_()
    if i != len(toks):
        raise ValueError(f"trailing tokens from {toks[i][1]!r}")
    return tree


def operands(expr):
    def flat(n):
        return [n] if isinstance(n, str) else flat(n[0]) + flat(n[1])
    return flat(parse_sum(expr))


def canonical(expr):
    """Structure-preserving normal form used for duplicate detection: the left-associative
    parse tree, fully parenthesized, whitespace-free. Falls back to whitespace-stripped text."""
    def show(n):
        return n if isinstance(n, str) else f"({show(n[0])}+{show(n[1])})"
    try:
        return show(parse_sum(expr))
    except ValueError:
        return re.sub(r"\s+", "", expr)


# ---- transformation classes (exactly one is frozen per run manifest) ----

def check_transform(transform, parent_body, cand_body):
    """Precondition check for `add_regroup_same_width`: the candidate is a sum of exactly
    the parent's operand multiset using binary `+` and parentheses only. Width and
    signedness preconditions are declared in the region spec and recorded."""
    if transform["id"] != "add_regroup_same_width":
        return False, f"unsupported transform {transform['id']}"
    try:
        p, c = operands(parent_body), operands(cand_body)
    except ValueError as e:
        return False, f"not in transform class: {e}"
    if Counter(p) != Counter(c):
        return False, f"operand multiset changed: {sorted(p)} -> {sorted(c)}"
    return True, "ok"


def bracketings(ops):
    """All full binary bracketings of an operand sequence (order preserved)."""
    if len(ops) == 1:
        return [ops[0]]
    out = []
    for k in range(1, len(ops)):
        for left in bracketings(ops[:k]):
            for right in bracketings(ops[k:]):
                out.append(f"({left} + {right})")
    return out


def unified_patch(parent_text, cand_text, name):
    return "".join(difflib.unified_diff(parent_text.splitlines(True), cand_text.splitlines(True),
                                        f"a/{name}", f"b/{name}"))
