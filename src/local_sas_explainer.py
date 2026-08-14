import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "sas-explainer:latest"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

SYSTEM_INSTRUCTIONS = """You explain implemented Clinical SAS derivation logic using only deterministic metadata.
Describe what the program does; do not compare it with the specification or assign validation status.
The deterministic lineage and supported evidence are authoritative. Never add or infer datasets,
variables, statements, values, filenames, or line numbers. Specification text is optional context only.
Return a concise summary, evidence-supported source identities, ordered implemented steps, and explicit
uncertainty or limitations. If the metadata does not support a detail, identify that limitation."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 500},
        "supported_sources": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 20,
        },
        "implemented_steps": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
    },
    "required": ["summary", "supported_sources", "implemented_steps", "limitations"],
    "additionalProperties": False,
}

SAFE_EXPLANATION_TERMS = {
    "SAS", "DATA", "SET", "MERGE", "PROC", "SORT", "INPUT", "PUT", "IF", "THEN",
    "ELSE", "AND", "OR", "NOT", "DATE", "DATETIME", "MISSING", "LOGIC", "SOURCE",
}


@dataclass(frozen=True)
class DerivationExplanation:
    summary: str
    supported_sources: list
    implemented_steps: list
    limitations: list
    accepted: bool = True

    def to_dict(self):
        return asdict(self)


class LocalSASExplainer(Protocol):
    def explain(self, payload):
        """Return a DerivationExplanation for the approved deterministic metadata."""


def build_explanation_payload(row):
    evidence_statements = []
    for evidence in row.get("evidence", []):
        statement = str(evidence.get("statement", "")).strip()
        if evidence.get("supported") and statement and statement not in evidence_statements:
            evidence_statements.append(statement)

    payload = {
        "variable_name": str(row.get("variable", "")),
        "deterministic_lineage_paths": row.get("lineage_paths", []),
        "deterministic_derivation_logic": str(row.get("derivation_logic", "")),
        "supported_evidence_statements": evidence_statements,
    }
    specification_context = {
        "origin": str(row.get("spec_origin", "")).strip(),
        "source": str(row.get("spec_source", "")).strip(),
        "derivation": str(row.get("spec_derivation", "")).strip(),
    }
    if any(specification_context.values()):
        payload["optional_specification_context"] = specification_context
    return payload


def _supported_source_identities(payload):
    variable = str(payload.get("variable_name", "")).upper()
    identities = set()
    for path in payload.get("deterministic_lineage_paths", []):
        for index, node in enumerate(path):
            identity = str(node).strip().upper()
            is_final_target = index == len(path) - 1 and identity.split(".")[-1] == variable
            if not identity or is_final_target:
                continue
            identities.add(identity)
            parts = identity.split(".")
            if len(parts) == 3:
                identities.add(".".join(parts[:2]))
    return identities


def _approved_code_terms(payload):
    deterministic_payload = {
        key: value
        for key, value in payload.items()
        if key != "optional_specification_context"
    }
    text = json.dumps(deterministic_payload, ensure_ascii=False).upper()
    return set(re.findall(r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)*\b", text))


def prioritize_explanation_rows(rows):
    return sorted(
        enumerate(rows),
        key=lambda item: (
            item[1].get("status") != "REVIEW REQUIRED",
            str(item[1].get("variable", "")),
        ),
    )


def _rejected_explanation(reason):
    return DerivationExplanation(
        summary="The local explanation was rejected because it exceeded deterministic evidence.",
        supported_sources=[],
        implemented_steps=[],
        limitations=[reason],
        accepted=False,
    )


def _clean_list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Expected a bounded list.")
    return [" ".join(str(item).split()) for item in value]


def validate_explanation(explanation, payload):
    summary = " ".join(str(explanation.summary).split())
    try:
        sources = _clean_list(explanation.supported_sources, 20)
        steps = _clean_list(explanation.implemented_steps, 12)
        limitations = _clean_list(explanation.limitations, 8)
    except ValueError:
        return _rejected_explanation("The model returned an invalid explanation structure.")
    if not summary or len(summary) > 500 or any(len(item) > 500 for item in steps + limitations):
        return _rejected_explanation("The model returned missing or oversized explanation text.")

    allowed_sources = _supported_source_identities(payload)
    unsupported_sources = [source for source in sources if source.upper() not in allowed_sources]
    if unsupported_sources:
        return _rejected_explanation(
            "Unsupported source claim(s): " + ", ".join(unsupported_sources)
        )

    explanation_text = " ".join([summary, *sources, *steps, *limitations])
    if re.search(r"\bline\s+\d+\b", explanation_text, re.I):
        return _rejected_explanation("The model introduced a line reference that was not provided.")
    approved_terms = _approved_code_terms(payload) | SAFE_EXPLANATION_TERMS
    dotted_terms = {
        term.upper()
        for term in re.findall(
            r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)+\b",
            explanation_text,
            re.I,
        )
    }
    unsupported_dotted_terms = dotted_terms - approved_terms
    if unsupported_dotted_terms:
        return _rejected_explanation(
            "Unsupported lineage identity: " + ", ".join(sorted(unsupported_dotted_terms))
        )
    explanation_terms = set(
        re.findall(r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)*\b", explanation_text)
    )
    unsupported_terms = explanation_terms - approved_terms
    if unsupported_terms:
        return _rejected_explanation(
            "Unsupported lineage identity or SAS term: " + ", ".join(sorted(unsupported_terms))
        )
    return DerivationExplanation(summary, sources, steps, limitations, accepted=True)


def explain_row(row, explainer):
    updated = dict(row)
    original_status = row.get("status")
    payload = build_explanation_payload(row)
    try:
        raw_explanation = explainer.explain(payload)
        explanation = validate_explanation(raw_explanation, payload)
    except Exception:
        explanation = DerivationExplanation(
            summary="The local SAS explanation service was unavailable.",
            supported_sources=[],
            implemented_steps=[],
            limitations=["Deterministic lineage and validation status remain unchanged."],
            accepted=False,
        )
    updated["local_derivation_explanation"] = explanation.to_dict()
    if "status" in row:
        updated["status"] = original_status
    return updated


def explain_rows(rows, explainer):
    return [explain_row(row, explainer) for row in rows]


def _validate_loopback_endpoint(endpoint):
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/api/chat"
    ):
        raise ValueError("The local model endpoint must be a loopback HTTP /api/chat URL.")
    return endpoint


def _post_json(endpoint, body, timeout):
    request = Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class OllamaSASExplainer:
    def __init__(
        self,
        model=DEFAULT_MODEL,
        endpoint=DEFAULT_ENDPOINT,
        timeout=60,
        transport=None,
    ):
        self.model = str(model).strip() or DEFAULT_MODEL
        self.endpoint = _validate_loopback_endpoint(str(endpoint).strip())
        self.timeout = timeout
        self.transport = transport or _post_json

    def explain(self, payload):
        body = {
            "model": self.model,
            "stream": False,
            "format": OUTPUT_SCHEMA,
            "options": {"temperature": 0, "seed": 7},
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ],
        }
        response = self.transport(self.endpoint, body, self.timeout)
        content = response.get("message", {}).get("content", "")
        parsed = json.loads(content)
        return DerivationExplanation(
            summary=parsed.get("summary", ""),
            supported_sources=parsed.get("supported_sources", []),
            implemented_steps=parsed.get("implemented_steps", []),
            limitations=parsed.get("limitations", []),
        )


def explainer_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    enabled = str(environ.get("LOCAL_SAS_EXPLAINER_ENABLED", "")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None, (
            "Explain Implemented Logic is off. Set LOCAL_SAS_EXPLAINER_ENABLED=1 "
            "to make it available."
        )
    model = str(environ.get("LOCAL_SAS_EXPLAINER_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    endpoint = (
        str(environ.get("LOCAL_SAS_EXPLAINER_URL", DEFAULT_ENDPOINT)).strip()
        or DEFAULT_ENDPOINT
    )
    try:
        return OllamaSASExplainer(model=model, endpoint=endpoint), ""
    except ValueError as error:
        return None, f"Local SAS Derivation Explanation is disabled: {error}"
