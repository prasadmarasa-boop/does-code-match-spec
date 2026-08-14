import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.local_sas_explainer import (
    explain_row,
    explainer_from_environment,
    prioritize_explanation_rows,
)
from src.sas_parser import analyze_sas
from src.spec_checker import compare_to_spec, load_spec


AUDIT_COLUMNS = {
    "lineage_paths",
    "evidence",
    "ambiguity_notes",
    "contributing_datasets",
    "local_derivation_explanation",
}


def compact_frame(rows):
    frame = pd.DataFrame(rows)
    return frame.drop(
        columns=[column for column in AUDIT_COLUMNS if column in frame],
        errors="ignore",
    )


def show_lineage_evidence(rows):
    for row in rows:
        variable = row.get("variable", "Unknown variable")
        with st.expander(f"{variable}: Show lineage evidence"):
            paths = row.get("lineage_paths", [])
            if paths:
                st.markdown("**Lineage path**")
                for path in paths:
                    st.code(" → ".join(path), language=None)
            else:
                st.info("No supported lineage path is available for this variable.")

            st.markdown("**Supporting SAS evidence**")
            evidence = row.get("evidence", [])
            if not evidence:
                st.warning("No exact supported SAS evidence is available.")
            for item in evidence:
                relationship = item.get("relationship") or item.get("kind", "Evidence")
                if item.get("supported"):
                    start = item.get("start_line")
                    end = item.get("end_line")
                    line_label = f"line {start}" if start == end else f"lines {start}–{end}"
                    st.markdown(f"**{relationship}** — {line_label}")
                    st.code(item.get("statement", ""), language="sas")
                else:
                    st.warning(f"Unsupported/uncertain: {relationship}")
                if item.get("note"):
                    st.caption(item["note"])

            st.markdown(f"**Confidence:** {row.get('confidence', 'Unknown')}")
            notes = row.get("ambiguity_notes", [])
            if notes:
                st.markdown("**Ambiguity notes**")
                for note in notes:
                    st.write(f"- {note}")


def display_explanation(explanation):
    if not explanation.get("accepted", False):
        st.warning(explanation.get("summary", "The explanation could not be accepted."))
    else:
        st.markdown("**Concise derivation summary**")
        st.write(explanation.get("summary", ""))

    st.markdown("**Evidence-supported sources**")
    sources = explanation.get("supported_sources", [])
    st.write(", ".join(sources) if sources else "No supported sources were returned.")

    st.markdown("**Implemented logic**")
    steps = explanation.get("implemented_steps", [])
    if steps:
        for index, step in enumerate(steps, start=1):
            st.write(f"{index}. {step}")
    else:
        st.write("No accepted implementation steps were returned.")

    st.markdown("**Uncertainty / limitations**")
    limitations = explanation.get("limitations", [])
    if limitations:
        for limitation in limitations:
            st.write(f"- {limitation}")
    else:
        st.write("No additional limitations were reported.")


def show_explanation_sections(rows, explainer, enabled, analysis_id):
    st.subheader("Explain Implemented Logic")
    st.caption(
        "REVIEW REQUIRED variables are shown first. Explanations are optional and never change "
        "MATCH, MISMATCH, or REVIEW REQUIRED."
    )
    cache = st.session_state.setdefault("local_derivation_explanations", {})
    prioritized = prioritize_explanation_rows(rows)
    for original_index, row in prioritized:
        variable = row.get("variable", "Unknown variable")
        status = row.get("status", "LINEAGE ONLY")
        cache_key = f"{analysis_id}:{original_index}:{variable}"
        with st.expander(f"Explain implemented logic — {variable} [{status}]"):
            if status == "REVIEW REQUIRED":
                st.caption("Prioritized because deterministic validation requires review.")
            if not enabled or explainer is None:
                st.info("Enable Local SAS Derivation Explanation above to generate this explanation.")
            elif st.button("Generate local explanation", key=f"explain:{cache_key}"):
                explained = explain_row(row, explainer)
                cache[cache_key] = explained["local_derivation_explanation"]
            if cache_key in cache:
                display_explanation(cache[cache_key])


def render_analysis(view, explainer, explanation_enabled):
    st.subheader(f"Final dataset: {view['final_dataset']}")
    st.write("Direct input datasets:", ", ".join(view["sources"]) or "None detected")
    st.write(
        "Upstream source datasets:",
        ", ".join(view["upstream_sources"]) or "None detected",
    )

    rows = view["rows"]
    if view["has_spec"]:
        st.subheader("Spec Validation")
        frame = compact_frame(rows)
        st.dataframe(frame, use_container_width=True)
        counts = frame["status"].value_counts()
        c1, c2, c3 = st.columns(3)
        c1.metric("MATCH", int(counts.get("MATCH", 0)))
        c2.metric("MISMATCH", int(counts.get("MISMATCH", 0)))
        c3.metric("REVIEW REQUIRED", int(counts.get("REVIEW REQUIRED", 0)))
    else:
        st.subheader("Variable Lineage")
        st.dataframe(compact_frame(rows), use_container_width=True)

    show_explanation_sections(rows, explainer, explanation_enabled, view["analysis_id"])
    show_lineage_evidence(rows)


st.set_page_config(page_title="Does the Code Match the Spec?", layout="wide")

st.title("Does the Code Match the Spec?")
st.caption("Deterministic Variable Lineage with Local SAS Derivation Explanation — Proof of Concept")

st.warning(
    "Prototype only. Do not upload proprietary code, confidential study specifications, "
    "patient-level data, PHI, credentials, or production sponsor materials."
)

left, right = st.columns(2)

with left:
    sas_file = st.file_uploader("Upload SAS program (.sas)", type=["sas"])
    pasted = st.text_area("Or paste SAS code", height=300)

with right:
    spec_file = st.file_uploader("Optional: upload specification (.xlsx)", type=["xlsx"])
    st.markdown(
        """
        **Mode 1 — SAS only**
        - Variable metadata
        - Assigned/Derived classification
        - Immediate source
        - Derivation logic
        - Confidence

        **Mode 2 — SAS + Spec**
        - Everything in Mode 1
        - Origin/source comparison
        - MATCH / MISMATCH / REVIEW REQUIRED
        """
    )

local_explainer, local_disabled_reason = explainer_from_environment()
explanation_enabled = st.checkbox(
    "Enable Explain Implemented Logic",
    value=False,
    disabled=local_explainer is None,
    help=(
        "Uses a loopback-only Ollama model to explain deterministic lineage and supported SAS "
        "evidence. It does not classify or compare variables."
    ),
)
if local_explainer is None:
    st.caption(local_disabled_reason)
else:
    st.caption(
        "Optional and off by default. The local explanation cannot change deterministic status."
    )
if explanation_enabled:
    st.info(
        "Local SAS Derivation Explanation runs on this computer through a loopback-only endpoint. "
        "No cloud API is used. Dataset contents, filenames, line numbers, and whole programs are "
        "not sent to the model."
    )

if st.button("Analyze", type="primary"):
    if sas_file is not None:
        code = sas_file.getvalue().decode("utf-8", errors="replace")
    else:
        code = pasted

    if not code.strip():
        st.error("Upload a SAS program or paste SAS code.")
        st.stop()

    result = analyze_sas(code)
    if not result["final_dataset"]:
        st.error("The prototype could not identify a final DATA step.")
        st.stop()

    rows = result["variables"]
    has_spec = spec_file is not None
    if has_spec:
        rows = compare_to_spec(
            rows,
            load_spec(spec_file),
            result["final_dataset"],
            result["variables_complete"],
        )

    analysis_id = st.session_state.get("analysis_sequence", 0) + 1
    st.session_state["analysis_sequence"] = analysis_id
    st.session_state["local_derivation_explanations"] = {}
    st.session_state["analysis_view"] = {
        "analysis_id": analysis_id,
        "final_dataset": result["final_dataset"],
        "sources": result["sources"],
        "upstream_sources": result["upstream_sources"],
        "rows": rows,
        "has_spec": has_spec,
    }

if "analysis_view" in st.session_state:
    render_analysis(st.session_state["analysis_view"], local_explainer, explanation_enabled)

st.divider()
st.caption("Research/proof-of-concept only. Not validated for regulatory production use.")
