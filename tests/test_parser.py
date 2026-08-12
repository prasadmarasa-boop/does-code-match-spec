from src.sas_parser import analyze_sas


def test_simple_data_step():
    code = """
    data adsl;
      set sdtm.dm;
      trtsdt = input(rfxstdtc, yymmdd10.);
      keep usubjid trtsdt;
    run;
    """
    result = analyze_sas(code)
    assert result["final_dataset"] == "ADSL"
    assert any(v["variable"] == "TRTSDT" for v in result["variables"])
