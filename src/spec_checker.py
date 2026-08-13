import re

import pandas as pd


def load_spec(uploaded_file):
    df = pd.read_excel(uploaded_file)
    df.columns = [str(column).strip() for column in df.columns]
    return df


def cell_text(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def source_identities(text):
    """Return qualified (dataset, variable) identities from lineage text."""
    identities = set()
    for token in re.findall(r"\b[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)+\b", text.upper()):
        parts = token.split(".")
        identities.add((".".join(parts[:-1]), parts[-1]))
    return identities


def immediate_identities(text, final_dataset):
    if not final_dataset:
        return set()
    variables = re.findall(r"\b[A-Z_][A-Z0-9_]*\b", text.upper())
    return {(final_dataset.upper(), variable) for variable in variables}


def _spec_fields(spec):
    return {
        "spec_origin": cell_text(spec.get("Origin")),
        "spec_source": cell_text(spec.get("Source")),
        "spec_derivation": cell_text(spec.get("Derivation")),
    }


def compare_to_spec(lineage_rows, spec_df, final_dataset=None):
    spec_map = {
        cell_text(row.get("Variable")).upper(): row
        for _, row in spec_df.iterrows()
        if cell_text(row.get("Variable"))
    }
    results = []
    program_variables = set()

    for row in lineage_rows:
        variable = cell_text(row.get("variable")).upper()
        program_variables.add(variable)
        spec = spec_map.get(variable)
        if spec is None:
            results.append(
                {
                    **row,
                    "status": "REVIEW REQUIRED",
                    "review_note": "Variable is present in the program output but not in the uploaded specification.",
                }
            )
            continue

        fields = _spec_fields(spec)
        program_class = cell_text(row.get("classification")).lower()
        spec_origin = fields["spec_origin"].lower()
        class_match = (
            (program_class == "assigned" and "assign" in spec_origin)
            or (program_class in {"derived", "constant"} and "deriv" in spec_origin)
        )

        spec_source = fields["spec_source"].upper()
        spec_sources = source_identities(spec_source)
        program_sources = source_identities(cell_text(row.get("ultimate_source")))
        program_sources.update(
            immediate_identities(cell_text(row.get("immediate_source")), final_dataset)
        )

        if not spec_source:
            source_match = True
            source_evaluated = True
        elif not spec_sources:
            source_match = False
            source_evaluated = False
        else:
            source_match = bool(spec_sources & program_sources)
            source_evaluated = True

        if not class_match:
            status = "MISMATCH"
            note = (
                f"Program classification '{row.get('classification', '')}' differs from "
                f"spec origin '{fields['spec_origin']}'."
            )
        elif not source_evaluated:
            status = "REVIEW REQUIRED"
            note = (
                f"Documented source '{fields['spec_source']}' does not contain a qualified "
                "dataset.variable identity and cannot be compared conservatively."
            )
        elif not source_match:
            status = "MISMATCH"
            note = (
                f"Program lineage '{row.get('ultimate_source') or row.get('immediate_source', '')}' "
                f"does not match documented source '{fields['spec_source']}' by dataset and variable."
            )
        elif fields["spec_derivation"]:
            status = "REVIEW REQUIRED"
            note = (
                "The specification contains derivation text, but deterministic semantic derivation "
                "equivalence is not implemented. Manual review is required."
            )
        else:
            status = "MATCH"
            note = "Origin and qualified source identity match the implemented lineage."

        results.append({**row, **fields, "status": status, "review_note": note})

    for variable, spec in spec_map.items():
        if variable in program_variables:
            continue
        fields = _spec_fields(spec)
        results.append(
            {
                "variable": variable,
                "classification": "",
                "immediate_source": "",
                "ultimate_source": "",
                "derivation_logic": "",
                "confidence": "",
                **fields,
                "status": "MISMATCH",
                "review_note": "Variable is required by the specification but missing from the final program output.",
            }
        )

    return results
