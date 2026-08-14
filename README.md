# Does the Code Match the Spec?
## Deterministic Variable Lineage with Local SAS Derivation Explanation

A proof-of-concept tool for reverse-engineering variable lineage from Clinical SAS programs and optionally comparing the implemented lineage against a specification.

## Why this project?

Clinical programming specifications and production SAS code can diverge as requirements evolve. Manual review of variable origin and derivation is time-consuming and can miss subtle inconsistencies.

This project combines deterministic analysis with an optional local explanation layer:

1. **Deterministic SAS parsing** to extract datasets, variables, assignments, and derivation relationships.
2. **Variable lineage reconstruction** from final output variables back toward source datasets and variables.
3. **Explain Implemented Logic** using an optional local SAS derivation explanation layer.
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

## Explain Implemented Logic

Deterministic `MATCH`, `MISMATCH`, and `REVIEW REQUIRED` remain authoritative. The optional
Local SAS Derivation Explanation layer describes what the deterministic parser found; it
does not compare the implementation with the specification and cannot change status.
Every variable can be explained. The Streamlit UI lists `REVIEW REQUIRED` variables first
to help reviewers focus attention without hiding `MATCH` or `MISMATCH` rows.

The included Ollama profile uses the open-weight `qwen2.5-coder:7b` code model because its
quantized download is practical for many conference-demo laptops. Build the local
SAS-focused profile and enable it for the application:

```bash
# Install Ollama separately, then:
ollama pull qwen2.5-coder:7b
ollama create sas-explainer -f local_model/Modelfile

# Windows PowerShell
$env:LOCAL_SAS_EXPLAINER_ENABLED="1"
streamlit run app/streamlit_app.py
```

Configuration is optional:

```text
LOCAL_SAS_EXPLAINER_MODEL=sas-explainer:latest
LOCAL_SAS_EXPLAINER_URL=http://127.0.0.1:11434/api/chat
```

The feature is off by default. The adapter accepts only an HTTP loopback address
(`127.0.0.1`, `localhost`, or `::1`) ending in `/api/chat`; a cloud or LAN model endpoint
is rejected. Explanations are stored separately from deterministic results and cannot
override them.

### SAS-specialized behavior

The versioned [`local_model/Modelfile`](local_model/Modelfile) gives the code model a
conservative Clinical SAS role covering DATA-step assignments, `INPUT`/`PUT`, SAS date
semantics, `IF/THEN/ELSE`, `SET`, `MERGE`, `PROC SORT`, and missing values. Requests use
temperature zero and a strict JSON schema. An explanation is rejected if it invents
qualified SAS identities, unsupported source claims, statements, or line references.

This is prompt specialization, not a claim that the base model was fine-tuned on Clinical
SAS. A future SAS-specific fine-tuned or GGUF model can replace the `FROM` model without
changing the deterministic engine or request contract.

### Exact information provided to the local model

For a selected variable, the request contains a fixed system instruction that says to
explain only the implemented deterministic logic, treat specification text as optional
context, and never invent datasets, variables, SAS statements, values, or line numbers.
The user payload contains only this JSON-shaped metadata:

```text
variable_name
deterministic_lineage_paths
deterministic_derivation_logic
supported_evidence_statements
optional_specification_context.origin
optional_specification_context.source
optional_specification_context.derivation
```

`supported_evidence_statements` contains only SAS statements already extracted and marked
as supported by the deterministic parser. The request does **not** include uploaded dataset
contents, patient-level data, workbook contents beyond the selected specification fields,
the complete uploaded SAS program, parser line numbers, filenames, or unsupported evidence.
Nothing is sent to a cloud API. The structured response contains only a concise derivation
summary, evidence-supported sources, ordered implemented steps, and uncertainty or
limitations. Specification context is omitted when no specification is loaded.

## Important disclaimer

This repository is a research/proof-of-concept project. It is **not a validated clinical or regulatory production system** and should not be used as the sole basis for regulatory decisions, production QC, or specification approval.

Do not upload proprietary sponsor code, confidential study specifications, patient-level data, credentials, or protected health information to a public deployment.

## Conference concept

**Does the Code Match the Spec? Deterministic Lineage with Local SAS Derivation Explanation**

The central question is simple:

> Can we automatically reconstruct what a SAS program actually implemented, and compare that implementation with what the specification says should have been implemented?
