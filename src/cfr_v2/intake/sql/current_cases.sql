/* CFR V2 case query (C01). Copied from cfr-v1/src/data/sql/current_cases.sql with:
   - the potl_defect_flag filter removed (OQ-13), so flagged cases are admitted too;
   - err_type, err_sub_type and case_type carried through to the output (V1 dropped them).
   Parameters: @start_date, @end_date (DATE, on orig_rslv_ts). One row per case_no. */
WITH base_cases AS (
SELECT
case_no,
latest_rslv_optn,
discrimination_type,
orig_rslv_ts,
reg_sta,
prim_ctgy,
prim_subctgy,
potl_defect_flag,
err_type,
err_sub_type,
case_type,
CASE WHEN grp_cd = 'VOCL' THEN 'Yes' ELSE 'No' END AS cqat_flag,
CASE WHEN esclt_case_id IS NOT NULL THEN 'Yes' ELSE 'No' END AS pie_flag,
creat_opr_id,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM11', cm11) AS cm11,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM15', cm15) AS cm15,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Comment', case_note) AS case_note,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Generic', case_ds) AS case_ds,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Comment', ccp_note) as ccp_note,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Comment', pend_resp_note) as pend_resp_note,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Comment', req_note) as req_note,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Comment', pend_req_note) as pend_req_note,
latest_rslv_note,
rcpt_ts
FROM `axp-lumi.dw.gsn_icare_case_dtl`
WHERE mkt_cd = 'US'
AND crnt_case_rec_flag = 'Yes'
AND DATE(orig_rslv_ts) BETWEEN @start_date AND @end_date
-- AND grp_cd not in ('VOCL', 'TLS2LR', 'CMS2LR')
and orig_grp_cd NOT IN ('SCOUT','TLSCEN','TLSPTNR','TLSECR','TLS')
),

/* decrypt cm15 in global_call once */
gsnmon_global_call_decrypt AS (
SELECT
* EXCEPT (cm15),
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM15', cm15) AS cm15,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Generic', emp_pplsft_id) AS emp_pplsft_id
FROM `axp-lumi.dw.gsnmon_global_call`
),

gsnmon_global_call_enrich AS (
SELECT
gc.*,
SUBSTRING(gc.cm15, 1, 11) AS cm11
FROM gsnmon_global_call_decrypt gc
),

/* PRIMARY PATH: Case -> global_call by cm11 OR cm15 (1-day date window) */
case_global_call AS (
SELECT
bc.*,
gc.emp_ntwk_id,
CAST(gc.nexidia_intrct_id AS STRING) AS intrct_id,
gc.ctc_utc_ts
FROM base_cases bc
JOIN gsnmon_global_call_enrich gc
ON (bc.cm11 = gc.cm11 OR bc.cm15 = gc.cm15)
AND DATE(bc.rcpt_ts) BETWEEN DATE(gc.ctc_utc_ts)
AND DATE_ADD(DATE(gc.ctc_utc_ts), INTERVAL 1 DAY)
WHERE gc.nexidia_intrct_id IS NOT NULL
),

/* SECONDARY PATH: people-soft ID matching for cases missing cm11/cm15 or not matched above.
Build candidate pairs and keep the closest call per case (±1 day window) */
base_cases_null_cm AS (
SELECT
*
FROM base_cases
WHERE cm11 IS NULL
AND cm15 IS NULL
),

global_call_null_cm_filtered AS (
SELECT
gc.* EXCEPT (cm15, emp_pplsft_id),
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM15', gc.cm15) AS cm15,
SUBSTRING(`axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM15', gc.cm15), 1, 11) AS cm11,
`axp-lumi.dw.decrypt_sde`('NGBD-SDE-Generic', emp_pplsft_id) AS emp_pplsft_id
FROM `axp-lumi.dw.gsnmon_global_call` gc
WHERE gc.emp_pplsft_id IS NOT NULL
AND gc.cm15 IS NULL
AND gc.nexidia_intrct_id IS NOT NULL
),

case_call_candidates AS (
SELECT
bc.case_no,
bc.creat_opr_id,
bc.rcpt_ts,
gc.emp_pplsft_id,
gc.emp_ntwk_id,
CAST(gc.nexidia_intrct_id AS STRING) AS intrct_id,
gc.ctc_utc_ts,
ABS(TIMESTAMP_DIFF(bc.rcpt_ts, gc.ctc_utc_ts, SECOND)) AS ts_diff_seconds
FROM base_cases_null_cm bc
JOIN global_call_null_cm_filtered gc
ON CAST(bc.creat_opr_id AS STRING) = CAST(gc.emp_pplsft_id AS STRING)
WHERE gc.ctc_utc_ts BETWEEN TIMESTAMP_SUB(bc.rcpt_ts, INTERVAL 1 DAY)
AND TIMESTAMP_ADD(bc.rcpt_ts, INTERVAL 1 DAY)
),

/* keep single best candidate per case_no (closest timestamp) */
case_call_best AS (
SELECT
case_no,
creat_opr_id,
rcpt_ts,
emp_pplsft_id,
emp_ntwk_id,
intrct_id,
ctc_utc_ts
FROM case_call_candidates
QUALIFY ROW_NUMBER() OVER (
PARTITION BY case_no
ORDER BY ts_diff_seconds ASC, ctc_utc_ts ASC
) = 1
),

/* combine the two match-paths, avoiding duplicates: prefer cm match (case_global_call).
For cases matched by operator-id only, include them too. */
case_global_call_all AS (
-- include all cm-based matches
SELECT * EXCEPT(ctc_utc_ts) FROM case_global_call

UNION ALL

-- include operator-id matches only for cases not already matched via cm
SELECT
bc.*,
cb.emp_ntwk_id,
cb.intrct_id
FROM base_cases bc
JOIN case_call_best cb
ON bc.case_no = cb.case_no
WHERE bc.case_no NOT IN (SELECT DISTINCT case_no FROM case_global_call)
),

/* One row per (case_no, intrct_id) to prevent multiplication later */
case_intrct AS (
SELECT
case_no,
ANY_VALUE(latest_rslv_optn) AS latest_rslv_optn,
ANY_VALUE(discrimination_type) AS discrimination_type,
ANY_VALUE(orig_rslv_ts) AS orig_rslv_ts,
ANY_VALUE(reg_sta) AS reg_sta,
ANY_VALUE(prim_ctgy) AS prim_ctgy,
ANY_VALUE(prim_subctgy) AS prim_subctgy,
ANY_VALUE(cqat_flag) AS cqat_flag,
ANY_VALUE(pie_flag) AS pie_flag,
ANY_VALUE(creat_opr_id) AS creat_opr_id,
ANY_VALUE(cm11) AS cm11,
ANY_VALUE(cm15) AS cm15,
ANY_VALUE(case_note) AS case_note,
ANY_VALUE(case_ds) AS case_ds,
ANY_VALUE(ccp_note) AS ccp_note,
ANY_VALUE(pend_resp_note) AS pend_resp_note,
ANY_VALUE(req_note) AS req_note,
ANY_VALUE(pend_req_note) AS pend_req_note,
ANY_VALUE(latest_rslv_note) AS latest_rslv_note,
ANY_VALUE(rcpt_ts) AS rcpt_ts,
ANY_VALUE(emp_ntwk_id) AS emp_ntwk_id,
ANY_VALUE(potl_defect_flag) AS potl_defect_flag,
ANY_VALUE(err_type) AS err_type,
ANY_VALUE(err_sub_type) AS err_sub_type,
ANY_VALUE(case_type) AS case_type,
intrct_id
FROM case_global_call_all
GROUP BY case_no, intrct_id
),

/* Transcript: enforce one row per intrct_id (take latest intrct_ts if duplicates exist) */
mt_dedup AS (
SELECT
CAST(intrct_id AS STRING) AS intrct_id,
CAST(intrct_ts AS TIMESTAMP) AS intrct_ts,
agnt_nm,
trnscr_clean_read_tx AS transcript
FROM `axp-lumi.dw.gsnnexidia_merge_transcript`
QUALIFY ROW_NUMBER() OVER (
PARTITION BY CAST(intrct_id AS STRING)
ORDER BY CAST(intrct_ts AS TIMESTAMP) DESC
) = 1
),

/* NICE: enforce one row per intrct_id (ads_id/doc_id) */
nsi_dedup AS (
SELECT
CAST(intrct_id AS STRING) AS intrct_id,
ANY_VALUE(ads_id) AS ads_id,
ANY_VALUE(COALESCE(complete_id, extnl_id)) AS cmpl_extrnl_id
FROM `axp-lumi.dw.nice_non_speech_interaction`
GROUP BY CAST(intrct_id AS STRING)
),

rows_enriched AS (
SELECT
ci.*,
mt.intrct_ts,
mt.agnt_nm,
mt.transcript,
nsi.ads_id,
nsi.cmpl_extrnl_id,

CASE
WHEN ci.intrct_id IS NULL THEN 'NULL'
WHEN (
ci.emp_ntwk_id IS NOT NULL AND nsi.ads_id IS NOT NULL AND
(
TRIM(LOWER(ci.emp_ntwk_id)) LIKE CONCAT(TRIM(LOWER(nsi.ads_id)), '%')
OR TRIM(LOWER(nsi.ads_id)) LIKE CONCAT(TRIM(LOWER(ci.emp_ntwk_id)), '%')
)
) THEN 'Exact-Matched'
WHEN ci.intrct_id IS NOT NULL THEN 'Not-Matched'
ELSE 'NULL'
END AS row_match_flag
FROM case_intrct ci
LEFT JOIN mt_dedup mt
ON mt.intrct_id = ci.intrct_id
LEFT JOIN nsi_dedup nsi
ON nsi.intrct_id = ci.intrct_id
)

SELECT
case_no,
ANY_VALUE(latest_rslv_optn) AS latest_rslv_optn,
ANY_VALUE(discrimination_type) AS discrimination_type,
ANY_VALUE(orig_rslv_ts) AS orig_rslv_ts,
ANY_VALUE(reg_sta) AS reg_sta,
ANY_VALUE(prim_ctgy) AS prim_ctgy,
ANY_VALUE(prim_subctgy) AS prim_subctgy,
ANY_VALUE(cqat_flag) AS cqat_flag,
ANY_VALUE(pie_flag) AS pie_flag,
ANY_VALUE(creat_opr_id) AS creat_opr_id,
ANY_VALUE(cm11) AS cm11,
ANY_VALUE(cm15) AS cm15,
ANY_VALUE(case_note) AS case_note,
ANY_VALUE(case_ds) AS case_ds,
ANY_VALUE(ccp_note) AS ccp_note,
ANY_VALUE(pend_resp_note) AS pend_resp_note,
ANY_VALUE(req_note) AS req_note,
ANY_VALUE(pend_req_note) AS pend_req_note,
ANY_VALUE(latest_rslv_note) AS latest_rslv_note,
ANY_VALUE(rcpt_ts) AS rcpt_ts,
ANY_VALUE(emp_ntwk_id) AS emp_ntwk_id,
ANY_VALUE(potl_defect_flag) AS potl_defect_flag,
ANY_VALUE(err_type) AS err_type,
ANY_VALUE(err_sub_type) AS err_sub_type,
ANY_VALUE(case_type) AS case_type,

ARRAY_AGG(DISTINCT intrct_id IGNORE NULLS) AS intrct_ids,
ARRAY_AGG(DISTINCT cmpl_extrnl_id IGNORE NULLS) AS doc_id,

CASE
WHEN MAX(CASE WHEN row_match_flag = 'Exact-Matched' THEN 1 ELSE 0 END) = 1 THEN 'Exact-Matched'
WHEN MAX(CASE WHEN row_match_flag = 'Not-Matched' THEN 1 ELSE 0 END) = 1 THEN 'Not-Matched'
ELSE 'NULL'
END AS exact_match_flag,

REGEXP_REPLACE(
ARRAY_TO_STRING(
ARRAY_AGG(
IF(
transcript IS NULL OR TRIM(transcript) = '' OR intrct_ts IS NULL,
NULL,
CONCAT(FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', intrct_ts), '||', transcript)
)
IGNORE NULLS
ORDER BY intrct_ts
),
'|'
),
r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\|\|',
''
) AS msg_tx2

FROM rows_enriched
GROUP BY case_no;