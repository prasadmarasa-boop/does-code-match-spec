import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

import src.local_sas_explainer as local_sas_explainer
from src.local_sas_explainer import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    DerivationExplanation,
    OllamaHTTPError,
    OllamaSASExplainer,
    build_explanation_payload,
    cache_explanation,
    explain_row,
    explainer_from_environment,
    prioritize_explanation_rows,
)
from src.sas_parser import analyze_sas


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


def test_unsupported_specification_source_identity_is_not_sent_to_model():
    row = {
        **variable_row(),
        "spec_source": "UNKNOWN.DATA.VAR",
        "spec_derivation": "Copy UNKNOWN.DATA.VAR into TRTSDT.",
    }

    payload = build_explanation_payload(row)

    assert payload["optional_specification_context"]["source"] == ""
    assert payload["optional_specification_context"]["derivation"] == ""
    assert "UNKNOWN.DATA.VAR" not in json.dumps(payload)


def test_trtsdt_payload_is_focused_on_selected_variable_chain():
    payload = build_explanation_payload(sample_variable_row("TRTSDT"))
    evidence_text = " ".join(payload["supported_evidence_statements"]).upper()

    assert "INPUT(RFXSTDTC, YYMMDD10.)" in evidence_text
    assert "EX_TRT" not in evidence_text
    assert "SAFFL" not in evidence_text
    assert "KEEP " not in evidence_text
    assert "PROC SORT" not in evidence_text


def test_trtsdt_accepts_normal_sas_date_vocabulary_and_numbering_is_normalized():
    explanation = DerivationExplanation(
        summary=(
            "TRTSDT converts the ISO character date in SDTM.DM.RFXSTDTC to a SAS numeric "
            "date using the YYMMDD10. informat."
        ),
        supported_sources=["SDTM.DM.RFXSTDTC"],
        implemented_steps=[
            "1. RFXSTDTC is carried forward through the supported lineage.",
            "2) INPUT(RFXSTDTC, YYMMDD10.) performs the date conversion.",
            "3. The numeric result is assigned to TRTSDT.",
        ],
        limitations=[],
    )

    output = explain_row(sample_variable_row("TRTSDT"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is True
    assert output["supported_sources"] == ["SDTM.DM.RFXSTDTC"]
    assert output["implemented_steps"][0].startswith("RFXSTDTC")
    assert output["implemented_steps"][1].startswith("INPUT")


def test_trtsdt_rejects_unrelated_ex_trt_or_saffl_steps_and_invented_identity():
    explanation = DerivationExplanation(
        summary="TRTSDT converts RFXSTDTC to a numeric SAS date.",
        supported_sources=["SDTM.DM.RFXSTDTC"],
        implemented_steps=[
            "Sort EX_TRT and assign SAFFL before deriving TRTSDT.",
            "Read UNKNOWN.DATA.VAR before applying INPUT.",
        ],
        limitations=[],
    )

    output = explain_row(sample_variable_row("TRTSDT"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert all("EX_TRT" not in step and "SAFFL" not in step for step in output["implemented_steps"])
    assert "UNKNOWN.DATA.VAR" in output["limitations"][0]


def test_trtsdt_final_target_cannot_be_used_in_directional_source_phrase():
    explanation = DerivationExplanation(
        summary="TRTSDT converts RFXSTDTC to a SAS date.",
        supported_sources=["SDTM.DM.RFXSTDTC"],
        implemented_steps=["RFXSTDTC from ADSL.TRTSDT is used as the source."],
        limitations=[],
    )

    output = explain_row(sample_variable_row("TRTSDT"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert "Unsupported directional source claim: ADSL.TRTSDT" in output["limitations"][0]


def test_saffl_payload_preserves_only_supported_intermediate_chain_evidence():
    payload = build_explanation_payload(sample_variable_row("SAFFL"))
    evidence_text = " ".join(payload["supported_evidence_statements"]).upper()

    assert payload["deterministic_lineage_paths"] == [
        ["SDTM.EX", "EX", "EX_TRT", "merge indicator B", "ADSL.SAFFL"]
    ]
    assert "SET EX" in evidence_text
    assert "WHERE NOT MISSING(EXSTDTC)" in evidence_text
    assert "EX_TRT(IN=B" in evidence_text
    assert 'IF B THEN SAFFL = "Y"' in evidence_text
    assert 'ELSE SAFFL = "N"' in evidence_text
    assert "IF A;" not in evidence_text
    assert "KEEP " not in evidence_text


def test_saffl_sources_are_deduplicated_in_first_seen_order_with_intermediates():
    explanation = saffl_explanation(
        sources=["SDTM.EX", "sdtm.ex", "EX_TRT", "SDTM.EX", "ex_trt"]
    )

    output = explain_row(sample_variable_row("SAFFL"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is True
    assert output["supported_sources"] == ["SDTM.EX", "EX_TRT"]
    assert output["implemented_steps"] == [
        "SDTM.EX is used to create the intermediate EX dataset.",
        "EX_TRT is derived from EX using supported filtering and selection logic.",
        "ADSL merges DM with EX_TRT by USUBJID and assigns merge indicator B to EX_TRT.",
        'If B is true, SAFFL = "Y".',
        'Otherwise, SAFFL = "N".',
    ]


def test_saffl_raw_final_step_without_upstream_intermediates_is_rejected():
    explanation = DerivationExplanation(
        summary='SAFFL is "Y" when merge indicator B is true and "N" otherwise.',
        supported_sources=["SDTM.EX", "EX_TRT"],
        implemented_steps=[
            "merge dm(in=a) ex_trt(in=b keep=usubjid exstdtc);",
            'if b then saffl = "Y";',
            'else saffl = "N";',
        ],
        limitations=[],
    )

    output = explain_row(sample_variable_row("SAFFL"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert "SDTM.EX" in output["limitations"][0]
    assert "EX" in output["limitations"][0]


def test_intermediate_ex_trt_and_merge_indicator_require_deterministic_support():
    trtsdt_claim = DerivationExplanation(
        summary="TRTSDT uses merge indicator B from EX_TRT.",
        supported_sources=["EX_TRT"],
        implemented_steps=["Merge EX_TRT using merge indicator B."],
        limitations=[],
    )

    trtsdt_output = explain_row(
        sample_variable_row("TRTSDT"), MockExplainer(trtsdt_claim)
    )["local_derivation_explanation"]
    saffl_output = explain_row(
        sample_variable_row("SAFFL"), MockExplainer(saffl_explanation())
    )["local_derivation_explanation"]

    assert trtsdt_output["accepted"] is False
    assert "Unsupported source claim" in trtsdt_output["limitations"][0]
    assert saffl_output["accepted"] is True
    assert "merge indicator B" in saffl_output["summary"]


def test_saffl_known_upstream_source_cannot_be_called_unknown():
    explanation = saffl_explanation(
        limitations=["The logic does not explicitly mention the source of the treatment record."]
    )

    output = explain_row(sample_variable_row("SAFFL"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert "despite deterministic upstream lineage" in output["limitations"][0]


def test_saffl_merge_participants_must_match_supported_merge_evidence():
    explanation = saffl_explanation()
    explanation = DerivationExplanation(
        summary=explanation.summary,
        supported_sources=explanation.supported_sources,
        implemented_steps=[
            "Merge EX_TRT with EX by USUBJID.",
            'If B is true, SAFFL = "Y"; otherwise SAFFL = "N".',
        ],
        limitations=[],
    )

    output = explain_row(sample_variable_row("SAFFL"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert "Unsupported MERGE participant(s): EX" in output["limitations"][0]


def test_agegr1_cannot_claim_age_source_is_unspecified_when_lineage_is_proven():
    explanation = DerivationExplanation(
        summary="AGEGR1 groups AGE into values below 18 and 18 or above.",
        supported_sources=["SDTM.DM.AGE"],
        implemented_steps=[
            'If AGE is below 18, assign AGEGR1 = "<18".',
            'Otherwise assign AGEGR1 = ">=18".',
        ],
        limitations=["The evidence does not specify the exact source of AGE."],
    )

    output = explain_row(sample_variable_row("AGEGR1"), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert "despite deterministic upstream lineage" in output["limitations"][0]


@pytest.mark.parametrize(
    "variable,limitation",
    [
        (
            "SAFFL",
            "Merge indicator B is not explicitly defined in the provided evidence.",
        ),
        (
            "AGEGR1",
            "AGEGR1 is not explicitly mentioned in the evidence and is inferred from context.",
        ),
    ],
)
def test_proven_target_or_indicator_cannot_be_called_unsupported(variable, limitation):
    row = sample_variable_row(variable)
    if variable == "SAFFL":
        explanation = saffl_explanation(limitations=[limitation])
    else:
        explanation = DerivationExplanation(
            summary=f"{variable} follows its deterministic derivation.",
            supported_sources=["SDTM.DM.AGE"],
            implemented_steps=[row["derivation_logic"]],
            limitations=[limitation],
        )

    output = explain_row(row, MockExplainer(explanation))["local_derivation_explanation"]

    assert output["accepted"] is False
    assert "despite deterministic upstream lineage" in output["limitations"][0]


def test_saffl_target_is_never_accepted_as_a_supported_source():
    output = explain_row(
        sample_variable_row("SAFFL"),
        MockExplainer(saffl_explanation(sources=["SDTM.EX", "ADSL.SAFFL"])),
    )["local_derivation_explanation"]

    assert output["accepted"] is False
    assert "ADSL.SAFFL" in output["limitations"][0]
    assert "ADSL.SAFFL" not in output["supported_sources"]


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
    assert output["supported_sources"] == ["SDTM.DM.RFXSTDTC"]
    assert "Unsupported source claim" in output["limitations"][0]


def test_evidence_supported_dataset_prefix_is_allowed_in_explanation():
    explanation = DerivationExplanation(
        summary="TRTSDT is derived from evidence in SDTM.DM.",
        supported_sources=["SDTM.DM", "RFXSTDTC"],
        implemented_steps=["Read RFXSTDTC and apply INPUT to derive TRTSDT."],
        limitations=[],
    )


def sample_variable_row(variable):
    code = Path("sample/sample_adsl.sas").read_text(encoding="utf-8")
    rows = analyze_sas(code)["variables"]
    return next(row for row in rows if row["variable"] == variable)


def saffl_explanation(sources=None, limitations=None):
    return DerivationExplanation(
        summary=(
            'SAFFL is set to "Y" when merge indicator B shows a matching EX_TRT record; '
            'otherwise it is set to "N". EX_TRT is derived upstream from SDTM.EX.'
        ),
        supported_sources=sources or ["SDTM.EX", "EX_TRT"],
        implemented_steps=[
            "1. SDTM.EX is used to create the intermediate EX dataset.",
            "2) EX_TRT is derived from EX using supported filtering and selection logic.",
            "3. ADSL merges DM with EX_TRT by USUBJID and assigns merge indicator B to EX_TRT.",
            '4. If B is true, SAFFL = "Y".',
            '5. Otherwise, SAFFL = "N".',
        ],
        limitations=limitations or [],
    )

    result = explain_row(variable_row(), MockExplainer(explanation))

    assert result["local_derivation_explanation"]["accepted"] is True


def test_final_target_dataset_may_be_described_but_not_claimed_as_a_source():
    explanation = DerivationExplanation(
        summary="TRTSDT is written to the final ADSL dataset.",
        supported_sources=["RFXSTDTC"],
        implemented_steps=["Apply INPUT to RFXSTDTC and create ADSL.TRTSDT."],
        limitations=[],
    )

    result = explain_row(variable_row(), MockExplainer(explanation))

    assert result["local_derivation_explanation"]["accepted"] is True


def test_upstream_variable_cannot_be_attributed_to_final_target_dataset():
    explanation = DerivationExplanation(
        summary="RFXSTDTC variable from the ADSL dataset is converted to TRTSDT.",
        supported_sources=["SDTM.DM.RFXSTDTC"],
        implemented_steps=["Apply INPUT to RFXSTDTC."],
        limitations=[],
    )

    result = explain_row(variable_row(), MockExplainer(explanation))

    output = result["local_derivation_explanation"]
    assert output["accepted"] is False
    assert "Unsupported dataset/variable pairing: ADSL.RFXSTDTC" in output["limitations"][0]
    assert output["supported_sources"] == ["SDTM.DM.RFXSTDTC"]
    assert output["implemented_steps"][-1] == "TRTSDT = input(rfxstdtc, yymmdd10.)"


@pytest.mark.parametrize(
    "summary,unsupported_pair",
    [
        ("The date variable RFXSTDTC from the ADSL dataset is converted.", "ADSL.RFXSTDTC"),
        ("Create the TRTSDT variable in the SDTM.DM dataset.", "SDTM.DM.TRTSDT"),
    ],
)
def test_alternate_prose_cannot_invent_dataset_variable_pairings(summary, unsupported_pair):
    explanation = DerivationExplanation(
        summary=summary,
        supported_sources=["SDTM.DM.RFXSTDTC"],
        implemented_steps=["Apply INPUT to RFXSTDTC."],
        limitations=[],
    )

    output = explain_row(variable_row(), MockExplainer(explanation))[
        "local_derivation_explanation"
    ]

    assert output["accepted"] is False
    assert unsupported_pair in output["limitations"][0]


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


def test_default_model_matches_installed_ollama_profile():
    assert DEFAULT_MODEL == "sas-lineage-assistant:latest"

    explainer, reason = explainer_from_environment({"LOCAL_SAS_EXPLAINER_ENABLED": "1"})

    assert reason == ""
    assert explainer.model == "sas-lineage-assistant:latest"


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
    allowed_sources = captured["body"]["format"]["properties"]["supported_sources"]["items"]["enum"]
    assert "SDTM.DM.RFXSTDTC" in allowed_sources
    assert "RFXSTDTC" not in allowed_sources
    assert "ADSL.TRTSDT" not in allowed_sources
    assert captured["body"]["format"]["properties"]["limitations"]["maxItems"] == 0
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


def test_schema_failure_falls_back_to_json_mode():
    formats = []

    def fake_transport(endpoint, body, timeout):
        formats.append(body["format"])
        if len(formats) == 1:
            raise OllamaHTTPError(400, "invalid format: JSON schema is unsupported")
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

    result = OllamaSASExplainer(transport=fake_transport).explain(
        build_explanation_payload(variable_row())
    )

    assert result.summary.startswith("TRTSDT")
    assert formats[0] != "json"
    assert formats[1] == "json"


def test_unsupported_model_output_gets_one_bounded_correction_without_new_payload_data():
    bodies = []

    def correcting_transport(endpoint, body, timeout):
        bodies.append(body)
        sources = ["ADSL.TRTSDT"] if len(bodies) == 1 else ["RFXSTDTC"]
        return {
            "message": {
                "content": json.dumps(
                    {
                        "summary": "TRTSDT converts RFXSTDTC to a SAS date.",
                        "supported_sources": sources,
                        "implemented_steps": ["Apply INPUT to RFXSTDTC."],
                        "limitations": [],
                    }
                )
            }
        }

    payload = build_explanation_payload(variable_row())
    result = OllamaSASExplainer(transport=correcting_transport).explain(payload)

    assert result.supported_sources == ["RFXSTDTC"]
    assert len(bodies) == 2
    assert json.loads(bodies[1]["messages"][1]["content"]) == payload
    assert "last node" in bodies[1]["messages"][2]["content"]
    assert len(bodies[1]["messages"]) == 3


def test_duplicate_sources_trigger_one_bounded_correction_with_safe_feedback():
    bodies = []

    def correcting_transport(endpoint, body, timeout):
        bodies.append(body)
        sources = ["SDTM.DM.RFXSTDTC", "sdtm.dm.rfxstdtc"] if len(bodies) == 1 else [
            "SDTM.DM.RFXSTDTC"
        ]
        return {
            "message": {
                "content": json.dumps(
                    {
                        "summary": "TRTSDT converts an ISO character date to a numeric SAS date.",
                        "supported_sources": sources,
                        "implemented_steps": ["INPUT with YYMMDD10. converts RFXSTDTC."],
                        "limitations": [],
                    }
                )
            }
        }

    result = OllamaSASExplainer(transport=correcting_transport).explain(
        build_explanation_payload(variable_row())
    )

    assert result.supported_sources == ["SDTM.DM.RFXSTDTC"]
    assert len(bodies) == 2
    feedback = bodies[1]["messages"][2]["content"]
    assert "Remove duplicate supported sources" in feedback
    assert "trtsdt = input" not in feedback.lower()


def test_model_not_found_http_response_is_visible_and_safely_logged(monkeypatch, caplog):
    def missing_model(*args, **kwargs):
        raise HTTPError(
            DEFAULT_ENDPOINT,
            404,
            "Not Found",
            {},
            BytesIO(b'{"error":"model \'missing-model\' not found"}'),
        )

    monkeypatch.setattr(local_sas_explainer, "urlopen", missing_model)

    result = explain_row(variable_row("MISMATCH"), OllamaSASExplainer(model="missing-model"))
    explanation = result["local_derivation_explanation"]

    assert result["status"] == "MISMATCH"
    assert explanation["error"] == "model not found"
    assert "type=OllamaHTTPError" in caplog.text
    assert "status=404" in caplog.text
    assert "model 'missing-model' not found" in caplog.text
    assert "trtsdt = input" not in caplog.text.lower()


def test_malformed_json_retries_once_then_reports_invalid_structured_response():
    formats = []

    def malformed_transport(endpoint, body, timeout):
        formats.append(body["format"])
        return {"message": {"content": "not valid json"}}

    result = explain_row(
        variable_row("REVIEW REQUIRED"),
        OllamaSASExplainer(transport=malformed_transport),
    )
    explanation = result["local_derivation_explanation"]

    assert result["status"] == "REVIEW REQUIRED"
    assert explanation["error"] == "invalid structured response"
    assert len(formats) == 2
    assert formats[1] == "json"


def test_timeout_is_visible_and_does_not_retry_or_change_status():
    calls = []

    def timeout_transport(endpoint, body, timeout):
        calls.append(body["format"])
        raise TimeoutError("synthetic timeout")

    result = explain_row(
        variable_row("MATCH"),
        OllamaSASExplainer(transport=timeout_transport),
    )

    assert result["status"] == "MATCH"
    assert result["local_derivation_explanation"]["error"] == "request timed out"
    assert len(calls) == 1


def test_explanation_cache_uses_mapping_assignment_and_survives_rerun_style_access():
    state = {"local_derivation_explanations": {"existing": {"summary": "old"}}}
    explanation = supported_explanation().to_dict()

    first_cache = cache_explanation(state, "1:0:TRTSDT", explanation)
    next_render_cache = state["local_derivation_explanations"]

    assert first_cache["existing"]["summary"] == "old"
    assert next_render_cache["1:0:TRTSDT"] == explanation
