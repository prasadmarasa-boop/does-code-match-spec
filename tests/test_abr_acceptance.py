from pathlib import Path

from src.sas_parser import analyze_sas, parse_steps


ROOT = Path(__file__).resolve().parents[1]
ABR_PROGRAM = ROOT / "sample" / "sample_abr.sas"


def abr_result():
    return analyze_sas(ABR_PROGRAM.read_text(encoding="utf-8"))


def variable_map(result):
    return {row["variable"]: row for row in result["variables"]}


def supported_evidence(row, kind=None):
    return [
        item
        for item in row["evidence"]
        if item["supported"] and (kind is None or item["kind"] == kind)
    ]


def test_synthetic_abr_dataset_lineage_summary():
    result = abr_result()

    assert result["final_dataset"] == "ABR_ANALYSIS"
    assert result["sources"] == ["SUBJECT_FOLLOWUP_SORTED", "BLEED_COUNTS_SORTED"]
    assert result["upstream_sources"] == [
        "SYNTHETIC.BLEEDING_EVENTS",
        "SYNTHETIC.SUBJECT_LEVEL",
    ]
    assert result["variables_complete"] is True
    assert result["warnings"] == []


def test_qualifying_event_filter_and_intermediate_dataset_are_parsed():
    code = ABR_PROGRAM.read_text(encoding="utf-8")
    steps, _ = parse_steps(code)
    qualifying = steps["QUALIFYING_BLEEDS"]

    assert qualifying["inputs"] == ["SYNTHETIC.BLEEDING_EVENTS"]
    assert qualifying["keep"] == ["EVENTDT", "EVENT_ID", "USUBJID"]
    filters = qualifying["transformation_evidence"]
    assert len(filters) == 1
    assert filters[0]["kind"] == "subsetting_if"
    assert (filters[0]["start_line"], filters[0]["end_line"]) == (6, 8)
    assert 'bleedfl = "Y"' in filters[0]["statement"]
    assert "not missing(eventdtc)" in filters[0]["statement"]
    assert 'exclusionfl ne "Y"' in filters[0]["statement"]


def test_proc_sql_count_and_grouping_have_deterministic_lineage():
    code = ABR_PROGRAM.read_text(encoding="utf-8")
    steps, _ = parse_steps(code)
    count_step = steps["BLEED_COUNTS"]
    records = count_step["assignments"]["N_QUAL_BLEEDS"]

    assert count_step["kind"] == "proc_sql"
    assert count_step["inputs"] == ["QUALIFYING_BLEEDS"]
    assert count_step["keep"] == ["USUBJID", "N_QUAL_BLEEDS"]
    assert records[0][0] == "sql_aggregate"
    assert records[0][2].lower() == "count(event_id)"
    assert records[0][3] == ["EVENT_ID"]
    assert records[0][4]["start_line"] == 15
    assert any(
        item["kind"] == "sql_group_by"
        and item["start_line"] == 17
        and item["statement"].lower() == "group by usubjid"
        for item in count_step["transformation_evidence"]
    )
    assert any(
        item["kind"] == "sql_from"
        and item["start_line"] == 16
        and item["statement"].lower() == "from qualifying_bleeds"
        for item in count_step["input_evidence"]["QUALIFYING_BLEEDS"]
    )


def test_qualifying_bleed_count_reaches_synthetic_event_source():
    row = variable_map(abr_result())["N_QUAL_BLEEDS"]

    assert row["ultimate_source"] == "SYNTHETIC.BLEEDING_EVENTS.EVENT_ID"
    assert [
        "SYNTHETIC.BLEEDING_EVENTS.EVENT_ID",
        "QUALIFYING_BLEEDS.EVENT_ID",
        "EVENT_ID",
        "BLEED_COUNTS.N_QUAL_BLEEDS",
        "BLEED_COUNTS_SORTED.N_QUAL_BLEEDS",
        "ABR_ANALYSIS.N_QUAL_BLEEDS",
    ] in row["lineage_paths"]
    assert {"subsetting_if", "sql_select", "sql_group_by", "proc_sort", "merge"}.issubset(
        {item["kind"] for item in supported_evidence(row)}
    )


def test_followup_days_preserves_both_date_derivation_branches():
    row = variable_map(abr_result())["FOLLOWUP_DAYS"]

    assert set(row["ultimate_source"].split(" | ")) == {
        "SYNTHETIC.SUBJECT_LEVEL.OBS_ENDDTC",
        "SYNTHETIC.SUBJECT_LEVEL.OBS_STARTDTC",
    }
    assert any(
        item["kind"] == "assignment"
        and item["start_line"] == 26
        and item["statement"] == "followup_days = obs_enddt - obs_startdt + 1;"
        for item in supported_evidence(row)
    )
    assert row["ambiguity_notes"] == []


def test_observation_years_traces_through_followup_days():
    row = variable_map(abr_result())["OBSERVATION_YEARS"]

    assert all("SUBJECT_FOLLOWUP.FOLLOWUP_DAYS" in path for path in row["lineage_paths"])
    assert all("SUBJECT_FOLLOWUP.OBSERVATION_YEARS" in path for path in row["lineage_paths"])
    assert any(
        item["kind"] == "assignment"
        and item["start_line"] == 27
        and item["statement"] == "observation_years = followup_days / 365.25;"
        for item in supported_evidence(row)
    )
    assert row["ambiguity_notes"] == []


def test_final_abr_formula_combines_proven_numerator_and_denominator():
    row = variable_map(abr_result())["ABR"]

    assert row["classification"] == "Derived"
    assert set(row["immediate_source"].split(", ")) == {
        "N_QUAL_BLEEDS",
        "OBSERVATION_YEARS",
    }
    assert set(row["ultimate_source"].split(" | ")) == {
        "SYNTHETIC.BLEEDING_EVENTS.EVENT_ID",
        "SYNTHETIC.SUBJECT_LEVEL.OBS_ENDDTC",
        "SYNTHETIC.SUBJECT_LEVEL.OBS_STARTDTC",
    }
    assert any("BLEED_COUNTS.N_QUAL_BLEEDS" in path for path in row["lineage_paths"])
    assert any("SUBJECT_FOLLOWUP.FOLLOWUP_DAYS" in path for path in row["lineage_paths"])
    assert any("SUBJECT_FOLLOWUP.OBSERVATION_YEARS" in path for path in row["lineage_paths"])
    assert any(
        item["kind"] == "if_assignment"
        and item["start_line"] == 46
        and item["statement"]
        == "if observation_years > 0 then abr = n_qual_bleeds / observation_years;"
        for item in supported_evidence(row)
    )
    assert row["ambiguity_notes"] == []


def test_abr_evidence_preserves_every_important_algorithm_step():
    row = variable_map(abr_result())["ABR"]
    evidence = supported_evidence(row)
    kinds = {item["kind"] for item in evidence}
    lines = {item["start_line"] for item in evidence}

    assert {
        "subsetting_if",
        "sql_create_table",
        "sql_from",
        "sql_group_by",
        "sql_select",
        "assignment",
        "proc_sort",
        "merge",
        "if_assignment",
    }.issubset(kinds)
    assert {5, 6, 13, 15, 16, 17, 23, 24, 25, 26, 27, 30, 34, 42, 45, 46}.issubset(
        lines
    )
    assert all(item["statement"] for item in evidence)


def test_unsupported_sql_join_remains_unresolved_and_warns():
    code = """proc sql;
create table ambiguous_count as
select a.usubjid, count(b.event_id) as n_qual_bleeds
from synthetic.subject_level as a
left join synthetic.bleeding_events as b
on a.usubjid = b.usubjid
group by a.usubjid;
quit;
data final(keep=n_qual_bleeds);
set ambiguous_count;
run;
"""

    result = analyze_sas(code)
    row = variable_map(result)["N_QUAL_BLEEDS"]

    assert result["warnings"]
    assert any("joins" in warning.lower() for warning in result["warnings"])
    assert "SYNTHETIC.SUBJECT_LEVEL" not in row["ultimate_source"]
    assert "SYNTHETIC.BLEEDING_EVENTS" not in row["ultimate_source"]
    assert any(not item["supported"] for item in row["evidence"])
    assert all(
        item["start_line"] is None and item["statement"] == ""
        for item in row["evidence"]
        if not item["supported"]
    )
