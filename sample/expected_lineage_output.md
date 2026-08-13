# Expected Prototype Output

## Mode 1 — SAS program only

Final dataset: ADSL

| Variable | Label | Classification | Immediate Source | Ultimate Source | Derivation Logic | Confidence |
|---|---|---|---|---|---|---|
| STUDYID | Study Identifier | Assigned | STUDYID | SDTM.DM.STUDYID | Direct carry-forward | High |
| USUBJID | Unique Subject Identifier | Assigned | USUBJID | SDTM.DM.USUBJID \| SDTM.EX.USUBJID | Direct carry-forward | Medium |
| AGE | Age | Assigned | AGE | SDTM.DM.AGE | Direct carry-forward | High |
| SEX | Sex | Assigned | SEX | SDTM.DM.SEX | Direct carry-forward | High |
| TRTSDT | Treatment Start Date | Derived | RFXSTDTC | SDTM.DM.RFXSTDTC | INPUT(RFXSTDTC, YYMMDD10.) | High |
| AGEGR1 | Age Group 1 | Derived | AGE | SDTM.DM.AGE | If AGE < 18 then '<18', else '>=18' | High |
| SAFFL | Safety Population Flag | Derived | EX_TRT merge indicator B | SDTM.EX dataset presence through EX_TRT | Y if subject has qualifying treatment evidence, else N | High |

## Mode 2 — SAS program + specification

| Variable | Program Finding | Specification | Result |
|---|---|---|---|
| STUDYID | SDTM.DM.STUDYID, Assigned | SDTM.DM.STUDYID, Assigned | MATCH |
| USUBJID | SDTM.DM.USUBJID, Assigned | SDTM.DM.USUBJID, Assigned | MATCH |
| AGE | SDTM.DM.AGE, Assigned | SDTM.DM.AGE, Assigned | MATCH |
| SEX | SDTM.DM.SEX, Assigned | SDTM.DM.SEX, Assigned | MATCH |
| TRTSDT | Derived from SDTM.DM.RFXSTDTC | Derived from SDTM.DM.RFXSTDTC with derivation text | REVIEW REQUIRED |
| AGEGR1 | Derived from AGE -> SDTM.DM.AGE | Derived from ADSL.AGE with derivation text | REVIEW REQUIRED |
| SAFFL | Derived from treatment evidence in EX | Spec source says SDTM.DM.USUBJID | MISMATCH |

Suggested explanation:
SAFFL is documented as sourced from SDTM.DM.USUBJID, but the SAS implementation sets it from the presence of a qualifying EX record through EX_TRT and merge indicator B. The source/derivation in the specification should be reviewed.

TRTSDT and AGEGR1 require manual review because deterministic semantic equivalence for
free-text derivations is intentionally not implemented in this version.
