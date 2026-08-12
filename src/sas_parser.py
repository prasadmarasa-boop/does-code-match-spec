
import re
from dataclasses import dataclass, asdict

@dataclass
class VariableLineage:
    variable: str
    classification: str
    immediate_source: str = ""
    ultimate_source: str = ""
    derivation_logic: str = ""
    confidence: str = "High"
    def to_dict(self):
        return asdict(self)

FUNCTIONS = {
    "input","put","ifn","ifc","cat","cats","catt","catx","coalesce","coalescec",
    "substr","scan","strip","trim","left","right","compress","upcase","lowcase",
    "missing","sum","mean","min","max","round","int","datepart","timepart"
}
KEYWORDS = {
    "if","then","else","do","end","not","and","or","in","first","last","where",
    "by","keep","drop","rename","format","informat","length","label","retain"
}

def strip_comments(code):
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"(?m)^\s*\*.*?;\s*$", "", code)
    return code

def split_clause(text):
    out, buf, depth = [], [], 0
    for ch in text.strip():
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if ch.isspace() and depth == 0:
            if buf:
                out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out

def parse_token(token):
    m = re.match(r"^([A-Za-z_][\w.]*)\s*(?:\((.*?)\))?$", token.strip(), re.S)
    if not m:
        return token.upper(), {}
    ds = m.group(1).upper()
    opts = m.group(2) or ""
    d = {}
    x = re.search(r"\bin\s*=\s*([A-Za-z_]\w*)", opts, re.I)
    if x:
        d["in"] = x.group(1).upper()
    return ds, d

def parse_steps(code):
    clean = strip_comments(code)
    steps, order = {}, []
    for m in re.finditer(r"\bdata\s+([A-Za-z_][\w.]*)\s*;(.*?)(?=\brun\s*;)", clean, re.I|re.S):
        out = m.group(1).upper()
        body = m.group(2)
        inputs, aliases = [], {}
        for kw in ("set","merge"):
            for im in re.finditer(rf"\b{kw}\s+([^;]+);", body, re.I|re.S):
                for tok in split_clause(im.group(1)):
                    ds, opts = parse_token(tok)
                    inputs.append(ds)
                    if "in" in opts:
                        aliases[opts["in"]] = ds
        km = list(re.finditer(r"\bkeep\s+([^;]+);", body, re.I))
        keep = [x.upper() for x in re.findall(r"\b[A-Za-z_]\w*\b", km[-1].group(1))] if km else []
        steps[out] = {"body": body, "inputs": list(dict.fromkeys(inputs)), "aliases": aliases, "keep": keep}
        order.append(out)
    return steps, order

def identifiers(expr):
    expr = re.sub(r"'[^']*'|\"[^\"]*\"", " ", expr)
    expr = re.sub(
        r"\b(?:best|comma|date|datetime|time|yymmdd|mmddyy|ddmmyy|e8601da|e8601dt|is8601da|is8601dt)\d*(?:\.\d*)?\b",
        " ", expr, flags=re.I
    )
    names = re.findall(r"\b[A-Za-z_]\w*\b", expr)
    out = []
    for n in names:
        low = n.lower()
        if low in FUNCTIONS or low in KEYWORDS:
            continue
        out.append(n.upper())
    return list(dict.fromkeys(out))

def assignments(body):
    out = {}
    for m in re.finditer(r"\bif\s+(.+?)\s+then\s+([A-Za-z_]\w*)\s*=\s*(.+?);", body, re.I|re.S):
        cond,lhs,rhs = m.groups()
        out.setdefault(lhs.upper(), []).append(("if", cond.strip(), rhs.strip(), identifiers(cond + " " + rhs)))
    for m in re.finditer(r"\belse\s+([A-Za-z_]\w*)\s*=\s*(.+?);", body, re.I|re.S):
        lhs,rhs = m.groups()
        out.setdefault(lhs.upper(), []).append(("else", "", rhs.strip(), identifiers(rhs)))
    for m in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*(.+?);\s*$", body):
        lhs,rhs = m.groups()
        if re.search(r"\bthen\b|\belse\b", m.group(0), re.I):
            continue
        out.setdefault(lhs.upper(), []).append(("simple", "", rhs.strip(), identifiers(rhs)))
    return out

def terminal_dataset(ds, steps, seen=None):
    seen = seen or set()
    if ds in seen:
        return ds
    seen.add(ds)
    st = steps.get(ds)
    if not st or not st["inputs"]:
        return ds
    return " + ".join(terminal_dataset(x, steps, seen.copy()) for x in st["inputs"])

def resolve_ref(current_ds, ref, steps):
    st = steps[current_ds]
    if ref in st["aliases"]:
        ds = st["aliases"][ref]
        return f"{ds} -> {terminal_dataset(ds, steps)}"
    for ds in st["inputs"]:
        dst = steps.get(ds)
        if dst and (ref in dst["keep"] or ref in assignments(dst["body"])):
            return resolve_carried(ds, ref, steps)
    terminals = []
    for ds in st["inputs"]:
        if ds in steps:
            terminals.append(f"{terminal_dataset(ds, steps)}.{ref}")
        else:
            terminals.append(f"{ds}.{ref}")
    return " | ".join(dict.fromkeys(terminals)) if terminals else f"{current_ds}.{ref}"

def resolve_carried(ds, var, steps):
    st = steps.get(ds)
    if not st:
        return f"{ds}.{var}"
    recs = assignments(st["body"]).get(var, [])
    if not recs or all(r[0] == "simple" and r[2].upper() == var for r in recs):
        candidates = []
        for inp in st["inputs"]:
            candidates.append(resolve_carried(inp, var, steps) if inp in steps else f"{inp}.{var}")
        return " | ".join(dict.fromkeys(candidates)) if candidates else f"{ds}.{var}"
    refs = []
    for r in recs:
        refs += [x for x in r[3] if x != var]
    if refs:
        return " | ".join(resolve_ref(ds, x, steps) for x in dict.fromkeys(refs))
    return ds

def analyze_sas(code):
    steps, order = parse_steps(code)
    if not order:
        return {"final_dataset": None, "sources": [], "variables": [], "warnings": ["No DATA step found."]}
    final = order[-1]
    st = steps[final]
    recmap = assignments(st["body"])
    vars_ = st["keep"] or list(recmap)
    rows = []
    for var in vars_:
        recs = recmap.get(var, [])
        if not recs:
            rows.append(VariableLineage(var, "Assigned", var, resolve_carried(final, var, steps),
                                        "Carried from input dataset.", "Medium").to_dict())
            continue
        if all(r[0] == "simple" and r[2].upper() == var for r in recs):
            rows.append(VariableLineage(var, "Assigned", var, resolve_carried(final, var, steps),
                                        "Direct carry-forward", "High").to_dict())
            continue
        refs, logic = [], []
        for kind,cond,rhs,ids in recs:
            refs += [x for x in ids if x != var]
            if kind == "if":
                logic.append(f"IF {cond} THEN {var} = {rhs}")
            elif kind == "else":
                logic.append(f"ELSE {var} = {rhs}")
            else:
                logic.append(f"{var} = {rhs}")
        refs = list(dict.fromkeys(refs))
        immediate = ", ".join(refs)
        ultimate = " | ".join(dict.fromkeys(resolve_ref(final, x, steps) for x in refs)) if refs else ""
        confidence = "High" if any(x in st["aliases"] for x in refs) else "Medium"
        rows.append(VariableLineage(var, "Derived" if refs else "Constant", immediate, ultimate,
                                    "; ".join(logic), confidence).to_dict())
    return {"final_dataset": final, "sources": st["inputs"], "variables": rows, "warnings": []}
