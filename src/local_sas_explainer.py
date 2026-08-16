import json
import logging
import os
import re
import socket
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "sas-lineage-assistant:latest"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOGGER = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """You explain one selected Clinical SAS variable using only deterministic metadata.
Describe what the program does; do not compare it with the specification or assign validation status.
The deterministic lineage and supported evidence are authoritative. Never add or infer datasets,
variables, statements, values, filenames, or line numbers. Specification text is optional context only.
Return a concise summary, evidence-supported source identities, ordered implemented steps, and explicit
uncertainty or limitations. Each lineage path is ordered from ultimate source to final target.
Explain the shortest evidence-backed chain for variable_name. Do not summarize the whole DATA step,
mention unrelated inputs, or describe SORT/KEEP/MERGE operations unless the supplied evidence for this
selected variable requires them. Preserve supported intermediate datasets and merge indicators in the
implemented steps, explaining every lineage hop in the supplied order. Use exact MERGE participants
from supported evidence and never reinterpret an upstream SET input as a MERGE input. Every node
supplied in a deterministic lineage path is supported; never claim that
such a node or its upstream source is unknown, unlisted, or unsupported.
The supported_sources list may contain only evidence-supported source or intermediate nodes before
the final target; never list the final target as a source. If metadata does not support a detail,
identify that limitation. Return only a JSON object with exactly these keys: summary (string),
supported_sources (array of strings), implemented_steps (array of strings), and limitations
(array of strings). Use the lineage-path direction exactly as supplied. Return sources once, in
first-seen lineage order. Do not number implemented_steps; the UI adds numbering. Do not use markdown
fences."""

CORRECTION_INSTRUCTIONS = """Recheck the response using this deterministic feedback: {feedback}
Explain only the selected variable's shortest supported chain. The last node in each path is the target,
not a source. Deduplicate sources, remove unrelated steps, and return a corrected JSON object only."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 500},
        "supported_sources": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 20,
            "description": (
                "Only source or intermediate identities that occur before the last node of a "
                "deterministic lineage path; never include a path's final target node."
            ),
        },
        "implemented_steps": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
            "description": (
                "Concise unnumbered prose explaining only the selected variable's required lineage "
                "hops and derivation; do not copy a raw SAS statement as an entire step."
            ),
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

@dataclass(frozen=True)
class DerivationExplanation:
    summary: str
    supported_sources: list
    implemented_steps: list
    limitations: list
    accepted: bool = True
    error: str = ""

    def to_dict(self):
        return asdict(self)


class LocalSASExplainer(Protocol):
    def explain(self, payload):
        """Return a DerivationExplanation for the approved deterministic metadata."""


class LocalExplanationError(Exception):
    def __init__(self, user_message, diagnostic_message=""):
        super().__init__(diagnostic_message or user_message)
        self.user_message = user_message
        self.diagnostic_message = diagnostic_message or user_message


class OllamaHTTPError(LocalExplanationError):
    def __init__(self, status, ollama_message):
        self.status = status
        self.ollama_message = ollama_message
        lowered = ollama_message.lower()
        if status == 404 or "model" in lowered and "not found" in lowered:
            user_message = "model not found"
        elif status in {400, 422} and any(
            term in lowered for term in ("format", "schema", "json")
        ):
            user_message = "structured output is unsupported"
        else:
            user_message = f"Ollama HTTP {status}"
        super().__init__(user_message, f"HTTP {status}: {ollama_message}")


class InvalidStructuredResponse(LocalExplanationError):
    def __init__(self, diagnostic_message="Ollama returned invalid structured JSON."):
        super().__init__("invalid structured response", diagnostic_message)


class OllamaUnavailable(LocalExplanationError):
    pass


def _ordered_unique(values):
    seen = set()
    result = []
    for value in values:
        cleaned = " ".join(str(value).split())
        key = cleaned.upper()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _lineage_edges(paths):
    return {
        f"{left} -> {right}".upper()
        for path in paths
        for left, right in zip(path, path[1:])
    }


def _focused_evidence_statements(row):
    paths = row.get("lineage_paths", [])
    edges = _lineage_edges(paths)
    target = str(row.get("variable", "")).upper()
    statements = []
    for evidence in row.get("evidence", []):
        statement = str(evidence.get("statement", "")).strip()
        relationship = str(evidence.get("relationship", "")).strip().upper()
        kind = str(evidence.get("kind", "")).lower()
        if not evidence.get("supported") or not statement or kind in {"data", "keep", "proc_sort"}:
            continue
        relevant_edge = relationship in edges
        direct_target_logic = bool(
            re.search(rf"\b{re.escape(target)}\b\s*=", statement, re.I)
        )
        if not relevant_edge and not direct_target_logic:
            continue
        # BY/subsetting statements duplicate pass-through evidence for qualified variables.
        left_identity = relationship.split(" -> ", 1)[0] if " -> " in relationship else ""
        if kind in {"by", "subsetting_if"} and "." in left_identity:
            continue
        indicator_match = re.search(r"merge indicator\s+([A-Z_][A-Z0-9_]*)", relationship, re.I)
        if kind == "subsetting_if" and indicator_match:
            referenced = re.search(r"\bif\s+([A-Z_][A-Z0-9_]*)\b", statement, re.I)
            if referenced and referenced.group(1).upper() != indicator_match.group(1).upper():
                continue
        # A variable carry-forward path is enough context; a full MERGE statement can expose
        # unrelated inputs that are not part of the selected variable's shortest chain.
        if kind == "merge" and "." in left_identity:
            continue
        statements.append(statement)
    return _ordered_unique(statements)


def build_explanation_payload(row):
    payload = {
        "variable_name": str(row.get("variable", "")),
        "deterministic_lineage_paths": row.get("lineage_paths", []),
        "deterministic_derivation_logic": str(row.get("derivation_logic", "")),
        "supported_evidence_statements": _focused_evidence_statements(row),
    }
    supported_sources = _supported_source_identities(payload)
    spec_source = str(row.get("spec_source", "")).strip()
    if spec_source.upper() not in supported_sources:
        spec_source = ""
    spec_derivation = str(row.get("spec_derivation", "")).strip()
    unsupported_spec_identities = {
        identity.upper()
        for identity in re.findall(
            r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)+\b",
            spec_derivation,
            re.I,
        )
        if identity.upper() not in _supported_lineage_identities(payload)
    }
    if unsupported_spec_identities:
        spec_derivation = ""
    specification_context = {
        "origin": str(row.get("spec_origin", "")).strip(),
        "source": spec_source,
        "derivation": spec_derivation,
    }
    if any(specification_context.values()):
        payload["optional_specification_context"] = specification_context
    return payload


def _ordered_supported_source_identities(payload):
    variable = str(payload.get("variable_name", "")).upper()
    identities = []
    for path in payload.get("deterministic_lineage_paths", []):
        for index, node in enumerate(path):
            identity = str(node).strip().upper()
            is_final_target = index == len(path) - 1 and identity.split(".")[-1] == variable
            if not identity or is_final_target:
                continue
            identities.append(identity)
            parts = identity.split(".")
            if len(parts) == 3:
                identities.append(".".join(parts[:2]))
    return _ordered_unique(identities)


def _supported_source_identities(payload):
    return set(_ordered_supported_source_identities(payload))


def _supported_lineage_identities(payload):
    identities = set(_ordered_supported_source_identities(payload))
    for path in payload.get("deterministic_lineage_paths", []):
        for node in path:
            identity = str(node).strip().upper()
            identities.add(identity)
            parts = identity.split(".")
            if len(parts) >= 2:
                identities.add(".".join(parts[:-1]))
    return identities


def _unsupported_dataset_variable_claims(explanation_text, payload):
    """Find explicit prose pairings that contradict every deterministic path identity."""
    supported_pairs = set()
    for path in payload.get("deterministic_lineage_paths", []):
        for node in path:
            parts = str(node).strip().upper().split(".")
            if len(parts) >= 2:
                supported_pairs.add((".".join(parts[:-1]), parts[-1]))

    claim_patterns = [
        r"\b([A-Z_][A-Z0-9_]*)\s+variable\s+(?:from|in)\s+(?:the\s+)?"
        r"([A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?)\s+dataset\b",
        r"\b(?:[A-Z_][A-Z0-9_]*\s+)?variable\s+([A-Z_][A-Z0-9_]*)\s+"
        r"(?:from|in)\s+(?:the\s+)?"
        r"([A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?)\s+dataset\b",
    ]
    claims = []
    for pattern in claim_patterns:
        claims.extend(re.findall(pattern, explanation_text, re.I))
    return [
        f"{dataset.upper()}.{variable.upper()}"
        for variable, dataset in claims
        if (dataset.upper(), variable.upper()) not in supported_pairs
    ]


def prioritize_explanation_rows(rows):
    return sorted(
        enumerate(rows),
        key=lambda item: (
            item[1].get("status") != "REVIEW REQUIRED",
            str(item[1].get("variable", "")),
        ),
    )


def cache_explanation(state, cache_key, explanation):
    """Persist one explanation through mapping assignment, including Streamlit session state."""
    cache = dict(state.get("local_derivation_explanations", {}))
    cache[cache_key] = explanation
    state["local_derivation_explanations"] = cache
    return cache


def _safe_error_message(error):
    if isinstance(error, LocalExplanationError):
        return error.user_message
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "request timed out"
    return "unexpected local adapter error"


def _log_explainer_error(error):
    status = getattr(error, "status", "none")
    if isinstance(error, LocalExplanationError):
        diagnostic = error.diagnostic_message
    else:
        diagnostic = str(error) or type(error).__name__
    diagnostic = " ".join(diagnostic.split())[:500]
    LOGGER.error(
        "Local SAS explanation failed: type=%s status=%s message=%s",
        type(error).__name__,
        status,
        diagnostic,
    )


def _safe_rejected_fragments(explanation, payload):
    if explanation is None or payload is None:
        return [], []
    sources = _preferred_model_sources(payload)
    steps = []
    for path in payload.get("deterministic_lineage_paths", []):
        steps.extend(f"{left} → {right}" for left, right in zip(path, path[1:]))
    derivation = " ".join(str(payload.get("deterministic_derivation_logic", "")).split())
    if derivation:
        steps.append(derivation)
    return _ordered_unique(sources), _ordered_unique(steps)


def _rejected_explanation(reason, explanation=None, payload=None):
    sources, steps = _safe_rejected_fragments(explanation, payload)
    return DerivationExplanation(
        summary=(
            "The model response was rejected; the deterministic lineage and derivation are shown "
            "below instead."
        ),
        supported_sources=sources,
        implemented_steps=steps,
        limitations=[reason, "Deterministic lineage and validation status remain authoritative."],
        accepted=False,
    )


def _clean_list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Expected a bounded list.")
    return [" ".join(str(item).split()) for item in value]


def _strip_step_number(step):
    return re.sub(r"^\s*(?:\d+\s*[.)]|[-*])\s*", "", step).strip()


def _allowed_dotted_identities(payload):
    allowed = _supported_lineage_identities(payload)
    deterministic_text = " ".join(
        [
            str(payload.get("deterministic_derivation_logic", "")),
            *payload.get("supported_evidence_statements", []),
        ]
    )
    allowed.update(
        term.upper()
        for term in re.findall(
            r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)+\b",
            deterministic_text,
            re.I,
        )
    )
    return allowed


def _allowed_atomic_identities(payload):
    allowed = {str(payload.get("variable_name", "")).upper()}
    for identity in _supported_lineage_identities(payload):
        allowed.update(part for part in identity.split(".") if part)
        if " " not in identity and "." not in identity:
            allowed.add(identity)
    deterministic_text = " ".join(
        [
            str(payload.get("deterministic_derivation_logic", "")),
            *payload.get("supported_evidence_statements", []),
        ]
    )
    allowed.update(
        token.upper()
        for token in re.findall(r"\b[A-Z_][A-Z0-9_]*\b", deterministic_text, re.I)
    )
    return allowed


def _identity_validation_issues(explanation_text, payload):
    allowed_dotted = _allowed_dotted_identities(payload)
    dotted = {
        term.upper()
        for term in re.findall(
            r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)+\b",
            explanation_text,
            re.I,
        )
    }
    issues = [f"Unsupported lineage identity: {term}" for term in sorted(dotted - allowed_dotted)]

    allowed_atomic = _allowed_atomic_identities(payload)
    sas_identifier = r"(?:[A-Z][A-Z0-9_]*|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*)"
    claim_patterns = [
        rf"\b(?:dataset|table)\s+({sas_identifier})\b",
        rf"\b({sas_identifier})\s+(?:dataset|table)\b",
        rf"\bmerge\s+(?!indicator\b)({sas_identifier})\b",
        rf"\bsort\s+({sas_identifier})\b",
        rf"\b(?:variable|column)\s+({sas_identifier})\b",
        rf"\b({sas_identifier})\s+(?:variable|column)\b",
        rf"\b({sas_identifier})\s*=",
    ]
    for pattern in claim_patterns:
        for match in re.findall(pattern, explanation_text):
            claimed = match[0] if isinstance(match, tuple) else match
            claimed = claimed.upper()
            if claimed not in allowed_atomic:
                issues.append(f"Unsupported identity claim: {claimed}")

    allowed_indicators = {
        match.group(1).upper()
        for path in payload.get("deterministic_lineage_paths", [])
        for node in path
        if (match := re.fullmatch(r"merge indicator\s+([A-Z_][A-Z0-9_]*)", str(node), re.I))
    }
    for indicator in re.findall(
        r"\bmerge\s+indicator\s+([A-Z_][A-Z0-9_]*)\b", explanation_text, re.I
    ):
        if indicator.upper() not in allowed_indicators:
            issues.append(f"Unsupported merge indicator: {indicator.upper()}")
    issues.extend(
        f"Unsupported dataset/variable pairing: {pairing}"
        for pairing in _unsupported_dataset_variable_claims(explanation_text, payload)
    )
    issues.extend(_unsupported_merge_claims(explanation_text, payload))
    issues.extend(_unsupported_directional_source_claims(explanation_text, payload))
    return _ordered_unique(issues)


def _unsupported_directional_source_claims(explanation_text, payload):
    allowed_sources = _supported_source_identities(payload)
    claims = re.findall(
        r"\b[A-Z_][A-Z0-9_]*\s+from\s+"
        r"([A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)+)\b",
        explanation_text,
        re.I,
    )
    return [
        f"Unsupported directional source claim: {identity.upper()}"
        for identity in claims
        if identity.upper() not in allowed_sources
    ]


def _unsupported_merge_claims(explanation_text, payload):
    supported_participants = set()
    for statement in payload.get("supported_evidence_statements", []):
        merge_match = re.search(r"\bmerge\s+(.+?);", statement, re.I | re.S)
        if not merge_match:
            continue
        clause = re.sub(r"\([^)]*\)", " ", merge_match.group(1))
        supported_participants.update(
            token.upper()
            for token in re.findall(r"\b[A-Z_][A-Z0-9_]*\b", clause, re.I)
        )
    if not supported_participants:
        return []

    claims = re.findall(
        r"\bmerges?\s+([A-Z_][A-Z0-9_]*)(?:\s*\([^)]*\))?\s+"
        r"(?:with|and)\s+([A-Z_][A-Z0-9_]*)(?:\s*\([^)]*\))?",
        explanation_text,
        re.I,
    )
    issues = []
    for left, right in claims:
        unsupported = {
            participant.upper()
            for participant in (left, right)
            if participant.upper() not in supported_participants
        }
        if unsupported:
            issues.append(
                "Unsupported MERGE participant(s): " + ", ".join(sorted(unsupported))
            )
    return issues


def _unsupported_operation_claims(explanation_text, payload):
    deterministic_text = " ".join(
        [
            str(payload.get("deterministic_derivation_logic", "")),
            *payload.get("supported_evidence_statements", []),
        ]
    ).upper()
    operation_support = {
        "SORT": "SORT" in deterministic_text,
        "KEEP": "KEEP" in deterministic_text,
        "MERGE": "MERGE" in deterministic_text,
        "FILTER": "WHERE" in deterministic_text or "IF FIRST." in deterministic_text,
    }
    return [
        f"Unsupported or unrelated operation: {operation}"
        for operation, supported in operation_support.items()
        if not supported and re.search(rf"\b{operation}(?:ED|ING|S)?\b", explanation_text, re.I)
    ]


def _missing_required_lineage_nodes(summary, steps, payload):
    prose = " ".join([summary, *steps])
    missing = []
    for path in payload.get("deterministic_lineage_paths", []):
        indicator_index = next(
            (
                index
                for index, node in enumerate(path)
                if re.fullmatch(r"merge indicator\s+[A-Z_][A-Z0-9_]*", str(node), re.I)
            ),
            None,
        )
        if indicator_index is None:
            continue
        for node in path[: indicator_index + 1]:
            if not re.search(rf"(?<![A-Z0-9_]){re.escape(str(node))}(?![A-Z0-9_])", prose, re.I):
                missing.append(str(node))
    return _ordered_unique(missing)


def _unsupported_unknown_source_limitation(limitations, payload):
    if not _ordered_supported_source_identities(payload):
        return False
    return any(
        re.search(
            r"(?:source\s+(?:is\s+)?unknown|does\s+not\s+(?:explicitly\s+)?mention\s+the\s+source|"
            r"does\s+not\s+(?:explicitly\s+)?specify\s+the\s+(?:exact\s+)?source|"
            r"(?:exact\s+)?source\s+(?:is\s+)?not\s+(?:explicitly\s+)?"
            r"(?:known|stated|mentioned|specified))",
            limitation,
            re.I,
        )
        for limitation in limitations
    )


def _unsupported_known_lineage_limitation(limitations, payload):
    supported = _ordered_supported_source_identities(payload)
    unsupported_phrase = re.compile(
        r"\bnot\s+(?:explicitly\s+)?(?:listed|identified|provided|mentioned)\s+as\s+"
        r"(?:an?\s+)?(?:supported\s+)?(?:source|lineage)|"
        r"\bnot\s+(?:a\s+)?supported\s+(?:source|lineage)",
        re.I,
    )
    return any(
        identity.lower() in limitation.lower() and unsupported_phrase.search(limitation)
        for limitation in limitations
        for identity in supported
    )


def _unsupported_proven_identity_limitation(limitations, payload):
    proven_tokens = set()
    for path in payload.get("deterministic_lineage_paths", []):
        for node in path:
            proven_tokens.update(
                token.upper()
                for token in re.findall(r"\b[A-Z_][A-Z0-9_]*\b", str(node), re.I)
                if token.upper() not in {"MERGE", "INDICATOR"}
            )
    contradiction = re.compile(
        r"\bnot\s+(?:explicitly\s+)?(?:defined|mentioned|provided|listed|identified)\b"
        r".{0,80}\b(?:evidence|lineage|metadata)|"
        r"\b(?:inferred|assumed)\s+from\s+(?:the\s+)?context\b",
        re.I,
    )
    return any(
        contradiction.search(limitation)
        and any(re.search(rf"\b{re.escape(token)}\b", limitation, re.I) for token in proven_tokens)
        for limitation in limitations
    )


def validate_explanation(explanation, payload):
    summary = " ".join(str(explanation.summary).split())
    try:
        sources = _clean_list(explanation.supported_sources, 20)
        steps = [_strip_step_number(step) for step in _clean_list(explanation.implemented_steps, 12)]
        limitations = _clean_list(explanation.limitations, 8)
    except ValueError:
        return _rejected_explanation(
            "The model returned an invalid explanation structure.", explanation, payload
        )
    if not summary or len(summary) > 500 or any(len(item) > 500 for item in steps + limitations):
        return _rejected_explanation(
            "The model returned missing or oversized explanation text.", explanation, payload
        )

    sources = _ordered_unique(sources)
    allowed_sources = _supported_source_identities(payload)
    unsupported_sources = [source for source in sources if source.upper() not in allowed_sources]
    if unsupported_sources:
        return _rejected_explanation(
            "Unsupported source claim(s): " + ", ".join(unsupported_sources),
            explanation,
            payload,
        )

    explanation_text = " ".join([summary, *sources, *steps, *limitations])
    if re.search(r"\bline\s+\d+\b", explanation_text, re.I):
        return _rejected_explanation(
            "The model introduced a line reference that was not provided.", explanation, payload
        )
    identity_issues = _identity_validation_issues(explanation_text, payload)
    if identity_issues:
        return _rejected_explanation(
            "; ".join(identity_issues),
            explanation,
            payload,
        )
    operation_issues = _unsupported_operation_claims(" ".join(steps), payload)
    if operation_issues:
        return _rejected_explanation(
            "; ".join(operation_issues),
            explanation,
            payload,
        )
    missing_nodes = _missing_required_lineage_nodes(summary, steps, payload)
    if missing_nodes:
        return _rejected_explanation(
            "Implemented logic omitted required deterministic lineage node(s): "
            + ", ".join(missing_nodes),
            explanation,
            payload,
        )
    if (
        _unsupported_unknown_source_limitation(limitations, payload)
        or _unsupported_known_lineage_limitation(limitations, payload)
        or _unsupported_proven_identity_limitation(limitations, payload)
    ):
        return _rejected_explanation(
            "The model claimed the source was unknown despite deterministic upstream lineage.",
            explanation,
            payload,
        )
    return DerivationExplanation(
        summary,
        sources,
        _ordered_unique(step for step in steps if step),
        _ordered_unique(limitations),
        accepted=True,
    )


def explain_row(row, explainer):
    updated = dict(row)
    original_status = row.get("status")
    payload = build_explanation_payload(row)
    try:
        raw_explanation = explainer.explain(payload)
        explanation = validate_explanation(raw_explanation, payload)
    except Exception as error:
        _log_explainer_error(error)
        explanation = DerivationExplanation(
            summary="Local explanation generation failed.",
            supported_sources=[],
            implemented_steps=[],
            limitations=[
                "Deterministic lineage and validation status remain unchanged."
            ],
            accepted=False,
            error=_safe_error_message(error),
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
    try:
        with urlopen(request, timeout=timeout) as response:
            response_text = response.read().decode("utf-8")
    except HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        try:
            message = str(json.loads(response_text).get("error", ""))
        except json.JSONDecodeError:
            message = response_text
        message = " ".join(message.split())[:500] or "No Ollama error message was returned."
        raise OllamaHTTPError(error.code, message) from error
    except (TimeoutError, socket.timeout) as error:
        raise OllamaUnavailable("request timed out", type(error).__name__) from error
    except URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise OllamaUnavailable("request timed out", type(reason).__name__) from error
        raise OllamaUnavailable(
            "Ollama is unavailable", f"URL error: {type(reason).__name__}: {reason}"
        ) from error
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as error:
        raise InvalidStructuredResponse("Ollama returned a non-JSON HTTP response.") from error


def _parse_explanation_response(response):
    if not isinstance(response, dict):
        raise InvalidStructuredResponse("Ollama response envelope was not a JSON object.")
    content = response.get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise InvalidStructuredResponse("Ollama response did not contain message.content.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise InvalidStructuredResponse("Ollama message.content was not valid JSON.") from error
    required = {"summary", "supported_sources", "implemented_steps", "limitations"}
    if not isinstance(parsed, dict) or set(parsed) != required:
        raise InvalidStructuredResponse(
            "Ollama JSON did not contain exactly the required explanation fields."
        )
    if not isinstance(parsed["summary"], str) or any(
        not isinstance(parsed[field], list)
        for field in ("supported_sources", "implemented_steps", "limitations")
    ):
        raise InvalidStructuredResponse("Ollama JSON explanation fields had invalid types.")
    return DerivationExplanation(
        summary=parsed["summary"],
        supported_sources=parsed["supported_sources"],
        implemented_steps=parsed["implemented_steps"],
        limitations=parsed["limitations"],
    )


def _supports_json_fallback(error):
    return isinstance(error, InvalidStructuredResponse) or (
        isinstance(error, OllamaHTTPError)
        and error.status in {400, 422}
        and error.user_message == "structured output is unsupported"
    )


def _output_schema_for_payload(payload):
    """Constrain structured source claims to deterministic, non-target identities."""
    schema = deepcopy(OUTPUT_SCHEMA)
    source_schema = schema["properties"]["supported_sources"]
    supported_sources = _preferred_model_sources(payload)
    if supported_sources:
        source_schema["items"]["enum"] = supported_sources
    else:
        source_schema["maxItems"] = 0
    paths = payload.get("deterministic_lineage_paths", [])
    if paths and all(path for path in paths):
        schema["properties"]["limitations"]["maxItems"] = 0
        schema["properties"]["limitations"]["description"] = (
            "Return an empty list because the supplied deterministic paths are complete; do not "
            "invent speculative limitations about proven identities or evidence."
        )
    return schema


def _preferred_model_sources(payload):
    preferred = []
    for path in payload.get("deterministic_lineage_paths", []):
        if path:
            preferred.append(str(path[0]).strip().upper())
        for index, node in enumerate(path):
            if re.fullmatch(r"merge indicator\s+[A-Z_][A-Z0-9_]*", str(node), re.I) and index:
                preferred.append(str(path[index - 1]).strip().upper())
    return _ordered_unique(preferred) or _ordered_supported_source_identities(payload)


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

    def _request_body(self, payload, output_format, correction_feedback=""):
        messages = [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        if correction_feedback:
            messages.append(
                {
                    "role": "system",
                    "content": CORRECTION_INSTRUCTIONS.format(feedback=correction_feedback),
                }
            )
        return {
            "model": self.model,
            "stream": False,
            "format": (
                _output_schema_for_payload(payload)
                if output_format == OUTPUT_SCHEMA
                else output_format
            ),
            "options": {"temperature": 0, "seed": 7},
            "messages": messages,
        }

    def _request_explanation(self, payload, output_format, correction_feedback=""):
        body = self._request_body(
            payload,
            output_format,
            correction_feedback=correction_feedback,
        )
        return _parse_explanation_response(
            self.transport(self.endpoint, body, self.timeout)
        )

    def explain(self, payload):
        try:
            explanation = self._request_explanation(payload, OUTPUT_SCHEMA)
            output_format = OUTPUT_SCHEMA
        except Exception as structured_error:
            if not _supports_json_fallback(structured_error):
                raise
            LOGGER.warning(
                "Ollama structured output failed; retrying JSON mode: type=%s status=%s",
                type(structured_error).__name__,
                getattr(structured_error, "status", "none"),
            )
            try:
                explanation = self._request_explanation(payload, "json")
                output_format = "json"
            except InvalidStructuredResponse as fallback_error:
                raise InvalidStructuredResponse(
                    "Ollama structured-schema and JSON-mode responses were both invalid."
                ) from fallback_error

        source_keys = [str(source).strip().upper() for source in explanation.supported_sources]
        feedback = []
        if len(source_keys) != len(set(source_keys)):
            feedback.append("Remove duplicate supported sources while preserving lineage order.")
        evidence_statements = {
            re.sub(r"\s+", " ", str(statement)).strip().rstrip(";").lower()
            for statement in payload.get("supported_evidence_statements", [])
        }
        raw_statement_steps = [
            step
            for step in explanation.implemented_steps
            if re.sub(r"\s+", " ", _strip_step_number(str(step))).strip().rstrip(";").lower()
            in evidence_statements
        ]
        if raw_statement_steps:
            feedback.append(
                "Paraphrase raw SAS statements into concise implemented-logic steps without numbering."
            )
        validated = validate_explanation(explanation, payload)
        if not validated.accepted:
            feedback.append(validated.limitations[0])
        if not feedback:
            return explanation
        LOGGER.warning(
            "Ollama explanation exceeded deterministic evidence; requesting one bounded correction."
        )
        return self._request_explanation(
            payload,
            output_format,
            correction_feedback=" ".join(feedback),
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
