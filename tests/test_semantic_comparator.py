import json
from types import SimpleNamespace

import pandas as pd

from src.semantic_comparator import (
    SEMANTIC_MATCH,
    SEMANTIC_MISMATCH,
    UNCERTAIN,
    SemanticResult,
    OpenAISemanticComparator,
    apply_semantic_assessments,
    build_semantic_payload,
    comparator_from_environment,
)
from src.spec_checker import compare_to_spec


class MockComparator:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.payloads = []

    def compare(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.result


def review_row():
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
                "statement": "",
                "start_line": None,
                "end_line": None,
            },
        ],
        "spec_origin": "Derived",
        "spec_source": "SDTM.DM.RFXSTDTC",
        "spec_derivation": "Convert RFXSTDTC to numeric SAS treatment start date.",
        "status": "REVIEW REQUIRED",
        "review_note": "Semantic derivation comparison is not deterministic.",
    }


def test_mocked_semantic_match_is_separate_and_does_not_change_status():
    comparator = MockComparator(
        SemanticResult(
            SEMANTIC_MATCH,
            "The specification and program describe the same RFXSTDTC date conversion.",
        )
    )

    result = apply_semantic_assessments([review_row()], comparator)[0]

    assert result["status"] == "REVIEW REQUIRED"
    assert result["semantic_assessment"] == SEMANTIC_MATCH
    assert result["semantic_assisted"] is True
    assert len(comparator.payloads) == 1


def test_payload_contains_only_approved_metadata_without_line_numbers():
    payload = build_semantic_payload(review_row())

    assert set(payload) == {
        "variable_name",
        "deterministic_lineage_path",
        "deterministic_derivation_logic",
        "specification",
        "evidence_statements",
    }
    assert payload["evidence_statements"] == [
        "trtsdt = input(rfxstdtc, yymmdd10.);"
    ]
    assert "start_line" not in str(payload)
    assert "end_line" not in str(payload)


def test_deterministic_source_mismatch_skips_ai_and_remains_mismatch():
    program = [
        {
            **review_row(),
            "status": "",
            "spec_origin": "",
            "spec_source": "",
            "spec_derivation": "",
        }
    ]
    spec = pd.DataFrame(
        [
            {
                "Variable": "TRTSDT",
                "Origin": "Derived",
                "Source": "SDTM.EX.EXSTDTC",
                "Derivation": "Use EXSTDTC as treatment start date.",
            }
        ]
    )
    deterministic = compare_to_spec(program, spec, "ADSL")
    comparator = MockComparator(SemanticResult(SEMANTIC_MATCH, "The derivations match."))

    result = apply_semantic_assessments(deterministic, comparator)[0]

    assert result["status"] == "MISMATCH"
    assert result["semantic_assessment"] == ""
    assert result["semantic_assisted"] is False
    assert comparator.payloads == []


def test_deterministic_match_skips_ai():
    row = {**review_row(), "status": "MATCH"}
    comparator = MockComparator(SemanticResult(SEMANTIC_MATCH, "The derivations match."))

    result = apply_semantic_assessments([row], comparator)[0]

    assert result["status"] == "MATCH"
    assert result["semantic_assessment"] == ""
    assert result["semantic_assisted"] is False
    assert comparator.payloads == []


def test_mocked_semantic_mismatch_is_advisory():
    comparator = MockComparator(
        SemanticResult(
            SEMANTIC_MISMATCH,
            "The specification describes conversion while the program describes truncation.",
        )
    )

    result = apply_semantic_assessments([review_row()], comparator)[0]

    assert result["status"] == "REVIEW REQUIRED"
    assert result["semantic_assessment"] == SEMANTIC_MISMATCH


def test_mocked_uncertain_result_is_preserved():
    comparator = MockComparator(
        SemanticResult(UNCERTAIN, "The supplied derivation evidence is insufficient.")
    )

    result = apply_semantic_assessments([review_row()], comparator)[0]

    assert result["semantic_assessment"] == UNCERTAIN


def test_ai_rationale_with_invented_source_is_rejected():
    comparator = MockComparator(
        SemanticResult(
            SEMANTIC_MATCH,
            "The program uses SDTM.EX.EXSTDTC and therefore matches.",
        )
    )

    result = apply_semantic_assessments([review_row()], comparator)[0]

    assert result["semantic_assessment"] == UNCERTAIN
    assert "outside the approved input" in result["semantic_rationale"]


def test_ai_failure_becomes_uncertain_without_changing_deterministic_status():
    comparator = MockComparator(error=RuntimeError("network unavailable"))

    result = apply_semantic_assessments([review_row()], comparator)[0]

    assert result["status"] == "REVIEW REQUIRED"
    assert result["semantic_assessment"] == UNCERTAIN
    assert "deterministic status is unchanged" in result["semantic_rationale"]


def test_ai_is_disabled_without_api_configuration():
    comparator, reason = comparator_from_environment({})

    assert comparator is None
    assert "OPENAI_API_KEY" in reason


def test_review_without_derivation_text_is_not_sent_to_ai():
    row = {**review_row(), "spec_derivation": ""}
    comparator = MockComparator(SemanticResult(SEMANTIC_MATCH, "The derivations match."))

    result = apply_semantic_assessments([row], comparator)[0]

    assert result["semantic_assessment"] == ""
    assert result["semantic_assisted"] is False
    assert comparator.payloads == []


def test_openai_adapter_uses_structured_response_with_whitelisted_payload():
    class FakeResponses:
        def __init__(self):
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            parsed = SimpleNamespace(
                assessment=SEMANTIC_MATCH,
                rationale="The supplied derivations are semantically equivalent.",
            )
            return SimpleNamespace(output_parsed=parsed)

    responses = FakeResponses()
    client = SimpleNamespace(responses=responses)
    comparator = OpenAISemanticComparator(
        api_key="not-used-by-fake-client",
        model="test-model",
        client=client,
    )
    row = {
        **review_row(),
        "filename": "PRIVATE_FILENAME_SENTINEL.sas",
        "raw_program": "RAW_PROGRAM_SENTINEL",
        "dataset_contents": "PATIENT_RECORD_SENTINEL",
        "spec_workbook": "WHOLE_WORKBOOK_SENTINEL",
        "evidence": [
            *review_row()["evidence"],
            {
                "supported": False,
                "statement": "UNSUPPORTED_EVIDENCE_SENTINEL;",
                "start_line": 987654,
                "end_line": 987654,
            },
        ],
    }
    payload = build_semantic_payload(row)

    result = comparator.compare(payload)

    assert result.assessment == SEMANTIC_MATCH
    assert responses.kwargs["model"] == "test-model"
    assert responses.kwargs["store"] is False
    assert responses.kwargs["input"][1]["content"] == json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    )
    assert responses.kwargs["text_format"] is not None
    serialized_request = json.dumps(responses.kwargs["input"])
    assert "trtsdt = input(rfxstdtc, yymmdd10.);" in serialized_request
    assert "PRIVATE_FILENAME_SENTINEL" not in serialized_request
    assert "RAW_PROGRAM_SENTINEL" not in serialized_request
    assert "PATIENT_RECORD_SENTINEL" not in serialized_request
    assert "WHOLE_WORKBOOK_SENTINEL" not in serialized_request
    assert "UNSUPPORTED_EVIDENCE_SENTINEL" not in serialized_request
    assert "987654" not in serialized_request
