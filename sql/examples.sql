-- このファイルには複数の例があります。
-- `queria-master sql --file` は 1 文だけ受け付けるため、使う文をコピーしてください。

-- 1. 東京都・Webあり・従業員10人以上
SELECT
    corporate_number,
    company_name,
    city_name,
    employee_number,
    company_url
FROM core.v_info_communications
WHERE prefecture_name = '東京都'
  AND employee_number >= 10
  AND company_url IS NOT NULL
ORDER BY employee_number DESC NULLS LAST
LIMIT 100;

-- 2. 中分類39（情報サービス業）の都道府県別件数
-- SELECT
--     c.prefecture_name,
--     count(DISTINCT c.corporate_number) AS companies
-- FROM core.companies c
-- JOIN core.company_industries i USING (corporate_number)
-- WHERE i.jsic_middle_code = '39'
-- GROUP BY 1
-- ORDER BY 2 DESC;

-- 3. 補助金実績がある情報通信企業
-- SELECT
--     company_name,
--     prefecture_name,
--     subsidy_count,
--     subsidy_total_amount,
--     business_summary
-- FROM core.v_info_communications
-- WHERE subsidy_count > 0
-- ORDER BY subsidy_total_amount DESC NULLS LAST
-- LIMIT 100;

-- 4. 全業種のカテゴリ別件数（大分類）
-- SELECT jsic_major_code, jsic_major_name, company_count
-- FROM core.v_category_summary
-- WHERE jsic_level = 'major'
-- ORDER BY company_count DESC;

-- 5. 収録境界（完全収録・要約のみ・対象外）
-- SELECT * FROM meta.coverage_boundary ORDER BY scope_key;
