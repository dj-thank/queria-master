use crate::models::{Company, DataStatus, ResearchReport, SavedSearch, SearchPlan, SearchResult};
use anyhow::{anyhow, Context, Result};
use duckdb::{Connection, Row};
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Clone)]
pub struct Db {
    path: PathBuf,
    runtime_path: Option<PathBuf>,
}

impl Db {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            runtime_path: None,
        }
    }

    /// Open the CompanyMaster sidecar DB while reading the existing Queria
    /// runtime DB through a read-only DuckDB attachment. The large runtime DB
    /// is never copied or modified by the GUI.
    pub fn with_runtime(path: PathBuf, runtime_path: PathBuf) -> Self {
        Self {
            path,
            runtime_path: Some(runtime_path),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn runtime_path(&self) -> Option<&Path> {
        self.runtime_path.as_deref()
    }

    pub(crate) fn connect(&self) -> Result<Connection> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(&self.path)
            .with_context(|| format!("DuckDBを開けません: {}", self.path.display()))?;
        conn.execute_batch("PRAGMA enable_progress_bar=false;")?;

        if let Some(runtime_path) = &self.runtime_path {
            attach_runtime(&conn, runtime_path)?;
        } else {
            conn.execute_batch(
                r#"
                CREATE TABLE IF NOT EXISTS companies (
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
                "#,
            )?;
        }

        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS industry_taxonomy (
              code VARCHAR PRIMARY KEY,
              name VARCHAR NOT NULL,
              level INTEGER NOT NULL,
              parent_code VARCHAR,
              revision VARCHAR,
              source_url VARCHAR
            );

            CREATE TABLE IF NOT EXISTS saved_searches (
              id VARCHAR PRIMARY KEY,
              name VARCHAR NOT NULL,
              query_text VARCHAR,
              plan_json VARCHAR NOT NULL,
              created_at TIMESTAMP DEFAULT current_timestamp
            );

            CREATE TABLE IF NOT EXISTS company_lists (
              id VARCHAR PRIMARY KEY,
              name VARCHAR UNIQUE NOT NULL,
              created_at TIMESTAMP DEFAULT current_timestamp
            );

            CREATE TABLE IF NOT EXISTS company_list_items (
              list_id VARCHAR NOT NULL,
              corporate_number VARCHAR NOT NULL,
              created_at TIMESTAMP DEFAULT current_timestamp,
              PRIMARY KEY(list_id, corporate_number)
            );

            CREATE TABLE IF NOT EXISTS research_reports (
              id VARCHAR PRIMARY KEY,
              corporate_number VARCHAR NOT NULL,
              company_name VARCHAR NOT NULL,
              thread_id VARCHAR NOT NULL,
              report_json VARCHAR NOT NULL,
              created_at TIMESTAMP DEFAULT current_timestamp
            );
            "#,
        )?;

        if self.runtime_path.is_some() {
            ensure_runtime_view(&conn)?;
        } else {
            conn.execute_batch(
                r#"
                CREATE INDEX IF NOT EXISTS idx_company_name ON companies(name);
                CREATE INDEX IF NOT EXISTS idx_company_prefecture ON companies(prefecture);
                CREATE INDEX IF NOT EXISTS idx_company_industry_code ON companies(industry_code);
                CREATE INDEX IF NOT EXISTS idx_company_employees ON companies(employees);
                "#,
            )?;
        }
        Ok(conn)
    }

    pub fn init(&self) -> Result<()> {
        let conn = self.connect()?;
        let count: i64 = conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?;
        if count == 0 {
            conn.execute_batch(
                r#"
                INSERT INTO companies (
                  corporate_number,name,prefecture,city,address,kind,industry_code,industry_name,
                  industry_source,employees,capital,established_year,website,representative,business_summary,
                  source_updated_at
                ) VALUES
                ('0100000000001','サンプルテクノロジー株式会社','東京都','千代田区','東京都千代田区丸の内1-1','301','3911','受託開発ソフトウェア業','sample',120,50000000,2017,'https://example.com','山田 太郎','業務システム・SaaSの開発を行うサンプルデータ。','sample'),
                ('0100000000002','サンプル食品株式会社','大阪府','大阪市','大阪府大阪市北区1-1','301','0972','生菓子製造業','sample',340,120000000,1998,NULL,'佐藤 花子','食品製造のサンプルデータ。','sample'),
                ('0100000000003','サンプル物流合同会社','愛知県','名古屋市','愛知県名古屋市中区1-1','305','4411','一般貨物自動車運送業','sample',45,10000000,2021,'https://example.org',NULL,'地域物流のサンプルデータ。','sample')
                ON CONFLICT DO NOTHING;
                "#,
            )?;
        }
        Ok(())
    }

    pub fn status(&self, duckdb_version: Option<String>) -> Result<DataStatus> {
        let conn = self.connect()?;
        let company_count: i64 = conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?;
        let taxonomy_count: i64 = conn.query_row("SELECT count(*) FROM industry_taxonomy", [], |r| r.get(0))?;
        let research_count: i64 = conn.query_row("SELECT count(*) FROM research_reports", [], |r| r.get(0))?;
        Ok(DataStatus {
            company_count: company_count.max(0) as u64,
            taxonomy_count: taxonomy_count.max(0) as u64,
            research_count: research_count.max(0) as u64,
            db_path: self.path.display().to_string(),
            duckdb_native: true,
            runtime_attached: self.runtime_path.is_some(),
            duckdb_version,
        })
    }

    pub fn search(&self, plan: SearchPlan, page: u32, page_size: u32) -> Result<SearchResult> {
        let start = Instant::now();
        let plan = plan.normalize();
        let page = page.max(1);
        let page_size = page_size.clamp(1, 500);
        let where_sql = build_where(&plan);
        let conn = self.connect()?;

        let count_sql = format!("SELECT count(*) FROM companies c WHERE {}", where_sql);
        let matching: i64 = conn.query_row(&count_sql, [], |r| r.get(0))?;
        let total = (matching.max(0) as u64).min(plan.limit as u64);
        let offset = ((page - 1) as u64 * page_size as u64).min(total);
        let remaining = total.saturating_sub(offset);
        let take = (page_size as u64).min(remaining);

        let query_sql = format!(
            r#"SELECT
              corporate_number,name,prefecture,city,address,kind,
              industry_code,industry_name,industry_source,
              inferred_industry_code,inferred_industry_name,inferred_industry_confidence,
              employees,capital,established_year,website,phone,representative,business_summary,source_updated_at
            FROM companies c
            WHERE {where_sql}
            ORDER BY name, corporate_number
            LIMIT {take} OFFSET {offset}"#
        );

        let mut stmt = conn.prepare(&query_sql)?;
        let mut rows = stmt.query([])?;
        let mut companies = Vec::with_capacity(take as usize);
        while let Some(row) = rows.next()? {
            companies.push(company_from_row(row)?);
        }
        Ok(SearchResult {
            rows: companies,
            total,
            page,
            page_size,
            elapsed_ms: start.elapsed().as_millis(),
        })
    }

    pub fn save_search(&self, name: &str, query: &str, plan: &SearchPlan) -> Result<()> {
        let conn = self.connect()?;
        let id = format!("search-{}", chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default());
        let json = serde_json::to_string(plan)?;
        conn.execute(
            "INSERT INTO saved_searches(id,name,query_text,plan_json) VALUES(?,?,?,?)",
            duckdb::params![id, name, query, json],
        )?;
        Ok(())
    }

    pub fn recent_searches(&self, limit: u32) -> Result<Vec<SavedSearch>> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare(
            "SELECT id,name,query_text,plan_json,CAST(created_at AS VARCHAR) FROM saved_searches ORDER BY created_at DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(duckdb::params![limit.clamp(1, 50)], |row| {
            let plan_json: String = row.get(3)?;
            let plan: SearchPlan = serde_json::from_str(&plan_json).unwrap_or_default();
            Ok(SavedSearch {
                id: row.get(0)?,
                name: row.get(1)?,
                query: row.get(2)?,
                plan,
                created_at: row.get(4)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows { out.push(row?); }
        Ok(out)
    }

    pub fn export_search_csv(&self, plan: SearchPlan, path: &Path) -> Result<u64> {
        let plan = plan.normalize();
        let where_sql = build_where(&plan);
        let safe_path = sql_quote(&path.to_string_lossy());
        let sql = format!(
            r#"COPY (
              SELECT corporate_number,name,prefecture,city,address,kind,industry_code,industry_name,
                     industry_source,inferred_industry_code,inferred_industry_name,inferred_industry_confidence,
                     employees,capital,established_year,website,phone,representative,business_summary,
                     source_updated_at
              FROM companies c WHERE {where_sql}
              ORDER BY name, corporate_number
              LIMIT {limit}
            ) TO {safe_path} (HEADER, DELIMITER ',')"#,
            limit = plan.limit,
        );
        let conn = self.connect()?;
        conn.execute_batch(&sql)?;
        let count_sql = format!("SELECT least(count(*), {}) FROM companies c WHERE {}", plan.limit, where_sql);
        let count: i64 = conn.query_row(&count_sql, [], |r| r.get(0))?;
        Ok(count.max(0) as u64)
    }

    pub fn add_to_list(&self, list_name: &str, corporate_numbers: &[String]) -> Result<u64> {
        let conn = self.connect()?;
        let list_name = list_name.trim();
        if list_name.is_empty() {
            return Err(anyhow!("リスト名が空です"));
        }
        let list_id = format!("list-{:x}", stable_hash(list_name));
        conn.execute(
            "INSERT INTO company_lists(id,name) VALUES(?,?) ON CONFLICT(name) DO NOTHING",
            duckdb::params![list_id.clone(), list_name],
        )?;
        let actual_id: String = conn.query_row(
            "SELECT id FROM company_lists WHERE name=?",
            duckdb::params![list_name],
            |r| r.get(0),
        )?;
        let mut added = 0u64;
        for number in corporate_numbers {
            let inserted = conn.execute(
                "INSERT INTO company_list_items(list_id,corporate_number) VALUES(?,?) ON CONFLICT DO NOTHING",
                duckdb::params![actual_id, number],
            )?;
            added += inserted as u64;
        }
        Ok(added)
    }

    pub fn add_search_to_list(&self, list_name: &str, plan: SearchPlan) -> Result<u64> {
        let conn = self.connect()?;
        let list_name = list_name.trim();
        if list_name.is_empty() { return Err(anyhow!("リスト名が空です")); }
        let list_id = format!("list-{:x}", stable_hash(list_name));
        conn.execute(
            "INSERT INTO company_lists(id,name) VALUES(?,?) ON CONFLICT(name) DO NOTHING",
            duckdb::params![list_id, list_name],
        )?;
        let actual_id: String = conn.query_row(
            "SELECT id FROM company_lists WHERE name=?",
            duckdb::params![list_name],
            |r| r.get(0),
        )?;
        let before: i64 = conn.query_row(
            "SELECT count(*) FROM company_list_items WHERE list_id=?",
            duckdb::params![actual_id.clone()],
            |r| r.get(0),
        )?;
        let plan = plan.normalize();
        let where_sql = build_where(&plan);
        let sql = format!(
            "INSERT OR IGNORE INTO company_list_items(list_id,corporate_number) SELECT {}, corporate_number FROM companies c WHERE {} ORDER BY name,corporate_number LIMIT {}",
            sql_quote(&actual_id), where_sql, plan.limit
        );
        conn.execute_batch(&sql)?;
        let after: i64 = conn.query_row(
            "SELECT count(*) FROM company_list_items WHERE list_id=?",
            duckdb::params![actual_id],
            |r| r.get(0),
        )?;
        Ok((after - before).max(0) as u64)
    }

    pub fn list_companies(&self, list_name: &str) -> Result<Vec<Company>> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare(
            r#"SELECT c.corporate_number,c.name,c.prefecture,c.city,c.address,c.kind,
               c.industry_code,c.industry_name,c.industry_source,c.inferred_industry_code,
               c.inferred_industry_name,c.inferred_industry_confidence,c.employees,c.capital,
               c.established_year,c.website,c.phone,c.representative,c.business_summary,c.source_updated_at
               FROM companies c
               JOIN company_list_items i ON c.corporate_number=i.corporate_number
               JOIN company_lists l ON l.id=i.list_id
               WHERE l.name=? ORDER BY c.name,c.corporate_number"#,
        )?;
        let mut rows = stmt.query(duckdb::params![list_name])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? { out.push(company_from_row(row)?); }
        Ok(out)
    }

    pub fn save_research(&self, report: &ResearchReport) -> Result<()> {
        let conn = self.connect()?;
        let id = format!("research-{}", chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default());
        let json = serde_json::to_string(report)?;
        conn.execute(
            "INSERT INTO research_reports(id,corporate_number,company_name,thread_id,report_json) VALUES(?,?,?,?,?)",
            duckdb::params![id, report.corporate_number, report.company_name, report.thread_id, json],
        )?;
        if self.runtime_path.is_none() {
            if let Some(guess) = &report.industry_guess {
            conn.execute(
                r#"UPDATE companies SET inferred_industry_code=?, inferred_industry_name=?,
                   inferred_industry_confidence=? WHERE corporate_number=?"#,
                duckdb::params![guess.code, guess.name, guess.confidence, report.corporate_number],
            )?;
            }
        }
        Ok(())
    }

    pub fn import_canonical_file(&self, path: &Path) -> Result<u64> {
        if self.runtime_path.is_some() {
            return Err(anyhow!(
                "QueriaランタイムDBはREAD_ONLY接続です。取り込みはCompanyMaster側のローカルDBで実行してください"
            ));
        }
        let ext = path.extension().and_then(|s| s.to_str()).unwrap_or("").to_ascii_lowercase();
        let conn = self.connect()?;
        let mut attached_alias: Option<&str> = None;
        let reader = match ext.as_str() {
            "parquet" => format!("read_parquet({})", sql_quote(&path.to_string_lossy())),
            "csv" => format!("read_csv_auto({}, header=true, all_varchar=false)", sql_quote(&path.to_string_lossy())),
            "json" | "jsonl" | "ndjson" => format!("read_json_auto({})", sql_quote(&path.to_string_lossy())),
            "duckdb" | "db" => {
                let alias = "native_import";
                conn.execute_batch(&format!(
                    "ATTACH {} AS {alias} (READ_ONLY);",
                    sql_quote(&path.to_string_lossy())
                ))?;
                attached_alias = Some(alias);
                format!("{alias}.main.companies")
            }
            _ => return Err(anyhow!("DuckDB / Parquet / CSV / JSONを取り込めます")),
        };
        let before: i64 = conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?;
        let sql = format!(
            r#"INSERT OR REPLACE INTO companies BY NAME
               SELECT
                 CAST(corporate_number AS VARCHAR) AS corporate_number,
                 CAST(name AS VARCHAR) AS name,
                 try_cast(prefecture AS VARCHAR) AS prefecture,
                 try_cast(city AS VARCHAR) AS city,
                 try_cast(address AS VARCHAR) AS address,
                 try_cast(kind AS VARCHAR) AS kind,
                 try_cast(industry_code AS VARCHAR) AS industry_code,
                 try_cast(industry_name AS VARCHAR) AS industry_name,
                 coalesce(try_cast(industry_source AS VARCHAR),'import') AS industry_source,
                 try_cast(inferred_industry_code AS VARCHAR) AS inferred_industry_code,
                 try_cast(inferred_industry_name AS VARCHAR) AS inferred_industry_name,
                 try_cast(inferred_industry_confidence AS DOUBLE) AS inferred_industry_confidence,
                 try_cast(employees AS BIGINT) AS employees,
                 try_cast(capital AS BIGINT) AS capital,
                 try_cast(established_year AS INTEGER) AS established_year,
                 try_cast(website AS VARCHAR) AS website,
                 try_cast(phone AS VARCHAR) AS phone,
                 try_cast(representative AS VARCHAR) AS representative,
                 try_cast(business_summary AS VARCHAR) AS business_summary,
                 NULL::VARCHAR AS business_items,
                 NULL::BIGINT AS subsidy_count,
                 NULL::DOUBLE AS subsidy_total_amount,
                 NULL::BIGINT AS procurement_count,
                 NULL::DOUBLE AS procurement_total_award,
                 NULL::INTEGER AS latest_fiscal_year,
                 NULL::DOUBLE AS latest_net_sales,
                 NULL::DOUBLE AS latest_ordinary_income,
                 NULL::DOUBLE AS latest_net_income,
                 NULL::DOUBLE AS latest_total_assets,
                 NULL::DOUBLE AS latest_net_assets,
                 coalesce(try_cast(source_updated_at AS VARCHAR), CAST(current_timestamp AS VARCHAR)) AS source_updated_at
               FROM {reader}
               WHERE corporate_number IS NOT NULL AND name IS NOT NULL"#
        );
        let exec_result = conn.execute_batch(&sql);
        if let Some(alias) = attached_alias {
            let _ = conn.execute_batch(&format!("DETACH {alias};"));
        }
        exec_result?;
        let after: i64 = conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?;
        Ok((after - before).max(0) as u64)
    }

    pub fn import_taxonomy_file(&self, path: &Path) -> Result<u64> {
        let conn = self.connect()?;
        let reader = match path.extension().and_then(|s| s.to_str()).unwrap_or("").to_ascii_lowercase().as_str() {
            "parquet" => format!("read_parquet({})", sql_quote(&path.to_string_lossy())),
            _ => format!("read_csv_auto({}, header=true, all_varchar=true)", sql_quote(&path.to_string_lossy())),
        };
        let sql = format!(
            r#"INSERT OR REPLACE INTO industry_taxonomy
               SELECT CAST(code AS VARCHAR),CAST(name AS VARCHAR),CAST(level AS INTEGER),
                      try_cast(parent_code AS VARCHAR),try_cast(revision AS VARCHAR),try_cast(source_url AS VARCHAR)
               FROM {reader}"#
        );
        conn.execute_batch(&sql)?;
        let count: i64 = conn.query_row("SELECT count(*) FROM industry_taxonomy", [], |r| r.get(0))?;
        Ok(count.max(0) as u64)
    }
}

fn attach_runtime(conn: &Connection, runtime_path: &Path) -> Result<()> {
    if !runtime_path.is_file() {
        return Err(anyhow!(
            "QueriaランタイムDBがありません: {}",
            runtime_path.display()
        ));
    }
    conn.execute_batch(&format!(
        "ATTACH {} AS queria_runtime (READ_ONLY);",
        sql_quote(&runtime_path.to_string_lossy())
    ))
    .with_context(|| format!("QueriaランタイムDBをREAD_ONLY接続できません: {}", runtime_path.display()))?;
    Ok(())
}

fn ensure_runtime_view(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE VIEW IF NOT EXISTS companies AS
        SELECT
          corporate_number,
          company_name AS name,
          prefecture_name AS prefecture,
          city_name AS city,
          full_address AS address,
          corporate_kind_code AS kind,
          concat_ws('|',
            nullif(jsic_major_code, ''),
            nullif(jsic_middle_codes, ''),
            nullif(jsic_codes_all_raw, '')
          ) AS industry_code,
          jsic_major_name AS industry_name,
          'queria_runtime' AS industry_source,
          NULL::VARCHAR AS inferred_industry_code,
          NULL::VARCHAR AS inferred_industry_name,
          NULL::DOUBLE AS inferred_industry_confidence,
          try_cast(employee_number AS BIGINT) AS employees,
          try_cast(capital_stock AS BIGINT) AS capital,
          try_cast(founding_year AS INTEGER) AS established_year,
          company_url AS website,
          NULL::VARCHAR AS phone,
          representative_name AS representative,
          business_summary,
          business_items_raw AS business_items,
          subsidy_count,
          subsidy_total_amount,
          procurement_count,
          procurement_total_award,
          try_cast(latest_fiscal_year AS INTEGER) AS latest_fiscal_year,
          try_cast(latest_net_sales AS DOUBLE) AS latest_net_sales,
          try_cast(latest_ordinary_income AS DOUBLE) AS latest_ordinary_income,
          try_cast(latest_net_income AS DOUBLE) AS latest_net_income,
          try_cast(latest_total_assets AS DOUBLE) AS latest_total_assets,
          try_cast(latest_net_assets AS DOUBLE) AS latest_net_assets,
          CAST(extracted_at AS VARCHAR) AS source_updated_at
        FROM queria_runtime.core.companies;
        "#,
    )?;
    Ok(())
}

fn company_from_row(row: &Row<'_>) -> duckdb::Result<Company> {
    Ok(Company {
        corporate_number: row.get(0)?, name: row.get(1)?, prefecture: row.get(2)?, city: row.get(3)?,
        address: row.get(4)?, kind: row.get(5)?, industry_code: row.get(6)?, industry_name: row.get(7)?,
        industry_source: row.get(8)?, inferred_industry_code: row.get(9)?, inferred_industry_name: row.get(10)?,
        inferred_industry_confidence: row.get(11)?, employees: row.get(12)?, capital: row.get(13)?,
        established_year: row.get(14)?, website: row.get(15)?, phone: row.get(16)?, representative: row.get(17)?,
        business_summary: row.get(18)?, source_updated_at: row.get(19)?,
    })
}

fn build_where(plan: &SearchPlan) -> String {
    let mut c: Vec<String> = vec!["1=1".into()];
    if !plan.prefectures.is_empty() { c.push(in_list("c.prefecture", &plan.prefectures)); }
    if !plan.cities.is_empty() { c.push(in_list("c.city", &plan.cities)); }
    if !plan.company_kinds.is_empty() { c.push(in_list("c.kind", &plan.company_kinds)); }
    if !plan.industry_codes.is_empty() {
        let terms: Vec<String> = plan.industry_codes.iter().map(|x| {
            let q = escape_like(x);
            format!("(c.industry_code LIKE {} ESCAPE '\\\\' OR c.industry_code LIKE {} ESCAPE '\\\\' OR c.industry_code LIKE {} ESCAPE '\\\\' OR c.inferred_industry_code LIKE {} ESCAPE '\\\\' OR c.business_items LIKE {} ESCAPE '\\\\')",
                sql_quote(&format!("{}%", q)),
                sql_quote(&format!("%|{}%", q)),
                sql_quote(&format!("%{}%", q)),
                sql_quote(&format!("{}%", q)),
                sql_quote(&format!("%{}%", q)))
        }).collect();
        c.push(format!("({})", terms.join(" OR ")));
    }
    for term in &plan.industry_terms {
        c.push(contains_any_columns(term, &["c.industry_name","c.inferred_industry_name","c.business_summary","c.business_items"]));
    }
    if let Some(v) = plan.min_employees { c.push(format!("c.employees >= {}", v.max(0))); }
    if let Some(v) = plan.max_employees { c.push(format!("c.employees <= {}", v.max(0))); }
    if let Some(v) = plan.min_capital { c.push(format!("c.capital >= {}", v.max(0))); }
    if let Some(v) = plan.max_capital { c.push(format!("c.capital <= {}", v.max(0))); }
    if let Some(v) = plan.established_from { c.push(format!("c.established_year >= {}", v.clamp(1800, 2200))); }
    if let Some(v) = plan.established_to { c.push(format!("c.established_year <= {}", v.clamp(1800, 2200))); }
    if plan.website_required == Some(true) { c.push("c.website IS NOT NULL AND trim(c.website) <> ''".into()); }
    if plan.website_required == Some(false) { c.push("(c.website IS NULL OR trim(c.website) = '')".into()); }
    for term in &plan.keyword_all {
        c.push(contains_any_columns(term, &["c.name","c.address","c.business_summary","c.industry_name","c.inferred_industry_name","c.website"]));
    }
    if !plan.keyword_any.is_empty() {
        let items: Vec<String> = plan.keyword_any.iter().map(|t| contains_any_columns(t, &["c.name","c.address","c.business_summary","c.industry_name","c.inferred_industry_name","c.website"])).collect();
        c.push(format!("({})", items.join(" OR ")));
    }
    c.join(" AND ")
}

fn contains_any_columns(term: &str, columns: &[&str]) -> String {
    let pattern = sql_quote(&format!("%{}%", escape_like(term)));
    let checks: Vec<String> = columns.iter().map(|col| format!("coalesce({col},'') ILIKE {pattern} ESCAPE '\\\\'" )).collect();
    format!("({})", checks.join(" OR "))
}

fn in_list(column: &str, values: &[String]) -> String {
    let values = values.iter().map(|v| sql_quote(v)).collect::<Vec<_>>().join(",");
    format!("{column} IN ({values})")
}

fn sql_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn escape_like(value: &str) -> String {
    value.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_")
}

fn stable_hash(value: &str) -> u64 {
    let mut hash = 1469598103934665603u64;
    for b in value.as_bytes() { hash ^= *b as u64; hash = hash.wrapping_mul(1099511628211); }
    hash
}
