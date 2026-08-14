import json
from copy import deepcopy

import pytest

from src.local_sas_explainer import (
    DEFAULT_ENDPOINT,
    DerivationExplanation,
    OllamaSASExplainer,
    build_explanation_payload,
    explain_row,
    explainer_from_environment,
    prioritize_explanation_rows,
)


class MockExplainer:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.payloads = []

    def explain(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.result


def variable_row(status="REVIEW REQUIRED"):
    return {
        "variable": "TRTSDT",
        "classification": "Derived",
        "immediate_source": "RFXSTDTC",
        "ultimate_source": "SDTM.DM.RFXSTDTC",
        "derivation_logic": "TRTSDT = input(rfxstdtc, yymmdd10.)",
        "lineage_paths": [["SDTM.DM.RFXSTDTC", "RFXSTDTC", "ADSL.TRTSDT"]],
        "evidence": [
            {
                "supported": True,
                "statement": "trtsdt = input(rfxstdtc, yymmdd10.);",
                "start_line": 36,
                "end_line": 36,
            },
            {
                "supported": False,
                "statement": "unsupported statement;",
                "start_line": 99,
                "end_line": 99,
            },
        ],
        "spec_origin": "Derived",
        "spec_source": "SDTM.DM.RFXSTDTC",
        "spec_derivation": "Convert RFXSTDTC to numeric SAS treatment start date.",
        "status": status,
        "review_note": "Deterministic validation result.",
    }


def supported_explanation():
    return DerivationExplanation(
        summary="TRTSDT converts RFXSTDTC to a numeric SAS date.",
        supported_sources=["SDTM.DM.RFXSTDTC", "RFXSTDTC"],
        implemented_steps=[
            "Read RFXSTDTC from the supported lineage.",
            "Apply INPUT with YYMMDD10 to derive TRTSDT.",
        ],
        limitations=["The evidence does not establish a display format."],
    )


def test_payload_contains_only_explanation_context_and_supported_evidence():
    payload = build_explanation_payload(variable_row())

    assert set(payload) == {
        "variable_name",
        "deterministic_lineage_paths",
        "deterministic_derivation_logic",
        "supported_evidence_statements",
        "optional_specification_context",
    }
    assert payload["supported_evidence_statements"] == [
        "trtsdt = input(rfxstdtc, yymmdd10.);"
    ]
    assert "status" not in str(payload)
    assert "start_line" not in str(payload)
    assert "unsupported statement" not in str(payload)


def test_specification_context_is_omitted_when_blank():
    row = {
        **variable_row(),
        "spec_origin": "",
        "spec_source": "",
        "spec_derivation": "",
    }

    payload = build_explanation_payload(row)

    assert "optional_specification_context" not in payload


def test_review_required_rows_are_prioritized_for_explanation_ui():
    rows = [
        {**variable_row("MATCH"), "variable": "A"},
        {**variable_row("REVIEW REQUIRED"), "variable": "Z"},
        {**variable_row("MISMATCH"), "variable": "B"},
    ]

    prioritized = prioritize_explanation_rows(rows)

    assert [row["status"] for _, row in prioritized] == [
        "REVIEW REQUIRED",
        "MATCH",
        "MISMATCH",
    ]


@pytest.mark.parametrize("status", ["MATCH", "MISMATCH", "REVIEW REQUIRED"])
def test_every_deterministic_classification_can_be_explained_without_status_change(status):
    row = variable_row(status)
    explainer = MockExplainer(supported_explanation())

    result = explain_row(row, explainer)

    assert result["status"] == status
    assert result["local_derivation_explanation"]["accepted"] is True
    assert len(explainer.payloads) == 1


def test_unsupported_source_claim_is_rejected_without_status_change():
    explanation = DerivationExplanation(
        summary="TRTSDT is derived from a treatment date.",
        supported_sources=["SDTM.EX.EXSTDTC"],
        implemented_steps=["Convert the source date to TRTSDT."],
        limitations=[],
    )
    explainer = MockExplainer(explanation)

    result = explain_row(variable_row("MISMATCH"), explainer)
    output = result["local_derivation_explanation"]

    assert result["status"] == "MISMATCH"
    assert output["accepted"] is False
    assert output["supported_sources"] == []
    assert "Unsupported source claim" in output["limitations"][0]


def test_unsupported_lineage_identity_in_steps_is_rejected():
    explanation = DerivationExplanation(
        summary="TRTSDT is a numeric SAS date.",
        supported_sources=["RFXSTDTC"],
        implemented_steps=["Read sdtm.ex.exstdtc and derive TRTSDT."],
        limitations=[],
    )

    result = explain_row(variable_row(), MockExplainer(explanation))

    assert result["local_derivation_explanation"]["accepted"] is False
    assert "Unsupported lineage identity" in result["local_derivation_explanation"]["limitations"][0]


def test_specification_source_alone_cannot_support_an_implemented_source_claim():
    row = {**variable_row(), "spec_source": "SDTM.EX.EXSTDTC"}
    explanation = DerivationExplanation(
        summary="TRTSDT is a numeric SAS date.",
        supported_sources=["RFXSTDTC"],
        implemented_steps=["Read SDTM.EX.EXSTDTC and derive TRTSDT."],
        limitations=[],
    )

    result = explain_row(row, MockExplainer(explanation))

    assert result["status"] == "REVIEW REQUIRED"
    assert result["local_derivation_explanation"]["accepted"] is False
    assert "Unsupported lineage identity" in result["local_derivation_explanation"]["limitations"][0]


def test_deterministic_evidence_is_preserved_after_explanation():
    row = variable_row()
    original_evidence = deepcopy(row["evidence"])

    result = explain_row(row, MockExplainer(supported_explanation()))

    assert row["evidence"] == original_evidence
    assert result["evidence"] == original_evidence
    assert result["local_derivation_explanation"]["implemented_steps"] == [
        "Read RFXSTDTC from the supported lineage.",
        "Apply INPUT with YYMMDD10 to derive TRTSDT.",
    ]


def test_local_runtime_failure_does_not_change_status():
    row = variable_row("MATCH")
    explainer = MockExplainer(error=ConnectionError("Ollama is not running"))

    result = explain_row(row, explainer)

    assert result["status"] == "MATCH"
    assert result["local_derivation_explanation"]["accepted"] is False
    assert "deterministic" in result["local_derivation_explanation"]["limitations"][0].lower()


def test_local_explainer_is_off_by_default():
    explainer, reason = explainer_from_environment({})

    assert explainer is None
    assert "LOCAL_SAS_EXPLAINER_ENABLED=1" in reason


def test_environment_enables_configured_local_explainer():
    explainer, reason = explainer_from_environment(
        {
            "LOCAL_SAS_EXPLAINER_ENABLED": "true",
            "LOCAL_SAS_EXPLAINER_MODEL": "sas-demo:test",
            "LOCAL_SAS_EXPLAINER_URL": "http://localhost:11434/api/chat",
        }
    )

    assert reason == ""
    assert explainer.model == "sas-demo:test"
    assert explainer.endpoint == "http://localhost:11434/api/chat"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434/api/chat",
        "http://example.com:11434/api/chat",
        "http://127.0.0.1:11434/api/generate",
        "http://user:secret@127.0.0.1:11434/api/chat",
    ],
)
def test_explainer_rejects_non_loopback_or_unexpected_endpoints(endpoint):
    with pytest.raises(ValueError, match="loopback"):
        OllamaSASExplainer(endpoint=endpoint)


def test_ollama_explanation_request_is_structured_and_private():
    captured = {}

    def fake_transport(endpoint, body, timeout):
        captured.update(endpoint=endpoint, body=body, timeout=timeout)
        return {
            "message": {
                "content": json.dumps(
                    {
                        "summary": "TRTSDT converts RFXSTDTC to a numeric SAS date.",
                        "supported_sources": ["RFXSTDTC"],
                        "implemented_steps": ["Apply INPUT to RFXSTDTC."],
                        "limitations": [],
                    }
                )
            }
        }

    row = {
        **variable_row(),
        "filename": "PRIVATE_FILENAME_SENTINEL.sas",
        "raw_program": "RAW_PROGRAM_SENTINEL",
        "dataset_contents": "PATIENT_RECORD_SENTINEL",
        "spec_workbook": "WHOLE_WORKBOOK_SENTINEL",
    }
    payload = build_explanation_payload(row)
    explainer = OllamaSASExplainer(transport=fake_transport)

    result = explainer.explain(payload)

    assert result.summary.startswith("TRTSDT")
    assert captured["endpoint"] == DEFAULT_ENDPOINT
    assert captured["body"]["stream"] is False
    assert captured["body"]["format"]["additionalProperties"] is False
    assert captured["body"]["options"] == {"temperature": 0, "seed": 7}
    user_content = captured["body"]["messages"][1]["content"]
    assert json.loads(user_content) == payload
    assert "trtsdt = input(rfxstdtc, yymmdd10.);" in user_content
    assert "PRIVATE_FILENAME_SENTINEL" not in user_content
    assert "RAW_PROGRAM_SENTINEL" not in user_content
    assert "PATIENT_RECORD_SENTINEL" not in user_content
    assert "WHOLE_WORKBOOK_SENTINEL" not in user_content
    assert "unsupported statement" not in user_content
    assert "\"start_line\"" not in user_content
