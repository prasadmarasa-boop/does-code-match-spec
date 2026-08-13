from pathlib import Path

from src.sas_parser import analyze_sas


ROOT = Path(__file__).resolve().parents[1]


def variable_map(result):
    return {row["variable"]: row for row in result["variables"]}


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


def test_sample_adsl_expected_lineage():
    code = (ROOT / "sample" / "sample_adsl.sas").read_text(encoding="utf-8")
    result = analyze_sas(code)
    rows = variable_map(result)

    assert result["final_dataset"] == "ADSL"
    assert set(rows) == {"STUDYID", "USUBJID", "AGE", "SEX", "TRTSDT", "AGEGR1", "SAFFL"}
    assert rows["STUDYID"]["ultimate_source"] == "SDTM.DM.STUDYID"
    assert "SDTM.DM.USUBJID" in rows["USUBJID"]["ultimate_source"]
    assert rows["AGE"]["ultimate_source"] == "SDTM.DM.AGE"
    assert rows["SEX"]["ultimate_source"] == "SDTM.DM.SEX"
    assert rows["TRTSDT"]["ultimate_source"] == "SDTM.DM.RFXSTDTC"
    assert rows["AGEGR1"]["ultimate_source"] == "SDTM.DM.AGE"
    assert rows["SAFFL"]["classification"] == "Derived"
    assert "SDTM.EX" in rows["SAFFL"]["ultimate_source"]
