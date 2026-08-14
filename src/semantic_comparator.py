import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Protocol


SEMANTIC_MATCH = "SEMANTIC MATCH"
SEMANTIC_MISMATCH = "SEMANTIC MISMATCH"
UNCERTAIN = "UNCERTAIN"
ALLOWED_ASSESSMENTS = {SEMANTIC_MATCH, SEMANTIC_MISMATCH, UNCERTAIN}

SYSTEM_INSTRUCTIONS = """You compare specification derivation semantics with deterministic SAS lineage metadata.
The deterministic lineage is authoritative. Use only the supplied JSON fields.
Never add or infer datasets, variables, SAS statements, values, or line numbers.
Classify only semantic equivalence of the documented and implemented derivations.
Return SEMANTIC MATCH when they express the same transformation, SEMANTIC MISMATCH when they
clearly express different transformations, and UNCERTAIN when the evidence is insufficient.
Keep the rationale concise and refer only to metadata present in the input."""

SAFE_RATIONALE_TERMS = {
    "AI", "SAS", "SPEC", "SPECIFICATION", "PROGRAM", "DERIVATION", "SEMANTIC",
    "MATCH", "MISMATCH", "UNCERTAIN", "IF", "THEN", "ELSE", "AND", "OR",
}


@dataclass(frozen=True)
class SemanticResult:
    assessment: str
    rationale: str

    def to_dict(self):
        return asdict(self)


class SemanticComparator(Protocol):
    def compare(self, payload):
        """Return SemanticResult for the approved metadata payload."""


def build_semantic_payload(row):
    evidence_statements = []
    for evidence in row.get("evidence", []):
        statement = str(evidence.get("statement", "")).strip()
        if evidence.get("supported") and statement and statement not in evidence_statements:
            evidence_statements.append(statement)
    return {
        "variable_name": str(row.get("variable", "")),
        "deterministic_lineage_path": row.get("lineage_paths", []),
        "deterministic_derivation_logic": str(row.get("derivation_logic", "")),
        "specification": {
            "origin": str(row.get("spec_origin", "")),
            "source": str(row.get("spec_source", "")),
            "derivation": str(row.get("spec_derivation", "")),
        },
        "evidence_statements": evidence_statements,
    }


def _approved_code_terms(payload):
    text = json.dumps(payload, ensure_ascii=False).upper()
    return set(re.findall(r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)*\b", text))


def validate_semantic_result(result, payload):
    assessment = str(result.assessment).upper().strip()
    rationale = " ".join(str(result.rationale).split())
    if assessment not in ALLOWED_ASSESSMENTS:
        return SemanticResult(UNCERTAIN, "AI output was rejected because its assessment label was invalid.")
    if not rationale or len(rationale) > 400:
        return SemanticResult(UNCERTAIN, "AI output was rejected because its rationale was missing or too long.")
    if re.search(r"\bline\s+\d+\b", rationale, re.I) or ";" in rationale or "`" in rationale:
        return SemanticResult(
            UNCERTAIN,
            "AI output was rejected because it introduced an unapproved statement or line reference.",
        )
    approved = _approved_code_terms(payload) | SAFE_RATIONALE_TERMS
    rationale_terms = set(
        re.findall(r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)*\b", rationale)
    )
    if rationale_terms - approved:
        return SemanticResult(
            UNCERTAIN,
            "AI output was rejected because it referenced metadata outside the approved input.",
        )
    return SemanticResult(assessment, rationale)


def apply_semantic_assessments(rows, comparator):
    assessed = []
    for row in rows:
        updated = dict(row)
        updated["semantic_assessment"] = ""
        updated["semantic_rationale"] = ""
        updated["semantic_assisted"] = False
        if (
            row.get("status") != "REVIEW REQUIRED"
            or not str(row.get("spec_derivation", "")).strip()
            or not str(row.get("derivation_logic", "")).strip()
        ):
            assessed.append(updated)
            continue
        payload = build_semantic_payload(row)
        try:
            raw_result = comparator.compare(payload)
            result = validate_semantic_result(raw_result, payload)
        except Exception:
            result = SemanticResult(
                UNCERTAIN,
                "AI semantic comparison was unavailable; deterministic status is unchanged.",
            )
        updated["semantic_assessment"] = result.assessment
        updated["semantic_rationale"] = result.rationale
        updated["semantic_assisted"] = True
        assessed.append(updated)
    return assessed


class OpenAISemanticComparator:
    def __init__(self, api_key, model="gpt-5.6", client=None):
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
        self.client = client
        self.model = model

    def compare(self, payload):
        from typing import Literal

        from pydantic import BaseModel

        class SemanticResponse(BaseModel):
            assessment: Literal["SEMANTIC MATCH", "SEMANTIC MISMATCH", "UNCERTAIN"]
            rationale: str

        response = self.client.responses.parse(
            model=self.model,
            store=False,
            input=[
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            text_format=SemanticResponse,
        )
        parsed = response.output_parsed
        if parsed is None:
            return SemanticResult(UNCERTAIN, "The AI returned no structured semantic assessment.")
        return SemanticResult(parsed.assessment, parsed.rationale)


def comparator_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    api_key = str(environ.get("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return None, "AI semantic comparison is disabled because OPENAI_API_KEY is not configured."
    model = str(environ.get("OPENAI_SEMANTIC_MODEL", "gpt-5.6")).strip() or "gpt-5.6"
    try:
        return OpenAISemanticComparator(api_key=api_key, model=model), ""
    except ImportError:
        return None, "AI semantic comparison is disabled because the optional OpenAI SDK is not installed."
