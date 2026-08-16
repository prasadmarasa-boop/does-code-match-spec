# Synthetic ABR Acceptance Test — Expected Lineage

This fixture is entirely synthetic. It demonstrates deterministic reverse engineering of
a multi-step clinical endpoint and does not encode proprietary study logic or data.

## Dataset summary

- Final dataset: `ABR_ANALYSIS`
- Direct input datasets: `SUBJECT_FOLLOWUP_SORTED`, `BLEED_COUNTS_SORTED`
- Upstream source datasets: `SYNTHETIC.BLEEDING_EVENTS`, `SYNTHETIC.SUBJECT_LEVEL`

## Expected ABR structure

```text
ABR
├── Numerator: N_QUAL_BLEEDS
│   └── COUNT(EVENT_ID) by USUBJID
│       └── QUALIFYING_BLEEDS
│           └── SYNTHETIC.BLEEDING_EVENTS
│               filtered by BLEEDFL = "Y"
│               and nonmissing EVENTDTC
│               and EXCLUSIONFL ne "Y"
└── Denominator: OBSERVATION_YEARS
    └── FOLLOWUP_DAYS / 365.25
        └── OBS_ENDDT - OBS_STARTDT + 1
            ├── INPUT(OBS_ENDDTC, YYMMDD10.)
            └── INPUT(OBS_STARTDTC, YYMMDD10.)
                └── SYNTHETIC.SUBJECT_LEVEL
```

## Important evidence

The parser should preserve exact statements and line numbers for:

- qualifying-event filter
- `CREATE TABLE BLEED_COUNTS`
- `COUNT(EVENT_ID) AS N_QUAL_BLEEDS`
- `FROM QUALIFYING_BLEEDS`
- `GROUP BY USUBJID`
- observation start and end conversions
- follow-up day arithmetic
- follow-up year conversion
- both `PROC SORT` pass-throughs
- final `MERGE` and subject-presence filter
- final ABR formula

The SQL support is intentionally narrow. Unsupported joins, unions, subqueries, `HAVING`,
window functions, and ambiguous SQL constructs must remain unresolved and carry an
unsupported-evidence note rather than fabricated lineage.
