from pathlib import Path

import pandas as pd

from src.sas_parser import analyze_sas
from src.spec_checker import compare_to_spec


ROOT = Path(__file__).resolve().parents[1]


def lineage_row(
    variable="USUBJID",
    classification="Assigned",
    immediate_source="USUBJID",
    ultimate_source="SDTM.DM.USUBJID",
):
    return {
        "variable": variable,
        "classification": classification,
        "immediate_source": immediate_source,
        "ultimate_source": ultimate_source,
        "derivation_logic": "Direct carry-forward",
        "confidence": "High",
    }


def result_map(results):
    return {row["variable"]: row for row in results}


def test_missing_specification_variable_is_mismatch():
    spec = pd.DataFrame(
        [
            {"Variable": "USUBJID", "Origin": "Assigned", "Source": "SDTM.DM.USUBJID"},
            {"Variable": "SAFFL", "Origin": "Derived", "Source": "SDTM.EX.USUBJID"},
        ]
    )

    results = result_map(compare_to_spec([lineage_row()], spec, "ADSL"))

    assert results["USUBJID"]["status"] == "MATCH"
    assert results["SAFFL"]["status"] == "MISMATCH"
    assert "missing from the final program output" in results["SAFFL"]["review_note"]


def test_same_variable_from_different_dataset_is_mismatch():
    spec = pd.DataFrame(
        [{"Variable": "USUBJID", "Origin": "Assigned", "Source": "SDTM.DM.USUBJID"}]
    )
    program = [lineage_row(ultimate_source="SDTM.EX.USUBJID")]

    result = compare_to_spec(program, spec, "ADSL")[0]

    assert result["status"] == "MISMATCH"


def test_ambiguous_merge_lineage_is_not_match_for_single_spec_source():
    spec = pd.DataFrame(
        [{"Variable": "USUBJID", "Origin": "Assigned", "Source": "SDTM.DM.USUBJID"}]
    )
    program = [
        lineage_row(ultimate_source="SDTM.DM.USUBJID | SDTM.EX.USUBJID")
    ]

    result = compare_to_spec(program, spec, "ADSL")[0]

    assert result["status"] == "REVIEW REQUIRED"
    assert "ambiguous" in result["review_note"].lower()


def test_unqualified_immediate_variable_does_not_invent_final_dataset_identity():
    spec = pd.DataFrame(
        [{"Variable": "AGEGR1", "Origin": "Derived", "Source": "ADSL.AGE"}]
    )
    program = [
        lineage_row(
            variable="AGEGR1",
            classification="Derived",
            immediate_source="AGE",
            ultimate_source="SDTM.DM.AGE | SDTM.EX.AGE",
        )
    ]

    result = compare_to_spec(program, spec, "ADSL")[0]

    assert result["status"] == "REVIEW REQUIRED"


def test_blank_source_cell_is_treated_as_blank_not_nan():
    spec = pd.DataFrame(
        [{"Variable": "USUBJID", "Origin": "Assigned", "Source": float("nan"), "Derivation": pd.NA}]
    )

    result = compare_to_spec([lineage_row()], spec, "ADSL")[0]

    assert result["status"] == "REVIEW REQUIRED"
    assert result["spec_source"] == ""
    assert result["spec_derivation"] == ""


def test_nonblank_derivation_requires_manual_review():
    spec = pd.DataFrame(
        [
            {
                "Variable": "TRTSDT",
                "Origin": "Derived",
                "Source": "SDTM.DM.RFXSTDTC",
                "Derivation": "Convert ISO date to numeric date.",
            }
        ]
    )
    program = [
        lineage_row(
            variable="TRTSDT",
            classification="Derived",
            immediate_source="RFXSTDTC",
            ultimate_source="SDTM.DM.RFXSTDTC",
        )
    ]

    result = compare_to_spec(program, spec, "ADSL")[0]

    assert result["status"] == "REVIEW REQUIRED"
    assert "semantic derivation equivalence is not implemented" in result["review_note"]


def test_program_variable_missing_from_spec_requires_review():
    result = compare_to_spec([lineage_row()], pd.DataFrame(columns=["Variable"]), "ADSL")[0]

    assert result["status"] == "REVIEW REQUIRED"


def test_unknown_variable_completeness_does_not_claim_spec_variable_is_absent():
    spec = pd.DataFrame(
        [
            {"Variable": "USUBJID", "Origin": "Assigned", "Source": "SDTM.DM.USUBJID"},
            {"Variable": "AGE", "Origin": "Assigned", "Source": "SDTM.DM.AGE"},
        ]
    )

    results = result_map(
        compare_to_spec([lineage_row()], spec, "ADSL", variables_complete=False)
    )

    assert results["USUBJID"]["status"] == "MATCH"
    assert results["AGE"]["status"] == "REVIEW REQUIRED"
    assert "cannot be proven absent" in results["AGE"]["review_note"]


def test_dataset_only_tokens_are_not_variable_identities():
    spec = pd.DataFrame(
        [{"Variable": "SAFFL", "Origin": "Derived", "Source": "SDTM.EX"}]
    )
    program = [
        lineage_row(
            variable="SAFFL",
            classification="Derived",
            immediate_source="B",
            ultimate_source="EX_TRT -> SDTM.EX",
        )
    ]

    result = compare_to_spec(program, spec, "ADSL")[0]

    assert result["status"] == "REVIEW REQUIRED"


def test_sample_spec_expected_conservative_results():
    code = (ROOT / "sample" / "sample_adsl.sas").read_text(encoding="utf-8")
    spec = pd.read_excel(ROOT / "sample" / "sample_adsl_spec.xlsx")
    lineage = analyze_sas(code)

    results = result_map(
        compare_to_spec(
            lineage["variables"],
            spec,
            lineage["final_dataset"],
            lineage["variables_complete"],
        )
    )

    assert results["STUDYID"]["status"] == "MATCH"
    assert results["USUBJID"]["status"] == "REVIEW REQUIRED"
    assert results["AGE"]["status"] == "MATCH"
    assert results["SEX"]["status"] == "MATCH"
    assert results["TRTSDT"]["status"] == "REVIEW REQUIRED"
    assert results["AGEGR1"]["status"] == "REVIEW REQUIRED"
    assert results["SAFFL"]["status"] == "MISMATCH"
