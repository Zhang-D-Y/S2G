"""Small, fail-closed grammar for local mux-only procedural blocks.

Only out may be assigned; conditions/selectors may read sel and constants;
data leaves may read a..i or the all-ones default. No declarations, calls,
loops, attributes, arithmetic on data, or auxiliary signals are admitted.
Functional equivalence is checked separately, not inferred from this grammar.
"""
import re

SELECTOR="sel"
SELECTOR_WIDTH=4
OUTPUT="out"
PORTS=tuple("abcdefghi")
DEFAULT=65535
WIDTH=16

def configure(d):
    global SELECTOR,SELECTOR_WIDTH,OUTPUT,PORTS,DEFAULT,WIDTH
    SELECTOR=d["selector"]; SELECTOR_WIDTH=d["selector_width"]; OUTPUT=d["output"]; PORTS=tuple(d["ports"]); DEFAULT=d["default"]; WIDTH=d["width"]

TOKEN = re.compile(r"\s*(\d+'[sS]?[bBoOdDhH][0-9a-fA-F_]+|'[01]|[A-Za-z_][A-Za-z0-9_$]*|\d+|===|!==|==|!=|<=|>=|&&|\|\||[()\[\]{}?:;,@*~!&|^=<>])")
OPS = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6, "===": 6, "!==": 6, "<": 7, ">": 7, "<=": 7, ">=": 7}


def number(tok):
    if tok in ("'0", "'1"):
        return 0 if tok == "'0" else (1 << WIDTH)-1
    if tok.isdigit():
        return int(tok)
    m = re.fullmatch(r"(\d+)'([bBoOdDhH])([0-9a-fA-F_]+)", tok)
    if not m:
        raise ValueError("only explicit unsigned binary/octal/decimal/hex constants are allowed")
    value = int(m[3].replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[m[2].lower()])
    if not 1 <= int(m[1]) <= 32 or value >= 2 ** int(m[1]):
        raise ValueError("constant width or value out of bounds")
    return value


def tokenize(text):
    if len(text.encode()) > 6000 or any(s in text for s in ("`", "//", "/*")) or re.search(r"\(\*\s*[A-Za-z_]", text):
        raise ValueError("oversized block or forbidden directive/comment/attribute")
    result, pos = [], 0
    text = text.strip()
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if not match:
            raise ValueError("unsupported token near " + repr(text[pos:pos+24]))
        result.append(match[1]); pos = match.end()
    return result


def control(node):
    if node[0] in ("sel", "constant"):
        return True
    return node[0] in ("unary", "binary") and all(control(n) for n in node[2:])


def mux(node):
    return node[0] == "data" or (node[0] == "constant" and node[1] == DEFAULT) or (
        node[0] == "ternary" and control(node[1]) and mux(node[2]) and mux(node[3]))


class Parser:
    def __init__(self, text):
        self.ts, self.i = tokenize(text), 0

    def peek(self):
        return self.ts[self.i] if self.i < len(self.ts) else None

    def pop(self, expected=None):
        got = self.peek()
        if got is None or (expected is not None and got != expected):
            raise ValueError(f"expected {expected!r}, got {got!r}")
        self.i += 1
        return got

    def expr(self, min_prec=0):
        tok = self.pop()
        if tok == "(":
            node = self.expr(); self.pop(")")
        elif tok in ("~", "!", "&", "|", "^"):
            node = ("unary", tok, self.expr(8))
        elif tok == SELECTOR:
            node = ("sel",)
            if self.peek() == "[":
                self.pop(); high = number(self.pop()); low = high
                if self.peek() == ":":
                    self.pop(); low = number(self.pop())
                self.pop("]")
                if not 0 <= low <= high < SELECTOR_WIDTH:
                    raise ValueError("sel slice out of range")
                node = ("sel", high, low)
        elif tok in PORTS:
            node = ("data", tok)
        else:
            node = ("constant", number(tok))
        while self.peek() in OPS and OPS[self.peek()] >= min_prec:
            op = self.pop(); node = ("binary", op, node, self.expr(OPS[op] + 1))
        if min_prec == 0 and self.peek() == "?":
            self.pop(); yes = self.expr(); self.pop(":"); node = ("ternary", node, yes, self.expr())
        return node

    def statement(self):
        tok = self.pop()
        if tok == "begin":
            while self.peek() != "end":
                self.statement()
            self.pop()
        elif tok == "if":
            self.pop("("); cond = self.expr(); self.pop(")")
            if not control(cond):
                raise ValueError("condition reads data or output")
            self.statement()
            if self.peek() == "else":
                self.pop(); self.statement()
        elif tok == "case":
            self.pop("("); sel = self.expr(); self.pop(")")
            if not control(sel):
                raise ValueError("case selector reads data or output")
            while self.peek() != "endcase":
                if self.peek() == "default":
                    self.pop()
                else:
                    number(self.pop())
                    while self.peek() == ",":
                        self.pop(); number(self.pop())
                self.pop(":"); self.statement()
            self.pop()
        elif tok == OUTPUT:
            self.pop("="); expr = self.expr(); self.pop(";")
            if not mux(expr):
                raise ValueError("output must select an unchanged data port or frozen default")
        else:
            raise ValueError(f"unsupported statement {tok!r}")

    def parse(self):
        tok = self.pop()
        if tok == "always":
            self.pop("@")
            if self.peek() == "(":
                self.pop(); self.pop("*"); self.pop(")")
            else:
                self.pop("*")
        elif tok != "always_comb":
            raise ValueError("one combinational always block is required")
        self.statement()
        if self.peek() is not None:
            raise ValueError("extra text outside the single block")
        return self.ts


def check(text):
    try:
        return True, " ".join(Parser(text).parse())
    except (ValueError, RecursionError) as exc:
        return False, str(exc)
