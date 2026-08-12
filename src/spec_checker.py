
import re
import pandas as pd

def load_spec(uploaded_file):
    df = pd.read_excel(uploaded_file)
    df.columns = [str(c).strip() for c in df.columns]
    return df

def tokens(text):
    if text is None:
        return set()
    text = str(text).upper()
    raw = set(re.findall(r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)*\b", text))
    out = set(raw)
    for t in raw:
        out.update(t.split("."))
    return out

def compare_to_spec(lineage_rows, spec_df):
    spec_map = {
        str(r["Variable"]).strip().upper(): r
        for _, r in spec_df.iterrows()
        if pd.notna(r.get("Variable"))
    }
    results = []
    for row in lineage_rows:
        var = row["variable"].upper()
        spec = spec_map.get(var)
        if spec is None:
            results.append({**row, "status": "REVIEW REQUIRED",
                            "review_note": "Variable not found in uploaded specification."})
            continue

        pc = str(row.get("classification","")).lower()
        so = str(spec.get("Origin","")).lower()
        class_match = (
            (pc == "assigned" and "assign" in so) or
            (pc in {"derived","constant"} and "deriv" in so)
        )

        spec_source = str(spec.get("Source","") or "").upper().strip()
        prog_text = (str(row.get("immediate_source","")) + " " +
                     str(row.get("ultimate_source",""))).upper()

        st = tokens(spec_source)
        pt = tokens(prog_text)
        meaningful = {x for x in st if x not in {"SDTM","ADAM","RAW"}}
        source_match = (not spec_source) or bool(meaningful & pt)

        if class_match and source_match:
            status = "MATCH"
            note = "Origin and source are consistent with the implemented lineage."
        elif not class_match:
            status = "MISMATCH"
            note = f"Program classification '{row.get('classification')}' differs from spec origin '{spec.get('Origin','')}'."
        else:
            status = "MISMATCH"
            note = f"Program lineage '{row.get('ultimate_source') or row.get('immediate_source')}' does not match documented source '{spec_source}'."

        results.append({
            **row,
            "spec_origin": spec.get("Origin",""),
            "spec_source": spec.get("Source",""),
            "spec_derivation": spec.get("Derivation",""),
            "status": status,
            "review_note": note
        })
    return results
