/* sample_adsl.sas
   Prototype input for:
   Does the Code Match the Spec?
   AI-Assisted Variable Lineage for Clinical SAS Programming
*/

proc sort data=sdtm.dm out=dm;
    by usubjid;
run;

proc sort data=sdtm.ex out=ex;
    by usubjid exstdtc;
run;

data ex_trt;
    set ex;
    where not missing(exstdtc);
    by usubjid;
    if first.usubjid;
    keep usubjid exstdtc;
run;

data adsl;
    merge dm(in=a)
          ex_trt(in=b keep=usubjid exstdtc);
    by usubjid;
    if a;

    /* Directly assigned variables */
    studyid = studyid;
    usubjid = usubjid;
    age     = age;
    sex     = sex;

    /* Derived variables */
    trtsdt = input(rfxstdtc, yymmdd10.);
    format trtsdt date9.;

    if not missing(age) then do;
        if age < 18 then agegr1 = "<18";
        else agegr1 = ">=18";
    end;

    /* Safety flag is based on treatment evidence from EX */
    if b then saffl = "Y";
    else saffl = "N";

    keep studyid usubjid age sex trtsdt agegr1 saffl;
run;
