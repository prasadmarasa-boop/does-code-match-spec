import sys
from pathlib import Path
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.sas_parser import analyze_sas
from src.spec_checker import load_spec, compare_to_spec


st.set_page_config(
    page_title="Does the Code Match the Spec?",
    layout="wide"
)

st.title("Does the Code Match the Spec?")
st.caption("AI-Assisted Variable Lineage for Clinical SAS Programming — Proof of Concept")

st.warning(
    "Prototype only. Do not upload proprietary code, confidential study specifications, "
    "patient-level data, PHI, credentials, or production sponsor materials."
)

left, right = st.columns(2)

with left:
    sas_file = st.file_uploader("Upload SAS program (.sas)", type=["sas"])
    pasted = st.text_area("Or paste SAS code", height=300)

with right:
    spec_file = st.file_uploader(
        "Optional: upload specification (.xlsx)",
        type=["xlsx"]
    )
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

    st.subheader(f"Final dataset: {result['final_dataset']}")
    st.write("Input datasets detected:", ", ".join(result["sources"]) or "None detected")

    lineage = result["variables"]

    if spec_file is None:
        st.subheader("Variable Lineage")
        st.dataframe(pd.DataFrame(lineage), use_container_width=True)
    else:
        spec_df = load_spec(spec_file)
        compared = compare_to_spec(
            lineage,
            spec_df,
            result["final_dataset"],
            result["variables_complete"],
        )

        st.subheader("Spec Validation")
        df = pd.DataFrame(compared)
        st.dataframe(df, use_container_width=True)

        counts = df["status"].value_counts()
        c1, c2, c3 = st.columns(3)
        c1.metric("MATCH", int(counts.get("MATCH", 0)))
        c2.metric("MISMATCH", int(counts.get("MISMATCH", 0)))
        c3.metric("REVIEW REQUIRED", int(counts.get("REVIEW REQUIRED", 0)))

st.divider()
st.caption(
    "Research/proof-of-concept only. Not validated for regulatory production use."
)
