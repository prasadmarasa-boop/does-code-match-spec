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
    contributing_datasets: tuple = ()

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
    def blank(match):
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    code = re.sub(r"/\*.*?\*/", blank, code, flags=re.S)
    code = re.sub(r"(?m)^\s*\*.*?;\s*$", blank, code)
    return code


def evidence_for_span(code, start, end, kind, relationship="", note=""):
    end_position = max(start, end - 1)
    return {
        "kind": kind,
        "start_line": code.count("\n", 0, start) + 1,
        "end_line": code.count("\n", 0, end_position) + 1,
        "statement": code[start:end].strip(),
        "relationship": relationship,
        "supported": True,
        "note": note,
    }


def unsupported_evidence(relationship, note):
    return {
        "kind": "unsupported",
        "start_line": None,
        "end_line": None,
        "statement": "",
        "relationship": relationship,
        "supported": False,
        "note": note,
    }


def evidence_with_relationship(evidence, relationship, note=""):
    copy = dict(evidence)
    copy["relationship"] = relationship
    if note:
        copy["note"] = note
    return copy


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


def transformation_evidence(body, body_start, code):
    evidence = []
    for kind, pattern in (
        ("where", r"\bwhere\s+[^;]+;"),
        ("by", r"\bby\s+[^;]+;"),
        ("subsetting_if", r"\bif\s+[^;]+;"),
    ):
        for match in re.finditer(pattern, body, re.I):
            if kind == "subsetting_if" and re.search(r"\bthen\b", match.group(0), re.I):
                continue
            evidence.append(
                evidence_for_span(
                    code,
                    body_start + match.start(),
                    body_start + match.end(),
                    kind,
                )
            )
    return evidence


def parse_steps(code):
    clean = strip_comments(code)
    events = []

    data_pattern = r"\bdata\s+([A-Za-z_][\w.]*)\s*(?:\(([^;]*)\))?\s*;(.*?)(?=\brun\s*;)"
    for match in re.finditer(data_pattern, clean, re.I | re.S):
        output = match.group(1).upper()
        output_options = match.group(2) or ""
        body = match.group(3)
        body_start = match.start(3)
        inputs, aliases, input_keep, input_evidence = [], {}, {}, {}
        for keyword in ("set", "merge"):
            for input_match in re.finditer(rf"\b{keyword}\s+([^;]+);", body, re.I | re.S):
                statement_evidence = evidence_for_span(
                    code,
                    body_start + input_match.start(),
                    body_start + input_match.end(),
                    keyword.lower(),
                )
                for token in split_clause(input_match.group(1)):
                    dataset, options = parse_token(token)
                    inputs.append(dataset)
                    input_evidence.setdefault(dataset, []).append(statement_evidence)
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
        keep_evidence = [
            evidence_for_span(
                code,
                body_start + keep_match.start(),
                body_start + keep_match.end(),
                "keep",
            )
            for keep_match in keep_statements[-1:]
        ]
        step = {
            "kind": "data",
            "body": body,
            "inputs": list(dict.fromkeys(inputs)),
            "aliases": aliases,
            "input_keep": input_keep,
            "input_evidence": input_evidence,
            "keep": _combined_keep(statement_keep, output_keep),
            "keep_evidence": keep_evidence,
            "output_evidence": evidence_for_span(
                code, match.start(), match.start(3), "data"
            ),
            "assignments": assignments(body, body_start, code),
            "transformation_evidence": transformation_evidence(body, body_start, code),
        }
        events.append((match.start(), output, step))

    for match in re.finditer(r"\bproc\s+sort\b(.*?)(?=\brun\s*;)", clean, re.I | re.S):
        block = match.group(1)
        source, source_options = _dataset_option(block, "data")
        output, output_options = _dataset_option(block, "out")
        if not source or not output or source == output:
            continue
        header_end = clean.find(";", match.start(), match.end()) + 1
        sort_evidence = evidence_for_span(code, match.start(), header_end, "proc_sort")
        block_start = match.start(1)
        step = {
            "kind": "proc_sort",
            "body": block,
            "inputs": [source],
            "aliases": {},
            "input_keep": {source: source_options["keep"]} if source_options.get("keep") else {},
            "input_evidence": {source: [sort_evidence]},
            "keep": output_options.get("keep", []),
            "keep_evidence": [],
            "output_evidence": sort_evidence,
            "assignments": {},
            "transformation_evidence": transformation_evidence(block, block_start, code),
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


def assignments(body, body_start=0, code=None):
    out = {}
    for match in re.finditer(
        r"\bif\s+([^;]+?)\s+then\s+([A-Za-z_]\w*)\s*=\s*([^;]*);",
        body,
        re.I,
    ):
        condition, lhs, rhs = match.groups()
        evidence = (
            evidence_for_span(code, body_start + match.start(), body_start + match.end(), "if_assignment")
            if code is not None
            else None
        )
        out.setdefault(lhs.upper(), []).append(
            ("if", condition.strip(), rhs.strip(), identifiers(condition + " " + rhs), evidence)
        )
    for match in re.finditer(r"\belse\s+([A-Za-z_]\w*)\s*=\s*([^;]*);", body, re.I):
        lhs, rhs = match.groups()
        evidence = (
            evidence_for_span(code, body_start + match.start(), body_start + match.end(), "else_assignment")
            if code is not None
            else None
        )
        out.setdefault(lhs.upper(), []).append(("else", "", rhs.strip(), identifiers(rhs), evidence))
    for match in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*(.+?);\s*$", body):
        lhs, rhs = match.groups()
        if re.search(r"\bthen\b|\belse\b", match.group(0), re.I):
            continue
        evidence = (
            evidence_for_span(code, body_start + match.start(), body_start + match.end(), "assignment")
            if code is not None
            else None
        )
        out.setdefault(lhs.upper(), []).append(("simple", "", rhs.strip(), identifiers(rhs), evidence))
    return out


def observed_variables(step):
    """Return variables explicitly observable when a DATA step has no output KEEP list."""
    if step["kind"] != "data":
        return []
    record_map = step["assignments"]
    observed = list(record_map)
    for records in record_map.values():
        for record in records:
            observed.extend(record[3])
    for match in re.finditer(r"\bby\s+([^;]+);", step["body"], re.I):
        observed.extend(re.findall(r"\b[A-Za-z_]\w*\b", match.group(1)))
    aliases = set(step["aliases"])
    return list(dict.fromkeys(x.upper() for x in observed if x.upper() not in aliases))


def output_variables(dataset, steps, seen=None):
    """Return (observable variables, complete) for a parsed output dataset."""
    seen = set() if seen is None else seen
    if dataset in seen:
        return [], False
    seen.add(dataset)
    step = steps.get(dataset)
    if not step:
        return [], False
    if step["keep"]:
        return list(step["keep"]), True
    if step["kind"] == "proc_sort":
        source = step["inputs"][0] if step["inputs"] else None
        return output_variables(source, steps, seen) if source else ([], False)
    return observed_variables(step), False


def terminal_dataset(dataset, steps, seen=None):
    seen = set() if seen is None else seen
    if dataset in seen:
        return dataset
    seen.add(dataset)
    step = steps.get(dataset)
    if not step or not step["inputs"]:
        return dataset
    return " + ".join(terminal_dataset(x, steps, seen.copy()) for x in step["inputs"])


def terminal_datasets(dataset, steps, seen=None):
    seen = set() if seen is None else seen
    if dataset in seen:
        return {dataset}
    seen.add(dataset)
    step = steps.get(dataset)
    if not step or not step["inputs"]:
        return {dataset}
    out = set()
    for source in step["inputs"]:
        out.update(terminal_datasets(source, steps, seen.copy()))
    return out


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
    records = step["assignments"].get(variable, []) if step["kind"] == "data" else []
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


def _append_trace(trace, node, evidence_records, note=""):
    previous = trace["nodes"][-1] if trace["nodes"] else "unknown"
    relationship = f"{previous} -> {node}"
    evidence = list(trace["evidence"])
    if evidence_records:
        evidence.extend(
            evidence_with_relationship(record, relationship, note)
            for record in evidence_records
            if record is not None
        )
    else:
        evidence.append(
            unsupported_evidence(
                relationship,
                note or "No exact supported SAS statement was found for this lineage hop.",
            )
        )
    return {"nodes": [*trace["nodes"], node], "evidence": evidence}


def trace_dataset(dataset, steps, seen=None):
    seen = set() if seen is None else seen
    if dataset in seen:
        return [
            {
                "nodes": [dataset],
                "evidence": [
                    unsupported_evidence(dataset, "Dataset lineage cycle prevents exact tracing.")
                ],
            }
        ]
    seen.add(dataset)
    step = steps.get(dataset)
    if not step or not step["inputs"]:
        return [{"nodes": [dataset], "evidence": []}]
    traces = []
    for source in step["inputs"]:
        records = [
            *step["input_evidence"].get(source, []),
            *step.get("transformation_evidence", []),
        ]
        for trace in trace_dataset(source, steps, seen.copy()):
            traces.append(_append_trace(trace, dataset, records))
    return traces


def trace_reference(current_dataset, ref, steps, seen=None):
    step = steps[current_dataset]
    if ref in step["aliases"]:
        source = step["aliases"][ref]
        records = [
            *step["input_evidence"].get(source, []),
            *step.get("transformation_evidence", []),
        ]
        return [
            _append_trace(trace, f"merge indicator {ref}", records)
            for trace in trace_dataset(source, steps, seen)
        ]

    candidates = [
        dataset
        for dataset in step["inputs"]
        if _input_allows(step, dataset, ref) and _step_can_output(dataset, ref, steps)
    ]
    if not candidates:
        node = f"{ref} (unresolved in {current_dataset})"
        return [
            {
                "nodes": [node],
                "evidence": [
                    unsupported_evidence(
                        node,
                        "No supported SET or MERGE input can be proven to provide this variable.",
                    )
                ],
            }
        ]
    traces = []
    for source in candidates:
        records = [
            *step["input_evidence"].get(source, []),
            *step.get("transformation_evidence", []),
        ]
        for trace in trace_variable(source, ref, steps, seen):
            traces.append(_append_trace(trace, ref, records))
    return traces


def trace_variable(dataset, variable, steps, seen=None):
    seen = set() if seen is None else seen
    key = (dataset, variable)
    if key in seen:
        node = f"{dataset}.{variable}"
        return [
            {
                "nodes": [node],
                "evidence": [
                    unsupported_evidence(node, "Variable lineage cycle prevents exact tracing.")
                ],
            }
        ]
    seen.add(key)
    step = steps.get(dataset)
    node = f"{dataset}.{variable}"
    if not step:
        return [{"nodes": [node], "evidence": []}]

    records = step["assignments"].get(variable, []) if step["kind"] == "data" else []
    direct = records and all(
        record[0] == "simple" and record[2].upper() == variable for record in records
    )
    if records and not direct:
        traces = []
        for record in records:
            refs = [ref for ref in record[3] if ref != variable]
            for ref in refs:
                for trace in trace_reference(dataset, ref, steps, seen.copy()):
                    traces.append(_append_trace(trace, node, [record[4]]))
        if traces:
            return traces
        return [
            {
                "nodes": [node],
                "evidence": [
                    unsupported_evidence(
                        node,
                        "The assignment has no resolvable variable source; only constant logic is supported.",
                    )
                ],
            }
        ]

    candidates = [
        source
        for source in step["inputs"]
        if _input_allows(step, source, variable) and _step_can_output(source, variable, steps)
    ]
    if not candidates:
        return [
            {
                "nodes": [node],
                "evidence": [
                    unsupported_evidence(
                        node,
                        "No exact supported input statement can establish the ultimate source.",
                    )
                ],
            }
        ]
    traces = []
    assignment_evidence = [record[4] for record in records if record[4] is not None]
    for source in candidates:
        input_evidence = [
            *step["input_evidence"].get(source, []),
            *step.get("transformation_evidence", []),
        ]
        edge_evidence = [*input_evidence, *assignment_evidence]
        for trace in trace_variable(source, variable, steps, seen.copy()):
            traces.append(_append_trace(trace, node, edge_evidence))
    return traces


def _deduplicate_evidence(records):
    seen, out = set(), []
    for record in records:
        key = (
            record.get("kind"),
            record.get("start_line"),
            record.get("end_line"),
            record.get("relationship"),
            record.get("supported"),
            record.get("note"),
        )
        if key not in seen:
            seen.add(key)
            out.append(record)
    return out


def variable_audit(dataset, variable, steps, records):
    traces = trace_variable(dataset, variable, steps)
    evidence = [item for trace in traces for item in trace["evidence"]]
    evidence.extend(record[4] for record in records if record[4] is not None)
    step = steps[dataset]
    evidence.extend(
        evidence_with_relationship(record, f"Output includes {dataset}.{variable}")
        for record in step.get("keep_evidence", [])
        if variable in step["keep"]
    )
    evidence.append(
        evidence_with_relationship(
            step["output_evidence"], f"Defines output dataset {dataset}"
        )
    )
    ambiguity_notes = []
    if len(traces) > 1:
        ambiguity_notes.append(
            "Multiple supported lineage paths exist; the actual contributing source cannot be proven."
        )
    if any(not record["supported"] for record in evidence):
        ambiguity_notes.append("One or more lineage hops lack exact supported SAS evidence.")
    return {
        "lineage_paths": [trace["nodes"] for trace in traces],
        "evidence": _deduplicate_evidence(evidence),
        "ambiguity_notes": ambiguity_notes,
    }


def analyze_sas(code):
    steps, order = parse_steps(code)
    if not order:
        return {
            "final_dataset": None,
            "sources": [],
            "variables": [],
            "variables_complete": False,
            "warnings": ["No DATA step or supported PROC SORT output found."],
        }

    final = order[-1]
    step = steps[final]
    record_map = step["assignments"] if step["kind"] == "data" else {}
    variables, variables_complete = output_variables(final, steps)
    rows = []
    for variable in variables:
        records = record_map.get(variable, [])
        audit = variable_audit(final, variable, steps, records)
        if not records:
            row = VariableLineage(
                    variable,
                    "Assigned",
                    variable,
                    resolve_carried(final, variable, steps),
                    "Carried from input dataset.",
                    "Medium",
                ).to_dict()
            row.update(audit)
            rows.append(row)
            continue
        if all(record[0] == "simple" and record[2].upper() == variable for record in records):
            row = VariableLineage(
                    variable,
                    "Assigned",
                    variable,
                    resolve_carried(final, variable, steps),
                    "Direct carry-forward",
                    "High",
                ).to_dict()
            row.update(audit)
            rows.append(row)
            continue

        refs, logic = [], []
        for kind, condition, rhs, ids, _ in records:
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
        contributing_datasets = set()
        for ref in refs:
            if ref in step["aliases"]:
                contributing_datasets.update(terminal_datasets(step["aliases"][ref], steps))
        row = VariableLineage(
                variable,
                "Derived" if refs else "Constant",
                immediate,
                ultimate,
                "; ".join(logic),
                confidence,
                tuple(sorted(contributing_datasets)),
            ).to_dict()
        row.update(audit)
        rows.append(row)
    warnings = []
    if not variables_complete:
        warnings.append(
            "The complete final variable set could not be determined because no output KEEP list was found."
        )
    return {
        "final_dataset": final,
        "sources": step["inputs"],
        "upstream_sources": sorted(
            {
                upstream
                for source in step["inputs"]
                for upstream in terminal_datasets(source, steps)
            }
        ),
        "variables": rows,
        "variables_complete": variables_complete,
        "warnings": warnings,
    }
