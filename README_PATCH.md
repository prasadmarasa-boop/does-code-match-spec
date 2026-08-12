# v2 patch

Replace these files in your GitHub repository:

- `src/sas_parser.py`
- `src/spec_checker.py`

Changes:
- exact token-based source matching
- adds `ultimate_source`
- filters common SAS formats/informats from variable extraction
- traces merge `IN=` indicators back to the contributing dataset
- traces intermediate DATA step sources
