use crate::db::Db;
use anyhow::{anyhow, Context, Result};
use serde::Serialize;

const HOUJIN_CATALOG: &str = "https://data.queria.io/houjin_bangou/ducklake.duckdb";
const GBIZINFO_CATALOG: &str = "https://data.queria.io/gbizinfo/ducklake.duckdb";

#[derive(Debug, Clone, Serialize)]
pub struct NativeDuckDbStatus {
    pub available: bool,
    pub version: String,
    pub engine: String,
    pub remote_catalog_mode: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct NativeSyncResult {
    pub imported: u64,
    pub source: String,
    pub duckdb_version: String,
}

pub fn status(db: &Db) -> Result<NativeDuckDbStatus> {
    let conn = db.connect()?;
    let version: String = conn.query_row("SELECT version()", [], |r| r.get(0))?;
    Ok(NativeDuckDbStatus {
        available: true,
        version,
        engine: "embedded-duckdb".to_string(),
        remote_catalog_mode: "DuckLake READ_ONLY direct attach".to_string(),
    })
}

/// Refresh the nationwide company master using only the embedded DuckDB engine.
///
/// No Queria/Python subprocess is involved. DuckDB loads its signed core
/// `ducklake`/`httpfs` extensions, attaches Queria's public DuckLake catalogs
/// read-only, joins them by corporate number, and materializes the result into
/// the app's local DuckDB file for fast repeated searches.
pub fn sync_company_master(db: &Db) -> Result<NativeSyncResult> {
    if db.runtime_path().is_some() {
        return Err(anyhow!(
            "既存のQueriaランタイムDBをREAD_ONLY接続中です。更新後にアプリを再起動してください"
        ));
    }
    let conn = db.connect()?;
    let version: String = conn.query_row("SELECT version()", [], |r| r.get(0))?;

    // Core extensions are version-matched to the embedded DuckDB runtime.
    conn.execute_batch(
        r#"
        INSTALL ducklake;
        LOAD ducklake;
        INSTALL httpfs;
        LOAD httpfs;
        "#,
    )
    .context("DuckDBのducklake/httpfs拡張を初期化できません")?;

    if let Ok(token) = std::env::var("QUERIA_TOKEN") {
        if !token.trim().is_empty() {
            let token = sql_quote(token.trim());
            let sql = format!(
                "CREATE OR REPLACE SECRET queria_auth (TYPE http, BEARER_TOKEN {token}, SCOPE 'https://data.queria.io');"
            );
            conn.execute_batch(&sql)
                .context("QUERIA_TOKENをDuckDB HTTP secretへ設定できません")?;
        }
    }

    let attach = format!(
        r#"
        ATTACH 'ducklake:{HOUJIN_CATALOG}' AS houjin_native (READ_ONLY);
        ATTACH 'ducklake:{GBIZINFO_CATALOG}' AS gbiz_native (READ_ONLY);
        "#
    );
    conn.execute_batch(&attach)
        .context("Queria DuckLakeをDuckDBから直接ATTACHできません")?;

    let stage_sql = r#"
        DROP TABLE IF EXISTS companies_refresh;
        CREATE TABLE companies_refresh (
          corporate_number VARCHAR PRIMARY KEY,
          name VARCHAR NOT NULL,
          prefecture VARCHAR,
          city VARCHAR,
          address VARCHAR,
          kind VARCHAR,
          industry_code VARCHAR,
          industry_name VARCHAR,
          industry_source VARCHAR,
          inferred_industry_code VARCHAR,
          inferred_industry_name VARCHAR,
          inferred_industry_confidence DOUBLE,
          employees BIGINT,
          capital BIGINT,
          established_year INTEGER,
          website VARCHAR,
          phone VARCHAR,
          representative VARCHAR,
          business_summary VARCHAR,
          business_items VARCHAR,
          subsidy_count BIGINT,
          subsidy_total_amount DOUBLE,
          procurement_count BIGINT,
          procurement_total_award DOUBLE,
          latest_fiscal_year INTEGER,
          latest_net_sales DOUBLE,
          latest_ordinary_income DOUBLE,
          latest_net_income DOUBLE,
          latest_total_assets DOUBLE,
          latest_net_assets DOUBLE,
          source_updated_at VARCHAR
        );

        INSERT INTO companies_refresh BY NAME
        SELECT
          CAST(h.corporate_number AS VARCHAR) AS corporate_number,
          h.name AS name,
          h.prefecture_name AS prefecture,
          h.city_name AS city,
          concat_ws('', h.prefecture_name, h.city_name, h.street_number) AS address,
          CAST(h.kind AS VARCHAR) AS kind,
          old.industry_code AS industry_code,
          old.industry_name AS industry_name,
          CASE
            WHEN old.industry_code IS NOT NULL THEN old.industry_source
            WHEN g.business_items IS NOT NULL THEN 'gBizINFO business_items'
            ELSE NULL
          END AS industry_source,
          old.inferred_industry_code,
          old.inferred_industry_name,
          old.inferred_industry_confidence,
          try_cast(g.employee_number AS BIGINT) AS employees,
          try_cast(g.capital_stock AS BIGINT) AS capital,
          coalesce(
            try_cast(year(try_cast(g.date_of_establishment AS DATE)) AS INTEGER),
            try_cast(g.founding_year AS INTEGER)
          ) AS established_year,
          g.company_url AS website,
          old.phone AS phone,
          g.representative_name AS representative,
          g.business_summary AS business_summary,
          g.business_items AS business_items,
          try_cast(g.subsidy_count AS BIGINT) AS subsidy_count,
          try_cast(g.subsidy_total_amount AS DOUBLE) AS subsidy_total_amount,
          try_cast(g.procurement_count AS BIGINT) AS procurement_count,
          try_cast(g.procurement_total_award AS DOUBLE) AS procurement_total_award,
          try_cast(g.latest_fiscal_year AS INTEGER) AS latest_fiscal_year,
          try_cast(g.latest_net_sales AS DOUBLE) AS latest_net_sales,
          try_cast(g.latest_ordinary_income AS DOUBLE) AS latest_ordinary_income,
          try_cast(g.latest_net_income AS DOUBLE) AS latest_net_income,
          try_cast(g.latest_total_assets AS DOUBLE) AS latest_total_assets,
          try_cast(g.latest_net_assets AS DOUBLE) AS latest_net_assets,
          CAST(h.update_date AS VARCHAR) AS source_updated_at
        FROM houjin_native.main.mart_houjin_bangou h
        LEFT JOIN gbiz_native.main.mart_gbizinfo_company g
          ON h.corporate_number = g.corporate_number
        LEFT JOIN companies old
          ON CAST(h.corporate_number AS VARCHAR) = old.corporate_number;
    "#;

    if let Err(err) = conn.execute_batch(stage_sql) {
        let _ = conn.execute_batch("DETACH gbiz_native; DETACH houjin_native;");
        return Err(err).context("DuckDBネイティブ同期のstaging作成に失敗しました");
    }

    let swap_sql = r#"
        BEGIN TRANSACTION;
        DROP TABLE companies;
        ALTER TABLE companies_refresh RENAME TO companies;
        CREATE INDEX idx_company_name ON companies(name);
        CREATE INDEX idx_company_prefecture ON companies(prefecture);
        CREATE INDEX idx_company_industry_code ON companies(industry_code);
        CREATE INDEX idx_company_employees ON companies(employees);
        COMMIT;
    "#;
    if let Err(err) = conn.execute_batch(swap_sql) {
        let _ = conn.execute_batch("ROLLBACK;");
        let _ = conn.execute_batch("DETACH gbiz_native; DETACH houjin_native;");
        return Err(err).context("DuckDBネイティブ同期のtable swapに失敗しました");
    }

    let _ = conn.execute_batch("DETACH gbiz_native; DETACH houjin_native;");

    let imported: i64 = conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?;
    // Keep query plans/statistics fresh after a multi-million-row replacement.
    conn.execute_batch("ANALYZE companies;")?;

    Ok(NativeSyncResult {
        imported: imported.max(0) as u64,
        source: "Queria DuckLake: houjin_bangou + gbizinfo (direct DuckDB READ_ONLY)".to_string(),
        duckdb_version: version,
    })
}

fn sql_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}
