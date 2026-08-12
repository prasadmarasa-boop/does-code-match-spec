# Does the Code Match the Spec?
## AI-Assisted Variable Lineage for Clinical SAS Programming

A proof-of-concept tool for reverse-engineering variable lineage from Clinical SAS programs and optionally comparing the implemented lineage against a specification.

## Why this project?

Clinical programming specifications and production SAS code can diverge as requirements evolve. Manual review of variable origin and derivation is time-consuming and can miss subtle inconsistencies.

This project explores a hybrid approach:

1. **Deterministic SAS parsing** to extract datasets, variables, assignments, and derivation relationships.
2. **Variable lineage reconstruction** from final output variables back toward source datasets and variables.
3. **Optional AI interpretation** for natural-language derivation summaries and semantic spec comparison.
4. **Spec validation** to classify results as `MATCH`, `MISMATCH`, or `REVIEW REQUIRED`.

## Two operating modes

### Mode 1 — SAS program only

Upload or paste a SAS program. The tool generates:

- final dataset
- final variables
- variable classification (`Assigned`, `Derived`, `Renamed`, etc.)
- immediate source
- ultimate source
- derivation logic
- lineage confidence

### Mode 2 — SAS program + specification

The tool performs all Mode 1 processing, then compares implemented lineage against the uploaded specification:

- origin comparison
- source comparison
- derivation comparison
- `MATCH`
- `MISMATCH`
- `REVIEW REQUIRED`
- explanatory review notes

## Prototype scope

The first prototype intentionally supports a limited subset of SAS:

- `DATA`
- `SET`
- `MERGE`
- `KEEP`
- simple assignments
- `IF / THEN / ELSE`
- common functions such as `INPUT()`
- basic source tracking

Future versions may add:

- `PROC SQL`
- `RENAME`
- macro expansion
- `%INCLUDE`
- arrays
- nested intermediate datasets
- SDTM-to-ADaM multi-program lineage
- graph visualization
- downloadable review reports

## Run locally

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

## Demo files

See the `sample/` folder:

- `sample_adsl.sas`
- `sample_adsl_spec.xlsx`
- `expected_lineage_output.md`

The sample specification intentionally contains one mismatch for `SAFFL`.

## Important disclaimer

This repository is a research/proof-of-concept project. It is **not a validated clinical or regulatory production system** and should not be used as the sole basis for regulatory decisions, production QC, or specification approval.

Do not upload proprietary sponsor code, confidential study specifications, patient-level data, credentials, or protected health information to a public deployment.

## Conference concept

**Does the Code Match the Spec? AI-Assisted Variable Lineage for Clinical SAS Programming**

The central question is simple:

> Can we automatically reconstruct what a SAS program actually implemented, and compare that implementation with what the specification says should have been implemented?
