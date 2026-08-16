/* Fully synthetic ABR acceptance example.
   No proprietary study logic or patient data is represented. */

data qualifying_bleeds(keep=usubjid event_id eventdt);
    set synthetic.bleeding_events;
    if bleedfl = "Y"
       and not missing(eventdtc)
       and exclusionfl ne "Y";
    eventdt = input(eventdtc, yymmdd10.);
run;

proc sql;
    create table bleed_counts as
    select usubjid,
           count(event_id) as n_qual_bleeds
    from qualifying_bleeds
    group by usubjid;
quit;

data subject_followup(
    keep=usubjid obs_startdt obs_enddt followup_days observation_years
);
    set synthetic.subject_level;
    obs_startdt = input(obs_startdtc, yymmdd10.);
    obs_enddt = input(obs_enddtc, yymmdd10.);
    followup_days = obs_enddt - obs_startdt + 1;
    observation_years = followup_days / 365.25;
run;

proc sort data=bleed_counts out=bleed_counts_sorted;
    by usubjid;
run;

proc sort data=subject_followup out=subject_followup_sorted;
    by usubjid;
run;

data abr_analysis(
    keep=usubjid n_qual_bleeds obs_startdt obs_enddt
         followup_days observation_years abr
);
    merge subject_followup_sorted(in=a)
          bleed_counts_sorted(in=b);
    by usubjid;
    if a and b;
    if observation_years > 0 then abr = n_qual_bleeds / observation_years;
run;
