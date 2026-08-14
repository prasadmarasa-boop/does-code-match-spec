from pathlib import Path

from src.sas_parser import analyze_sas


ROOT = Path(__file__).resolve().parents[1]


def variable_map(result):
    return {row["variable"]: row for row in result["variables"]}


def supported_evidence(row, kind=None):
    return [
        item
        for item in row["evidence"]
        if item["supported"] and (kind is None or item["kind"] == kind)
    ]


def test_simple_data_step():
    code = """
    data adsl;
      set sdtm.dm;
      trtsdt = input(rfxstdtc, yymmdd10.);
      keep usubjid trtsdt;
    run;
    """
    result = analyze_sas(code)
    variables = variable_map(result)

    assert result["final_dataset"] == "ADSL"
    assert set(variables) == {"USUBJID", "TRTSDT"}
    assert variables["TRTSDT"]["ultimate_source"] == "SDTM.DM.RFXSTDTC"


def test_data_step_output_options_control_final_variables():
    code = """
    data adsl(keep=usubjid age);
      set sdtm.dm;
      age = age;
      ignored = 1;
    run;
    """
    result = analyze_sas(code)

    assert result["final_dataset"] == "ADSL"
    assert set(variable_map(result)) == {"USUBJID", "AGE"}


def test_proc_sort_preserves_upstream_lineage():
    code = """
    proc sort data=sdtm.dm out=dm;
      by usubjid;
    run;
    data adsl;
      set dm;
      studyid = studyid;
      keep studyid;
    run;
    """
    studyid = variable_map(analyze_sas(code))["STUDYID"]

    assert studyid["ultimate_source"] == "SDTM.DM.STUDYID"


def test_final_proc_sort_is_pass_through_not_assignment_text():
    code = """
    data adsl;
      set sdtm.dm;
      keep usubjid age;
    run;
    proc sort data=adsl out=sorted;
      by usubjid;
    run;
    """
    result = analyze_sas(code)
    rows = variable_map(result)

    assert result["final_dataset"] == "SORTED"
    assert result["variables_complete"] is True
    assert set(rows) == {"USUBJID", "AGE"}
    assert rows["USUBJID"]["ultimate_source"] == "SDTM.DM.USUBJID"
    assert "DATA" not in rows


def test_direct_assignment_is_assigned_and_traced():
    code = """
    data final;
      set raw.dm;
      usubjid = usubjid;
    run;
    """
    row = variable_map(analyze_sas(code))["USUBJID"]

    assert row["classification"] == "Assigned"
    assert row["derivation_logic"] == "Direct carry-forward"
    assert row["ultimate_source"] == "RAW.DM.USUBJID"


def test_direct_assignment_has_exact_statement_evidence():
    code = """data final;
set raw.dm;
usubjid = usubjid;
keep usubjid;
run;
"""
    row = variable_map(analyze_sas(code))["USUBJID"]
    assignment = supported_evidence(row, "assignment")

    assert any(item["start_line"] == 3 for item in assignment)
    assert any(item["statement"] == "usubjid = usubjid;" for item in assignment)


def test_derived_variable_tracks_rhs_source():
    code = """
    data final;
      set raw.dm;
      age2 = age + 1;
    run;
    """
    row = variable_map(analyze_sas(code))["AGE2"]

    assert row["classification"] == "Derived"
    assert row["immediate_source"] == "AGE"
    assert row["ultimate_source"] == "RAW.DM.AGE"


def test_derived_variable_has_assignment_and_source_evidence():
    code = """data final;
set raw.dm;
age2 = age + 1;
keep age2;
run;
"""
    row = variable_map(analyze_sas(code))["AGE2"]

    assert row["lineage_paths"] == [["RAW.DM.AGE", "AGE", "FINAL.AGE2"]]
    assert any(
        item["kind"] == "assignment"
        and item["start_line"] == 3
        and item["relationship"] == "AGE -> FINAL.AGE2"
        for item in row["evidence"]
    )


def test_if_then_else_derivation_collects_condition_source_and_logic():
    code = """
    data final;
      set raw.dm;
      if age < 18 then agegr1 = '<18';
      else agegr1 = '>=18';
      keep agegr1;
    run;
    """
    row = variable_map(analyze_sas(code))["AGEGR1"]

    assert row["classification"] == "Derived"
    assert row["immediate_source"] == "AGE"
    assert row["ultimate_source"] == "RAW.DM.AGE"
    assert "IF age < 18 THEN AGEGR1 = '<18'" in row["derivation_logic"]
    assert "ELSE AGEGR1 = '>=18'" in row["derivation_logic"]


def test_if_then_else_has_both_statement_lines_as_evidence():
    code = """data final;
set raw.dm;
if age < 18 then agegr1 = '<18';
else agegr1 = '>=18';
keep agegr1;
run;
"""
    row = variable_map(analyze_sas(code))["AGEGR1"]

    assert any(item["kind"] == "if_assignment" and item["start_line"] == 3 for item in row["evidence"])
    assert any(item["kind"] == "else_assignment" and item["start_line"] == 4 for item in row["evidence"])


def test_multi_step_lineage_reaches_terminal_source():
    code = """
    data intermediate;
      set sdtm.dm;
      trtsdt = input(rfxstdtc, yymmdd10.);
      keep trtsdt;
    run;
    data final;
      set intermediate;
      trtsdt = trtsdt;
      keep trtsdt;
    run;
    """
    row = variable_map(analyze_sas(code))["TRTSDT"]

    assert row["ultimate_source"] == "SDTM.DM.RFXSTDTC"


def test_multi_step_lineage_preserves_evidence_for_each_hop():
    code = """data intermediate;
set sdtm.dm;
trtsdt = input(rfxstdtc, yymmdd10.);
keep trtsdt;
run;
data final;
set intermediate;
trtsdt = trtsdt;
keep trtsdt;
run;
"""
    row = variable_map(analyze_sas(code))["TRTSDT"]
    relationships = {item["relationship"] for item in supported_evidence(row)}

    assert ["SDTM.DM.RFXSTDTC", "RFXSTDTC", "INTERMEDIATE.TRTSDT", "FINAL.TRTSDT"] in row["lineage_paths"]
    assert "RFXSTDTC -> INTERMEDIATE.TRTSDT" in relationships
    assert "INTERMEDIATE.TRTSDT -> FINAL.TRTSDT" in relationships


def test_data_step_without_keep_has_unknown_completeness_but_resolves_observed_variable():
    code = """
    data adsl;
      set sdtm.dm;
      by usubjid;
    run;
    """
    result = analyze_sas(code)

    assert result["variables_complete"] is False
    assert variable_map(result)["USUBJID"]["ultimate_source"] == "SDTM.DM.USUBJID"
    assert result["warnings"]


def test_sample_adsl_expected_lineage():
    code = (ROOT / "sample" / "sample_adsl.sas").read_text(encoding="utf-8")
    result = analyze_sas(code)
    rows = variable_map(result)

    assert result["final_dataset"] == "ADSL"
    assert set(rows) == {"STUDYID", "USUBJID", "AGE", "SEX", "TRTSDT", "AGEGR1", "SAFFL"}
    assert rows["STUDYID"]["ultimate_source"] == "SDTM.DM.STUDYID"
    assert rows["USUBJID"]["ultimate_source"] == "SDTM.DM.USUBJID | SDTM.EX.USUBJID"
    assert rows["AGE"]["ultimate_source"] == "SDTM.DM.AGE"
    assert rows["SEX"]["ultimate_source"] == "SDTM.DM.SEX"
    assert rows["TRTSDT"]["ultimate_source"] == "SDTM.DM.RFXSTDTC"
    assert rows["AGEGR1"]["ultimate_source"] == "SDTM.DM.AGE"
    assert rows["SAFFL"]["classification"] == "Derived"
    assert "SDTM.EX" in rows["SAFFL"]["ultimate_source"]


def test_exact_line_capture_survives_multiline_comments():
    code = """/* comment
   still comment */
data final;
set raw.dm;
age2 = age + 1;
keep age2;
run;
"""
    row = variable_map(analyze_sas(code))["AGE2"]

    assert any(
        item["kind"] == "assignment"
        and item["start_line"] == 5
        and item["end_line"] == 5
        for item in row["evidence"]
    )


def test_proc_sort_pass_through_has_exact_evidence():
    code = """proc sort data=sdtm.dm out=dm;
by usubjid;
run;
data final;
set dm;
keep usubjid;
run;
"""
    row = variable_map(analyze_sas(code))["USUBJID"]

    assert row["lineage_paths"] == [["SDTM.DM.USUBJID", "DM.USUBJID", "FINAL.USUBJID"]]
    assert any(
        item["kind"] == "proc_sort"
        and item["start_line"] == 1
        and item["relationship"] == "SDTM.DM.USUBJID -> DM.USUBJID"
        for item in row["evidence"]
    )


def test_sample_saffl_has_evidence_for_every_dataset_and_indicator_hop():
    code = (ROOT / "sample" / "sample_adsl.sas").read_text(encoding="utf-8")
    row = variable_map(analyze_sas(code))["SAFFL"]
    path = ["SDTM.EX", "EX", "EX_TRT", "merge indicator B", "ADSL.SAFFL"]
    relationships = {item["relationship"] for item in supported_evidence(row)}

    assert path in row["lineage_paths"]
    assert "SDTM.EX -> EX" in relationships
    assert "EX -> EX_TRT" in relationships
    assert "EX_TRT -> merge indicator B" in relationships
    assert "merge indicator B -> ADSL.SAFFL" in relationships
    assert {11, 16, 17, 18, 19, 24, 45}.issubset(
        {item["start_line"] for item in supported_evidence(row)}
    )
    assert {"where", "by", "subsetting_if"}.issubset(
        {item["kind"] for item in supported_evidence(row)}
    )


def test_sample_upstream_dataset_summary_is_resolved_not_direct_only():
    code = (ROOT / "sample" / "sample_adsl.sas").read_text(encoding="utf-8")
    result = analyze_sas(code)

    assert result["sources"] == ["DM", "EX_TRT"]
    assert result["upstream_sources"] == ["SDTM.DM", "SDTM.EX"]


def test_unsupported_source_has_no_fabricated_line_number():
    code = """data final;
x = missing_source + 1;
keep x;
run;
"""
    row = variable_map(analyze_sas(code))["X"]
    unsupported = [item for item in row["evidence"] if not item["supported"]]

    assert unsupported
    assert all(item["start_line"] is None and item["statement"] == "" for item in unsupported)
    assert any("No supported SET or MERGE input" in item["note"] for item in unsupported)
