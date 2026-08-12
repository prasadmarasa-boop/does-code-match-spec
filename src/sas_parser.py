import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional


@dataclass
class VariableLineage:
    variable: str
    classification: str
    immediate_source: str = ""
    derivation_logic: str = ""
    confidence: str = "High"

    def to_dict(self):
        return asdict(self)


SAS_KEYWORDS = {
    "if", "then", "else", "do", "end", "input", "put", "missing",
    "not", "and", "or", "in", "first", "last"
}


def strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"(?m)^\s*\*.*?;\s*$", "", code)
    return code


def find_final_data_step(code: str):
    clean = strip_comments(code)
    steps = list(re.finditer(
        r"\bdata\s+([A-Za-z_][\w.]*)\s*;(.*?)(?=\brun\s*;)",
        clean,
        flags=re.I | re.S
    ))
    if not steps:
        return None, ""
    m = steps[-1]
    return m.group(1), m.group(2)


def parse_sources(body: str) -> List[str]:
    sources = []
    for kw in ("set", "merge"):
        for m in re.finditer(rf"\b{kw}\s+([^;]+);", body, flags=re.I):
            text = re.sub(r"\([^)]*\)", "", m.group(1))
            for token in re.findall(r"\b[A-Za-z_][\w.]*\b", text):
                if token.lower() not in {"in", "keep", "drop", "rename"}:
                    sources.append(token)
    return list(dict.fromkeys(sources))


def parse_keep(body: str) -> List[str]:
    matches = list(re.finditer(r"\bkeep\s+([^;]+);", body, flags=re.I))
    if not matches:
        return []
    return re.findall(r"\b[A-Za-z_]\w*\b", matches[-1].group(1))


def rhs_variables(rhs: str) -> List[str]:
    quoted_removed = re.sub(r'(["\']).*?\1', "", rhs)
    vars_ = re.findall(r"\b[A-Za-z_]\w*\b", quoted_removed)
    return [
        v for v in vars_
        if v.lower() not in SAS_KEYWORDS
        and not re.fullmatch(r"\d+", v)
        and not re.match(r"^[A-Za-z]\d+$", v)
    ]


def parse_assignments(body: str) -> Dict[str, VariableLineage]:
    out = {}

    # Simple assignments such as trtsdt = input(rfxstdtc,...);
    for m in re.finditer(
        r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*(.+?);",
        body
    ):
        lhs = m.group(1)
        rhs = m.group(2).strip()

        # Ignore SAS dataset options / comparisons accidentally matched
        if lhs.lower() in {"if", "where"}:
            continue

        srcs = rhs_variables(rhs)

        if rhs.lower() == lhs.lower():
            classification = "Assigned"
            immediate = lhs
            logic = "Direct carry-forward"
        elif srcs:
            classification = "Derived"
            immediate = ", ".join(dict.fromkeys(srcs))
            logic = rhs
        else:
            classification = "Constant"
            immediate = ""
            logic = rhs

        out[lhs.upper()] = VariableLineage(
            variable=lhs.upper(),
            classification=classification,
            immediate_source=immediate.upper() if immediate else "",
            derivation_logic=logic,
            confidence="High",
        )

    # Very simple IF/THEN assignments.
    for m in re.finditer(
        r"\bif\s+(.+?)\s+then\s+([A-Za-z_]\w*)\s*=\s*(.+?);",
        body,
        flags=re.I
    ):
        condition, lhs, value = m.groups()
        refs = rhs_variables(condition)
        existing = out.get(lhs.upper())
        logic = f"IF {condition.strip()} THEN {lhs} = {value.strip()}"
        if existing:
            if logic not in existing.derivation_logic:
                existing.derivation_logic += "; " + logic
        else:
            out[lhs.upper()] = VariableLineage(
                variable=lhs.upper(),
                classification="Derived",
                immediate_source=", ".join(dict.fromkeys(refs)).upper(),
                derivation_logic=logic,
                confidence="Medium",
            )

    return out


def analyze_sas(code: str):
    dataset, body = find_final_data_step(code)
    if not dataset:
        return {
            "final_dataset": None,
            "sources": [],
            "variables": [],
            "warnings": ["No DATA step ending in RUN; was detected."]
        }

    keep_vars = [v.upper() for v in parse_keep(body)]
    assignments = parse_assignments(body)
    sources = parse_sources(body)

    rows = []
    for var in keep_vars:
        if var in assignments:
            rows.append(assignments[var].to_dict())
        else:
            rows.append(VariableLineage(
                variable=var,
                classification="Assigned",
                immediate_source=var,
                derivation_logic="Carried from input dataset; exact source resolution pending.",
                confidence="Medium"
            ).to_dict())

    return {
        "final_dataset": dataset.upper(),
        "sources": [s.upper() for s in sources],
        "variables": rows,
        "warnings": []
    }
