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
- conservative derivation review (`REVIEW REQUIRED` until semantic equivalence is implemented)
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
- output dataset options such as `DATA ADSL(KEEP=...)`
- `PROC SORT DATA=... OUT=...` lineage
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

The sample specification intentionally contains one mismatch for `SAFFL`. Rows with
nonblank derivation text are reported as `REVIEW REQUIRED` because semantic derivation
equivalence is not implemented yet.

Source validation is conservative: exact qualified lineage is required for `MATCH`,
contradictory qualified lineage is `MISMATCH`, and ambiguous MERGE attribution, blank
source evidence, incomplete output-variable discovery, or dataset-only evidence is
`REVIEW REQUIRED`.

## Run tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Optional AI semantic comparison

The deterministic lineage engine remains authoritative. An optional AI layer can assess
free-text derivation semantics only for rows whose deterministic result is
`REVIEW REQUIRED`. It never changes the deterministic `status`; its result is stored
separately as `semantic_assessment` with one of:

- `SEMANTIC MATCH`
- `SEMANTIC MISMATCH`
- `UNCERTAIN`

Install the optional dependency and configure the API through environment variables:

```bash
pip install -r requirements-ai.txt
# required to enable the UI checkbox
OPENAI_API_KEY=...
# optional; defaults to gpt-5.6
OPENAI_SEMANTIC_MODEL=gpt-5.6
```

The feature is off by default. It is disabled entirely when `OPENAI_API_KEY` or the
optional OpenAI SDK is unavailable. A deterministic `MISMATCH` is never sent for AI
comparison and can never be overridden. Every Responses API request explicitly sets
`store=False`.

### Exact information sent to the AI

For an eligible variable, the request contains a fixed system instruction that says to
compare derivation semantics, use only supplied metadata, never invent datasets,
variables, SAS statements, values, or line numbers, and return one of the three allowed
assessment labels with a concise rationale. The user payload contains only this
JSON-shaped metadata:

```text
variable_name
deterministic_lineage_path
deterministic_derivation_logic
specification.origin
specification.source
specification.derivation
evidence_statements
```

`evidence_statements` contains only SAS statements already extracted and marked as
supported by the deterministic parser. The request does **not** include uploaded dataset
contents, patient-level data, workbook contents beyond the selected specification fields,
the complete uploaded SAS program, parser line numbers, filenames, or unsupported evidence.
The AI can return only an assessment label and concise rationale. Its rationale is rejected
to `UNCERTAIN` if it introduces unapproved SAS identities, statements, or line references.
Rows without both specification derivation text and deterministic derivation logic are not
sent, because there is no semantic derivation pair to compare.

## Important disclaimer

This repository is a research/proof-of-concept project. It is **not a validated clinical or regulatory production system** and should not be used as the sole basis for regulatory decisions, production QC, or specification approval.

Do not upload proprietary sponsor code, confidential study specifications, patient-level data, credentials, or protected health information to a public deployment.

## Conference concept

**Does the Code Match the Spec? AI-Assisted Variable Lineage for Clinical SAS Programming**

The central question is simple:

> Can we automatically reconstruct what a SAS program actually implemented, and compare that implementation with what the specification says should have been implemented?
