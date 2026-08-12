import pandas as pd


def load_spec(uploaded_file):
    df = pd.read_excel(uploaded_file)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def compare_to_spec(lineage_rows, spec_df):
    spec_map = {
        str(row["Variable"]).strip().upper(): row
        for _, row in spec_df.iterrows()
        if pd.notna(row.get("Variable"))
    }

    results = []

    for row in lineage_rows:
        var = row["variable"].upper()
        spec = spec_map.get(var)

        if spec is None:
            results.append({
                **row,
                "status": "REVIEW REQUIRED",
                "review_note": "Variable not found in uploaded specification."
            })
            continue

        program_class = row.get("classification", "").strip().lower()
        spec_origin = str(spec.get("Origin", "")).strip().lower()

        class_match = (
            (program_class == "assigned" and "assign" in spec_origin)
            or (program_class in {"derived", "constant"} and "deriv" in spec_origin)
        )

        # Prototype only: source comparison is intentionally conservative.
        spec_source = str(spec.get("Source", "")).strip().upper()
        immediate = str(row.get("immediate_source", "")).strip().upper()

        if class_match and (not spec_source or any(tok in spec_source for tok in immediate.split(", ") if tok)):
            status = "MATCH"
            note = "Prototype comparison found no obvious origin/source conflict."
        elif not class_match:
            status = "MISMATCH"
            note = f"Program classification '{row.get('classification')}' differs from spec origin '{spec.get('Origin', '')}'."
        else:
            status = "REVIEW REQUIRED"
            note = "Source/derivation requires semantic or deeper lineage comparison."

        results.append({
            **row,
            "spec_origin": spec.get("Origin", ""),
            "spec_source": spec.get("Source", ""),
            "spec_derivation": spec.get("Derivation", ""),
            "status": status,
            "review_note": note
        })

    return results
