WITH joined AS (
    SELECT
        h.corporate_number,
        h.name AS company_name,
        h.name_en AS company_name_en,
        h.furigana AS company_name_kana,
        c.name AS gbizinfo_company_name,
        h.kind AS corporate_kind_code,
        h.post_code,
        h.prefecture_code,
        h.prefecture_name,
        h.city_code,
        h.city_name,
        h.street_number,
        concat_ws('', h.prefecture_name, h.city_name, h.street_number) AS full_address,
        h.lg_code,
        c.representative_name,
        c.capital_stock,
        c.employee_number,
        c.date_of_establishment,
        c.founding_year,
        c.business_summary,
        concat_ws('|',
            CASE WHEN regexp_matches(coalesce(c.business_items, ''), '(^|[|\\-])G:') THEN 'G' END,
            CASE WHEN regexp_matches(coalesce(c.business_items, ''), '(^|-)37:') THEN 'G37' END,
            CASE WHEN regexp_matches(coalesce(c.business_items, ''), '(^|-)38:') THEN 'G38' END,
            CASE WHEN regexp_matches(coalesce(c.business_items, ''), '(^|-)39:') THEN 'G39' END,
            CASE WHEN regexp_matches(coalesce(c.business_items, ''), '(^|-)40:') THEN 'G40' END,
            CASE WHEN regexp_matches(coalesce(c.business_items, ''), '(^|-)41:') THEN 'G41' END
        ) AS jsic_codes_raw,
        c.business_items AS business_items_raw,
        c.company_url,
        c.subsidy_count,
        c.subsidy_total_amount,
        c.procurement_count,
        c.procurement_total_award,
        c.latest_fiscal_year,
        c.latest_net_sales,
        c.latest_ordinary_income,
        c.latest_net_income,
        c.latest_total_assets,
        c.latest_net_assets,
        c.avg_age,
        c.avg_monthly_overtime,
        c.female_ratio,
        h.update_date AS nta_update_date
    FROM houjin_bangou.main.mart_houjin_bangou h
    LEFT JOIN gbizinfo.main.mart_gbizinfo_company c
      ON h.corporate_number = c.corporate_number
)
SELECT
    *,
    CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G([|]|$)') THEN 'G' END AS jsic_major_code,
    CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G([|]|$)') THEN '情報通信業' END AS jsic_major_name,
    concat_ws('|',
        CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G37([|]|$)') THEN '37' END,
        CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G38([|]|$)') THEN '38' END,
        CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G39([|]|$)') THEN '39' END,
        CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G40([|]|$)') THEN '40' END,
        CASE WHEN regexp_matches(coalesce(jsic_codes_raw, ''), '(^|[|])G41([|]|$)') THEN '41' END
    ) AS jsic_middle_codes,
    current_timestamp AS extracted_at
FROM joined
