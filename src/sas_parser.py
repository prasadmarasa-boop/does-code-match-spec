import re
from dataclasses import asdict, dataclass


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
    "input", "put", "ifn", "ifc", "cat", "cats", "catt", "catx", "coalesce",
    "coalescec", "substr", "scan", "strip", "trim", "left", "right", "compress",
    "upcase", "lowcase", "missing", "sum", "mean", "min", "max", "round", "int",
    "datepart", "timepart",
}
KEYWORDS = {
    "if", "then", "else", "do", "end", "not", "and", "or", "in", "first", "last",
    "where", "by", "keep", "drop", "rename", "format", "informat", "length", "label",
    "retain",
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
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _option_variables(options, name):
    if not options:
        return []
    match = re.search(
        rf"\b{name}\s*=\s*(?:\((.*?)\)|(.+?)(?=\b(?:keep|drop|rename|where|in|firstobs|obs)\s*=|$))",
        options,
        re.I | re.S,
    )
    if not match:
        return []
    value = match.group(1) if match.group(1) is not None else match.group(2)
    return [x.upper() for x in re.findall(r"\b[A-Za-z_]\w*\b", value)]


def parse_token(token):
    match = re.match(r"^([A-Za-z_][\w.]*)\s*(?:\((.*)\))?$", token.strip(), re.S)
    if not match:
        return token.upper(), {}
    dataset = match.group(1).upper()
    options = match.group(2) or ""
    parsed = {}
    indicator = re.search(r"\bin\s*=\s*([A-Za-z_]\w*)", options, re.I)
    if indicator:
        parsed["in"] = indicator.group(1).upper()
    keep = _option_variables(options, "keep")
    if keep:
        parsed["keep"] = keep
    return dataset, parsed


def _combined_keep(*keeps):
    present = [set(values) for values in keeps if values]
    if not present:
        return []
    combined = present[0]
    for values in present[1:]:
        combined &= values
    return sorted(combined)


def _dataset_option(block, name):
    match = re.search(
        rf"\b{name}\s*=\s*([A-Za-z_][\w.]*)\s*(?:\(([^;]*)\))?",
        block,
        re.I | re.S,
    )
    if not match:
        return None, {}
    dataset = match.group(1).upper()
    options = match.group(2) or ""
    parsed = {"keep": _option_variables(options, "keep")}
    return dataset, parsed


def parse_steps(code):
    clean = strip_comments(code)
    events = []

    data_pattern = r"\bdata\s+([A-Za-z_][\w.]*)\s*(?:\(([^;]*)\))?\s*;(.*?)(?=\brun\s*;)"
    for match in re.finditer(data_pattern, clean, re.I | re.S):
        output = match.group(1).upper()
        output_options = match.group(2) or ""
        body = match.group(3)
        inputs, aliases, input_keep = [], {}, {}
        for keyword in ("set", "merge"):
            for input_match in re.finditer(rf"\b{keyword}\s+([^;]+);", body, re.I | re.S):
                for token in split_clause(input_match.group(1)):
                    dataset, options = parse_token(token)
                    inputs.append(dataset)
                    if "in" in options:
                        aliases[options["in"]] = dataset
                    if options.get("keep"):
                        input_keep[dataset] = options["keep"]

        keep_statements = list(re.finditer(r"\bkeep\s+([^;]+);", body, re.I))
        statement_keep = (
            [x.upper() for x in re.findall(r"\b[A-Za-z_]\w*\b", keep_statements[-1].group(1))]
            if keep_statements
            else []
        )
        output_keep = _option_variables(output_options, "keep")
        step = {
            "kind": "data",
            "body": body,
            "inputs": list(dict.fromkeys(inputs)),
            "aliases": aliases,
            "input_keep": input_keep,
            "keep": _combined_keep(statement_keep, output_keep),
        }
        events.append((match.start(), output, step))

    for match in re.finditer(r"\bproc\s+sort\b(.*?)(?=\brun\s*;)", clean, re.I | re.S):
        block = match.group(1)
        source, source_options = _dataset_option(block, "data")
        output, output_options = _dataset_option(block, "out")
        if not source or not output or source == output:
            continue
        step = {
            "kind": "proc_sort",
            "body": block,
            "inputs": [source],
            "aliases": {},
            "input_keep": {source: source_options["keep"]} if source_options.get("keep") else {},
            "keep": output_options.get("keep", []),
        }
        events.append((match.start(), output, step))

    steps, order = {}, []
    for _, output, step in sorted(events):
        steps[output] = step
        order.append(output)
    return steps, order


def identifiers(expr):
    expr = re.sub(r"'[^']*'|\"[^\"]*\"", " ", expr)
    expr = re.sub(
        r"\b(?:best|comma|date|datetime|time|yymmdd|mmddyy|ddmmyy|e8601da|e8601dt|is8601da|is8601dt)\d*(?:\.\d*)?\b",
        " ",
        expr,
        flags=re.I,
    )
    names = re.findall(r"\b[A-Za-z_]\w*\b", expr)
    out = []
    for name in names:
        if name.lower() not in FUNCTIONS and name.lower() not in KEYWORDS:
            out.append(name.upper())
    return list(dict.fromkeys(out))


def assignments(body):
    out = {}
    for match in re.finditer(
        r"\bif\s+([^;]+?)\s+then\s+([A-Za-z_]\w*)\s*=\s*([^;]*);",
        body,
        re.I,
    ):
        condition, lhs, rhs = match.groups()
        out.setdefault(lhs.upper(), []).append(
            ("if", condition.strip(), rhs.strip(), identifiers(condition + " " + rhs))
        )
    for match in re.finditer(r"\belse\s+([A-Za-z_]\w*)\s*=\s*([^;]*);", body, re.I):
        lhs, rhs = match.groups()
        out.setdefault(lhs.upper(), []).append(("else", "", rhs.strip(), identifiers(rhs)))
    for match in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*(.+?);\s*$", body):
        lhs, rhs = match.groups()
        if re.search(r"\bthen\b|\belse\b", match.group(0), re.I):
            continue
        out.setdefault(lhs.upper(), []).append(("simple", "", rhs.strip(), identifiers(rhs)))
    return out


def terminal_dataset(dataset, steps, seen=None):
    seen = set() if seen is None else seen
    if dataset in seen:
        return dataset
    seen.add(dataset)
    step = steps.get(dataset)
    if not step or not step["inputs"]:
        return dataset
    return " + ".join(terminal_dataset(x, steps, seen.copy()) for x in step["inputs"])


def _step_can_output(dataset, variable, steps):
    step = steps.get(dataset)
    return not step or not step["keep"] or variable in step["keep"]


def _input_allows(step, dataset, variable):
    keep = step.get("input_keep", {}).get(dataset, [])
    return not keep or variable in keep


def resolve_ref(current_dataset, ref, steps):
    step = steps[current_dataset]
    if ref in step["aliases"]:
        dataset = step["aliases"][ref]
        return f"{dataset} -> {terminal_dataset(dataset, steps)}"

    candidates = [
        dataset
        for dataset in step["inputs"]
        if _input_allows(step, dataset, ref) and _step_can_output(dataset, ref, steps)
    ]
    if not candidates:
        return f"{current_dataset}.{ref}"
    return " | ".join(dict.fromkeys(resolve_carried(x, ref, steps) for x in candidates))


def resolve_carried(dataset, variable, steps, seen=None):
    seen = set() if seen is None else seen
    key = (dataset, variable)
    if key in seen:
        return f"{dataset}.{variable}"
    seen.add(key)

    step = steps.get(dataset)
    if not step:
        return f"{dataset}.{variable}"
    records = assignments(step["body"]).get(variable, [])
    direct = records and all(
        record[0] == "simple" and record[2].upper() == variable for record in records
    )
    if not records or direct:
        candidates = [
            source
            for source in step["inputs"]
            if _input_allows(step, source, variable) and _step_can_output(source, variable, steps)
        ]
        resolved = [resolve_carried(x, variable, steps, seen.copy()) for x in candidates]
        return " | ".join(dict.fromkeys(resolved)) if resolved else f"{dataset}.{variable}"

    refs = []
    for record in records:
        refs.extend(x for x in record[3] if x != variable)
    if refs:
        return " | ".join(resolve_ref(dataset, x, steps) for x in dict.fromkeys(refs))
    return dataset


def analyze_sas(code):
    steps, order = parse_steps(code)
    if not order:
        return {
            "final_dataset": None,
            "sources": [],
            "variables": [],
            "warnings": ["No DATA step or supported PROC SORT output found."],
        }

    final = order[-1]
    step = steps[final]
    record_map = assignments(step["body"])
    variables = step["keep"] or list(record_map)
    rows = []
    for variable in variables:
        records = record_map.get(variable, [])
        if not records:
            rows.append(
                VariableLineage(
                    variable,
                    "Assigned",
                    variable,
                    resolve_carried(final, variable, steps),
                    "Carried from input dataset.",
                    "Medium",
                ).to_dict()
            )
            continue
        if all(record[0] == "simple" and record[2].upper() == variable for record in records):
            rows.append(
                VariableLineage(
                    variable,
                    "Assigned",
                    variable,
                    resolve_carried(final, variable, steps),
                    "Direct carry-forward",
                    "High",
                ).to_dict()
            )
            continue

        refs, logic = [], []
        for kind, condition, rhs, ids in records:
            refs.extend(x for x in ids if x != variable)
            if kind == "if":
                logic.append(f"IF {condition} THEN {variable} = {rhs}")
            elif kind == "else":
                logic.append(f"ELSE {variable} = {rhs}")
            else:
                logic.append(f"{variable} = {rhs}")
        refs = list(dict.fromkeys(refs))
        immediate = ", ".join(refs)
        ultimate = " | ".join(dict.fromkeys(resolve_ref(final, x, steps) for x in refs)) if refs else ""
        confidence = "High" if any(x in step["aliases"] for x in refs) else "Medium"
        rows.append(
            VariableLineage(
                variable,
                "Derived" if refs else "Constant",
                immediate,
                ultimate,
                "; ".join(logic),
                confidence,
            ).to_dict()
        )
    return {"final_dataset": final, "sources": step["inputs"], "variables": rows, "warnings": []}
