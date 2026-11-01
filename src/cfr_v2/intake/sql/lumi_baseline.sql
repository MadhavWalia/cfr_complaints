/* CFR V2 LUMI baseline (C01, LDR-0014): the approved minimal customer baseline per case.
   Port of cfr-v1/src/data/sql/structured/new_demographics.sql (OQ-06, OQ-11). Changes:
   - its {{CURRENT_CASES_TABLE}} is current_cases.sql's base_cases CTE (cm15 already decrypted),
     so this is not a standalone query: it is appended to the case query's WITH clause, and
     the combined query runs as one BigQuery job (case_query_sql(with_baseline=True));
   - CTE names carry a bl_ prefix; the final GROUP BY becomes the `baseline` CTE.
   Output per case_no:
     llm_context  JSON for the model (no IDs, no PII, no protected attributes)
     ref_map      account_ref -> rel_id / cm15, RESTRICTED, never sent to a model
     n_accounts   1 means account disambiguation is trivial
   Bump BASELINE_QUERY_VERSION in intake/source.py on every change. */
bl_params AS (
  SELECT 24 AS canc_lookback_months   -- include accounts closed up to N months before the case
),

bl_base AS (
  SELECT
    case_no,
    cm15,                                          -- already decrypted in base_cases
    DATE(rcpt_ts) AS ref_dt                        -- complaint received date
  FROM base_cases
  WHERE cm15 IS NOT NULL
),

-- Latest row per relationship, only the columns we need
bl_c360 AS (
  SELECT
    rel_id,
    parnt_rel_id,
    cust_id,
    cm15 AS cm15_enc,
    prod_full_nm,
    prod_co_brnd_in,
    prod_co_brnd_nm,
    bus_unit_cd,
    acct_ctgy_type_cd,
    acct_sta_ds,
    rel_type,
    rel_sub_role,
    rgn_mkt_nm,
    acct_setup_dt,
    card_repl_dt,
    acct_canc_dt
  FROM `axp-lumi.dw.cust360_customer_demographics`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY rel_id ORDER BY lst_updt_ts DESC) = 1
),

-- 1. Resolve the case card -> customer (the only unavoidable decrypt: C360 side)
bl_case_acct AS (
  SELECT
    b.case_no,
    b.ref_dt,
    b.cm15 AS case_cm15,
    c.rel_id AS case_rel_id,
    c.cust_id
  FROM bl_base b
  JOIN bl_c360 c
    ON `axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM15', c.cm15_enc) = b.cm15
  QUALIFY ROW_NUMBER() OVER (PARTITION BY b.case_no ORDER BY c.acct_setup_dt DESC) = 1
),

-- 2a. Every account the customer holds
bl_own AS (
  SELECT ca.case_no, ca.ref_dt, ca.case_rel_id, ca.case_cm15, c.*, 'Customer' AS holder
  FROM bl_case_acct ca
  JOIN bl_c360 c
    ON c.cust_id = ca.cust_id
),

-- 2b. Cards on the customer's accounts held by someone else (e.g. spouse's supp)
bl_linked_children AS (
  SELECT o.case_no, o.ref_dt, o.case_rel_id, o.case_cm15, c.*, 'Other cardholder' AS holder
  FROM bl_own o
  JOIN bl_c360 c
    ON c.parnt_rel_id = o.rel_id
   AND c.cust_id != o.cust_id
),

-- 2c. The basic account, if the customer is a supp / associate on someone else's account
bl_linked_parents AS (
  SELECT o.case_no, o.ref_dt, o.case_rel_id, o.case_cm15, c.*, 'Other cardholder' AS holder
  FROM bl_own o
  JOIN bl_c360 c
    ON c.rel_id = o.parnt_rel_id
   AND c.cust_id != o.cust_id
),

bl_all_accts AS (
  SELECT * FROM bl_own
  UNION ALL SELECT * FROM bl_linked_children
  UNION ALL SELECT * FROM bl_linked_parents
),

-- Normalise dates (works whether the column is DATE, DATETIME, TIMESTAMP or 'YYYY-MM-DD' string),
-- drop the 0001-01-01 sentinel, dedupe
bl_cleaned AS (
  SELECT
    a.* EXCEPT (acct_setup_dt, card_repl_dt, acct_canc_dt),
    NULLIF(SAFE_CAST(a.acct_setup_dt AS DATE), DATE '0001-01-01') AS setup_dt,
    NULLIF(SAFE_CAST(a.card_repl_dt  AS DATE), DATE '0001-01-01') AS repl_dt,
    NULLIF(SAFE_CAST(a.acct_canc_dt  AS DATE), DATE '0001-01-01') AS canc_dt
  FROM bl_all_accts a
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.case_no, a.rel_id
    ORDER BY IF(a.holder = 'Customer', 0, 1)
  ) = 1
),

-- Keep only accounts that existed when the complaint was received and weren't closed long before
bl_filtered AS (
  SELECT c.*
  FROM bl_cleaned c
  CROSS JOIN bl_params p
  WHERE (
          (c.setup_dt IS NULL OR c.setup_dt <= c.ref_dt)
          AND (c.canc_dt IS NULL
               OR c.canc_dt >= DATE_SUB(c.ref_dt, INTERVAL p.canc_lookback_months MONTH))
        )
     OR c.rel_id = c.case_rel_id                 -- always keep the case account
),

-- Short refs: case account = A1, then customer's own, basics first, oldest first
bl_refs AS (
  SELECT
    f.*,
    ROW_NUMBER() OVER (
      PARTITION BY f.case_no
      ORDER BY
        IF(f.rel_id = f.case_rel_id, 0, 1),
        IF(f.holder = 'Customer', 0, 1),
        IF(f.parnt_rel_id IS NULL, 0, 1),
        f.setup_dt
    ) AS ref_no
  FROM bl_filtered f
),

bl_llm_rows AS (
  SELECT
    r.case_no,
    r.ref_dt,
    r.ref_no,
    CONCAT('A', CAST(r.ref_no AS STRING)) AS account_ref,
    r.rel_id,
    r.cm15_enc,
    r.case_cm15,
    r.rel_id = r.case_rel_id AS is_case_account,
    r.prod_full_nm AS product,
    IF(r.prod_co_brnd_in = 'Y', r.prod_co_brnd_nm, NULL) AS co_brand_partner,
    CASE r.bus_unit_cd
      WHEN 'S' THEN 'Consumer'
      WHEN 'O' THEN 'OPEN (small business)'
      WHEN 'C' THEN 'Corporate'
      WHEN 'E' THEN 'Merchant'
      ELSE r.bus_unit_cd
    END AS business_unit,
    CASE r.acct_ctgy_type_cd
      WHEN 'CRD' THEN 'Card'
      WHEN 'CLN' THEN 'Consumer loan'
      WHEN 'LOC' THEN 'Line of credit'
      WHEN 'OLN' THEN 'Business loan'
      WHEN 'LNS' THEN 'Loan'
      WHEN 'PSA' THEN 'Personal savings account'
      WHEN 'CCH' THEN 'Consumer checking account'
      WHEN 'BCH' THEN 'Business checking account'
      ELSE r.acct_ctgy_type_cd
    END AS account_type,
    -- Status as of complaint receipt, not today
    CASE
      WHEN r.canc_dt > r.ref_dt THEN 'Open at time of case'
      ELSE r.acct_sta_ds
    END AS status_at_case,
    FORMAT_DATE('%Y-%m', r.setup_dt) AS opened_month,
    IF(r.repl_dt <= r.ref_dt, FORMAT_DATE('%Y-%m', r.repl_dt), NULL) AS card_replaced_month,
    r.holder,
    CASE
      WHEN r.parnt_rel_id IS NULL THEN 'Basic'
      WHEN p.ref_no IS NOT NULL THEN CONCAT(COALESCE(r.rel_type, 'Linked'), ' on A', CAST(p.ref_no AS STRING))
      ELSE CONCAT(COALESCE(r.rel_type, 'Linked'), ' on another account')
    END AS card_role,
    r.rel_sub_role AS role_detail,
    r.rgn_mkt_nm AS market
  FROM bl_refs r
  LEFT JOIN bl_refs p
    ON p.case_no = r.case_no
   AND p.rel_id = r.parnt_rel_id
),

baseline AS (
  SELECT
    case_no,
    -- Sent to the LLM
    TO_JSON_STRING(STRUCT(
      FORMAT_DATE('%Y-%m-%d', ANY_VALUE(ref_dt)) AS case_received_date,
      ARRAY_AGG(STRUCT(
        account_ref, is_case_account, product, co_brand_partner, business_unit,
        account_type, status_at_case, opened_month, card_replaced_month, holder,
        card_role, role_detail, market
      ) ORDER BY ref_no) AS accounts
    )) AS llm_context,
    -- Kept in the pipeline only
    ARRAY_AGG(STRUCT(
      account_ref,
      rel_id,
      CASE
        WHEN is_case_account THEN case_cm15                               -- reuse, no decrypt
        WHEN cm15_enc IS NULL THEN NULL                                   -- non-37 accounts
        ELSE `axp-lumi.dw.decrypt_sde`('NGBD-SDE-CM15', cm15_enc)         -- linked accounts only
      END AS cm15
    ) ORDER BY ref_no) AS ref_map,
    COUNT(*) AS n_accounts       -- = 1 -> you can skip disambiguation for this case
  FROM bl_llm_rows
  GROUP BY case_no
)
