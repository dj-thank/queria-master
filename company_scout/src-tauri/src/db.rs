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

#[derive(Debug, Clone)]
pub struct PhoneCandidateRecord {
    pub phone: String,
    pub phone_type: String,
    pub source_url: String,
    pub evidence_text: String,
    pub confidence: f64,
    pub observed_at: String,
    pub status: String,
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
                  entity_key VARCHAR,
                  fuma_id VARCHAR,
                  source_kind VARCHAR,
                  name VARCHAR NOT NULL,
                  prefecture VARCHAR,
                  city VARCHAR,
                  address VARCHAR,
                  kind VARCHAR,
                  industry_code VARCHAR,
                  industry_name VARCHAR,
                  industry_source VARCHAR,
                  industry_middle_code VARCHAR,
                  industry_middle_name VARCHAR,
                  industry_small_code VARCHAR,
                  industry_small_name VARCHAR,
                  industry_detail_code VARCHAR,
                  industry_detail_name VARCHAR,
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
                  source_updated_at VARCHAR,
                  phone_type VARCHAR,
                  phone_source_url VARCHAR,
                  phone_confidence DOUBLE,
                  phone_evidence_text VARCHAR,
                  phone_observed_at VARCHAR,
                  phone_status VARCHAR
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

            CREATE TABLE IF NOT EXISTS company_contact_overrides (
              corporate_number VARCHAR PRIMARY KEY,
              phone VARCHAR,
              source_url VARCHAR NOT NULL,
              evidence_text VARCHAR,
              phone_type VARCHAR,
              phone_confidence DOUBLE,
              phone_observed_at VARCHAR,
              phone_status VARCHAR,
              collected_at TIMESTAMP DEFAULT current_timestamp
            );
            CREATE TABLE IF NOT EXISTS company_phone_candidates (
              corporate_number VARCHAR NOT NULL,
              phone VARCHAR NOT NULL,
              phone_type VARCHAR,
              source_url VARCHAR NOT NULL,
              evidence_text VARCHAR,
              phone_confidence DOUBLE,
              phone_observed_at VARCHAR,
              phone_status VARCHAR,
              collected_at TIMESTAMP DEFAULT current_timestamp,
              PRIMARY KEY(corporate_number, phone, source_url)
            );
            CREATE TABLE IF NOT EXISTS company_phone_collection_state (
              entity_key VARCHAR PRIMARY KEY,
              website VARCHAR,
              state VARCHAR NOT NULL,
              last_completed_at VARCHAR,
              last_error VARCHAR
            );
            ALTER TABLE company_contact_overrides ADD COLUMN IF NOT EXISTS phone_type VARCHAR;
            ALTER TABLE company_contact_overrides ADD COLUMN IF NOT EXISTS phone_confidence DOUBLE;
            ALTER TABLE company_contact_overrides ADD COLUMN IF NOT EXISTS phone_observed_at VARCHAR;
            ALTER TABLE company_contact_overrides ADD COLUMN IF NOT EXISTS phone_status VARCHAR;
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
        let g_runtime = self.runtime_path.is_some() && is_g_fuma_runtime(&conn);
        let company_count: i64 = if self.runtime_path.is_some() {
            let table = if g_runtime { "queria_runtime.core.g_companies" } else { "queria_runtime.core.companies" };
            conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?
        } else {
            conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?
        };
        let taxonomy_count: i64 = if g_runtime {
            conn.query_row("SELECT count(*) FROM queria_runtime.meta.industry_taxonomy", [], |r| r.get(0))?
        } else {
            conn.query_row("SELECT count(*) FROM industry_taxonomy", [], |r| r.get(0))?
        };
        // Do not scan the aggregate search view just to paint the sidebar.
        // The source tables provide the same coverage figures much faster and
        // avoid delaying bootstrap on a 5.8M-row snapshot.
        let coverage: (i64, i64, i64, i64, i64, i64) = if g_runtime {
            conn.query_row(
                "SELECT
                   count(*) FILTER (WHERE nullif(trim(industry_code), '') IS NOT NULL),
                   count(*) FILTER (WHERE employees IS NOT NULL),
                   count(*) FILTER (WHERE capital IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(website), '') IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(phone), '') IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(address), '') IS NOT NULL)
                 FROM queria_runtime.core.g_companies",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
            )?
        } else if self.runtime_path.is_some() {
            conn.query_row(
                "SELECT
                   (SELECT count(DISTINCT corporate_number) FROM queria_runtime.core.company_industries),
                   count(*) FILTER (WHERE employee_number IS NOT NULL),
                   count(*) FILTER (WHERE capital_stock IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(company_url), '') IS NOT NULL),
                   (SELECT count(*) FROM queria_runtime.search.company_documents WHERE nullif(trim(phone), '') IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(full_address), '') IS NOT NULL)
                 FROM queria_runtime.core.companies",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
            )?
        } else {
            conn.query_row(
                "SELECT
                   count(*) FILTER (WHERE nullif(trim(industry_code), '') IS NOT NULL),
                   count(*) FILTER (WHERE employees IS NOT NULL),
                   count(*) FILTER (WHERE capital IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(website), '') IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(phone), '') IS NOT NULL),
                   count(*) FILTER (WHERE nullif(trim(address), '') IS NOT NULL)
                 FROM companies",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
            )?
        };
        let research_count: i64 = conn.query_row("SELECT count(*) FROM research_reports", [], |r| r.get(0))?;
        Ok(DataStatus {
            company_count: company_count.max(0) as u64,
            taxonomy_count: taxonomy_count.max(0) as u64,
            industry_count: coverage.0.max(0) as u64,
            employee_count: coverage.1.max(0) as u64,
            capital_count: coverage.2.max(0) as u64,
            website_count: coverage.3.max(0) as u64,
            phone_count: coverage.4.max(0) as u64,
            address_count: coverage.5.max(0) as u64,
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
        // The desktop UI requests up to 30,000 rows for its virtualized list.
        // Only the visible slice is mounted in React, so this keeps the
        // result transfer fast without creating 30,000 DOM nodes.
        let page_size = page_size.clamp(1, 50_000);
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
              corporate_number,entity_key,fuma_id,source_kind,name,prefecture,city,address,kind,
              industry_code,industry_name,industry_source,
              industry_middle_code,industry_middle_name,industry_small_code,industry_small_name,
              industry_detail_code,industry_detail_name,
              inferred_industry_code,inferred_industry_name,inferred_industry_confidence,
              employees,capital,established_year,website,phone,representative,business_summary,source_updated_at,
              phone_type,phone_source_url,phone_confidence,phone_evidence_text,phone_observed_at,phone_status
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
              SELECT corporate_number,entity_key,fuma_id,source_kind,name,prefecture,city,address,kind,industry_code,industry_name,
                     industry_source,industry_middle_code,industry_middle_name,industry_small_code,industry_small_name,
                     industry_detail_code,industry_detail_name,inferred_industry_code,inferred_industry_name,inferred_industry_confidence,
                     employees,capital,established_year,website,phone,representative,business_summary,source_updated_at,
                     phone_type,phone_source_url,phone_confidence,phone_evidence_text,phone_observed_at,phone_status
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

    pub fn export_search_xlsx(&self, plan: SearchPlan, path: &Path) -> Result<u64> {
        let plan = plan.normalize();
        if plan.limit > 1_048_575 {
            return Err(anyhow!("XLSXは1シートあたり最大1,048,575社です。全件出力はCSVを使用してください"));
        }
        let where_sql = build_where(&plan);
        let limit = plan.limit;
        let query_sql = format!(
            r#"SELECT
              corporate_number,entity_key,fuma_id,source_kind,name,prefecture,city,address,kind,
              industry_code,industry_name,industry_source,
              industry_middle_code,industry_middle_name,industry_small_code,industry_small_name,
              industry_detail_code,industry_detail_name,
              inferred_industry_code,inferred_industry_name,inferred_industry_confidence,
              employees,capital,established_year,website,phone,representative,business_summary,source_updated_at,
              phone_type,phone_source_url,phone_confidence,phone_evidence_text,phone_observed_at,phone_status
            FROM companies c
            WHERE {where_sql}
            ORDER BY name, corporate_number
            LIMIT {limit}"#
        );
        let count_sql = format!("SELECT count(*) FROM ({query_sql}) export_rows");
        let conn = self.connect()?;
        let count: i64 = conn.query_row(&count_sql, [], |r| r.get(0))?;
        let mut stmt = conn.prepare(&query_sql)?;
        let mut rows = stmt.query([])?;

        let mut workbook = rust_xlsxwriter::Workbook::new();
        // Keep the workbook API compatible with the pinned rust_xlsxwriter release.
        // The Excel row limit below still prevents an unbounded allocation.
        let worksheet = workbook.add_worksheet();
        let headers = [
            "法人番号", "Entity Key", "FUMA_ID", "データソース", "会社名", "都道府県", "市区町村", "住所", "法人種別", "業種コード", "業種名",
            "業種ソース", "中分類コード", "中分類名", "小分類コード", "小分類名", "細分類コード", "細分類名",
            "AI推定業種コード", "AI推定業種名", "AI推定信頼度", "従業員数", "資本金", "設立年", "Webサイト", "電話", "代表者", "事業概要", "更新日時",
            "電話用途", "電話根拠URL", "電話信頼度", "電話証拠", "電話取得日時", "電話状態",
        ];
        for (column, header) in headers.iter().enumerate() {
            worksheet.write_string(0, column as u16, *header)?;
        }
        let mut output_row: u32 = 1;
        while let Some(row) = rows.next()? {
            let company = company_from_row(row)?;
            let values = [
                company.corporate_number,
                company.entity_key.unwrap_or_default(),
                company.fuma_id.unwrap_or_default(),
                company.source_kind.unwrap_or_default(),
                company.name,
                company.prefecture.unwrap_or_default(),
                company.city.unwrap_or_default(),
                company.address.unwrap_or_default(),
                company.kind.unwrap_or_default(),
                company.industry_code.unwrap_or_default(),
                company.industry_name.unwrap_or_default(),
                company.industry_source.unwrap_or_default(),
                company.industry_middle_code.unwrap_or_default(),
                company.industry_middle_name.unwrap_or_default(),
                company.industry_small_code.unwrap_or_default(),
                company.industry_small_name.unwrap_or_default(),
                company.industry_detail_code.unwrap_or_default(),
                company.industry_detail_name.unwrap_or_default(),
                company.inferred_industry_code.unwrap_or_default(),
                company.inferred_industry_name.unwrap_or_default(),
                company.inferred_industry_confidence.map(|v| v.to_string()).unwrap_or_default(),
                company.employees.map(|v| v.to_string()).unwrap_or_default(),
                company.capital.map(|v| v.to_string()).unwrap_or_default(),
                company.established_year.map(|v| v.to_string()).unwrap_or_default(),
                company.website.unwrap_or_default(),
                company.phone.unwrap_or_default(),
                company.representative.unwrap_or_default(),
                company.business_summary.unwrap_or_default(),
                company.source_updated_at.unwrap_or_default(),
                company.phone_type.unwrap_or_default(),
                company.phone_source_url.unwrap_or_default(),
                company.phone_confidence.map(|v| v.to_string()).unwrap_or_default(),
                company.phone_evidence_text.unwrap_or_default(),
                company.phone_observed_at.unwrap_or_default(),
                company.phone_status.unwrap_or_default(),
            ];
            for (column, value) in values.iter().enumerate() {
                worksheet.write_string(output_row, column as u16, value.as_str())?;
            }
            output_row = output_row.saturating_add(1);
        }
        workbook.save(path).with_context(|| format!("XLSXを書き出せません: {}", path.display()))?;
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
            r#"SELECT c.corporate_number,c.entity_key,c.fuma_id,c.source_kind,c.name,c.prefecture,c.city,c.address,c.kind,
               c.industry_code,c.industry_name,c.industry_source,c.industry_middle_code,c.industry_middle_name,
               c.industry_small_code,c.industry_small_name,c.industry_detail_code,c.industry_detail_name,
               c.inferred_industry_code,c.inferred_industry_name,c.inferred_industry_confidence,c.employees,c.capital,
               c.established_year,c.website,c.phone,c.representative,c.business_summary,c.source_updated_at,
               c.phone_type,c.phone_source_url,c.phone_confidence,c.phone_evidence_text,c.phone_observed_at,c.phone_status
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

fn is_g_fuma_runtime(conn: &Connection) -> bool {
    conn.query_row("SELECT count(*) FROM queria_runtime.core.g_companies", [], |r| r.get::<_, i64>(0)).is_ok()
}

fn ensure_runtime_view(conn: &Connection) -> Result<()> {
    if is_g_fuma_runtime(conn) {
        conn.execute_batch(
            r#"
            CREATE OR REPLACE VIEW companies AS
            SELECT
              coalesce(nullif(base.corporate_number, ''), base.entity_key) AS corporate_number,
              base.entity_key, base.fuma_id, base.source_kind,
              base.name, base.prefecture, base.city, base.address, base.kind,
              base.industry_code, base.industry_name, base.industry_source,
              base.industry_middle_code, base.industry_middle_name,
              base.industry_small_code, base.industry_small_name,
              base.industry_detail_code, base.industry_detail_name,
              NULL::VARCHAR AS inferred_industry_code,
              NULL::VARCHAR AS inferred_industry_name,
              NULL::DOUBLE AS inferred_industry_confidence,
              base.employees, base.capital, base.established_year, base.website,
              coalesce(local_contacts.phone, base.phone) AS phone,
              base.representative, base.business_summary, base.business_summary AS business_items, base.source_updated_at,
              coalesce(local_contacts.phone_type, base.phone_type) AS phone_type,
              coalesce(local_contacts.source_url, base.phone_source_url) AS phone_source_url,
              coalesce(local_contacts.phone_confidence, base.phone_confidence) AS phone_confidence,
              coalesce(local_contacts.evidence_text, base.phone_evidence_text) AS phone_evidence_text,
              coalesce(local_contacts.phone_observed_at, base.phone_observed_at) AS phone_observed_at,
              coalesce(local_contacts.phone_status, local_state.state, base.phone_status) AS phone_status
            FROM queria_runtime.core.g_companies AS base
            LEFT JOIN company_contact_overrides AS local_contacts
              ON local_contacts.corporate_number = base.entity_key
            LEFT JOIN company_phone_collection_state AS local_state
              ON local_state.entity_key = base.entity_key;
            "#,
        )?;
        return Ok(());
    }
    conn.execute_batch(
        r#"
        CREATE OR REPLACE VIEW companies AS
        WITH industry_agg AS (
          -- The one-row-per-company core snapshot only carries JSIC for a
          -- small subset. company_industries is the normalized official
          -- relation and contains the additional 149k classified companies.
          SELECT
            corporate_number,
            string_agg(DISTINCT nullif(jsic_code, ''), '|') AS codes,
            string_agg(DISTINCT nullif(jsic_major_name, ''), ' / ') AS major_names,
            string_agg(DISTINCT nullif(jsic_middle_name, ''), ' / ') AS middle_names,
            string_agg(DISTINCT nullif(jsic_small_name, ''), ' / ') AS small_names
          FROM queria_runtime.core.company_industries
          WHERE corporate_number IS NOT NULL
          GROUP BY corporate_number
        )
        SELECT
          base.corporate_number,
          base.corporate_number AS entity_key,
          NULL::VARCHAR AS fuma_id,
          'national' AS source_kind,
          base.company_name AS name,
          coalesce(nullif(base.prefecture_name, ''), contacts.resolved_prefecture_name) AS prefecture,
          coalesce(nullif(base.city_name, ''), contacts.resolved_city_name) AS city,
          coalesce(nullif(base.full_address, ''), contacts.resolved_address) AS address,
          base.corporate_kind_code AS kind,
          concat_ws('|',
            nullif(base.jsic_major_code, ''),
            nullif(base.jsic_middle_codes, ''),
            nullif(base.jsic_codes_all_raw, ''),
            nullif(industry_agg.codes, '')
          ) AS industry_code,
          nullif(concat_ws(' / ',
            nullif(base.jsic_major_name, ''),
            nullif(industry_agg.major_names, ''),
            nullif(industry_agg.middle_names, ''),
            nullif(industry_agg.small_names, '')
          ), '') AS industry_name,
          CASE WHEN industry_agg.codes IS NOT NULL THEN 'queria_runtime/jsic' ELSE 'queria_runtime' END AS industry_source,
          NULL::VARCHAR AS industry_middle_code,
          NULL::VARCHAR AS industry_middle_name,
          NULL::VARCHAR AS industry_small_code,
          NULL::VARCHAR AS industry_small_name,
          NULL::VARCHAR AS industry_detail_code,
          NULL::VARCHAR AS industry_detail_name,
          NULL::VARCHAR AS inferred_industry_code,
          NULL::VARCHAR AS inferred_industry_name,
          NULL::DOUBLE AS inferred_industry_confidence,
          coalesce(try_cast(base.employee_number AS BIGINT), try_cast(contacts.employee_number AS BIGINT)) AS employees,
          coalesce(try_cast(base.capital_stock AS BIGINT), try_cast(contacts.capital_stock AS BIGINT)) AS capital,
          coalesce(try_cast(base.founding_year AS INTEGER), try_cast(contacts.founding_year AS INTEGER)) AS established_year,
          coalesce(nullif(base.company_url, ''), nullif(contacts.effective_company_url, ''), nullif(contacts.company_url, '')) AS website,
          coalesce(local_contacts.phone, contacts.phone) AS phone,
          coalesce(nullif(base.representative_name, ''), contacts.representative_name) AS representative,
          coalesce(nullif(base.business_summary, ''), contacts.business_summary) AS business_summary,
          base.business_items_raw AS business_items,
          CAST(base.extracted_at AS VARCHAR) AS source_updated_at,
          local_contacts.phone_type AS phone_type,
          local_contacts.source_url AS phone_source_url,
          local_contacts.phone_confidence AS phone_confidence,
          local_contacts.evidence_text AS phone_evidence_text,
          local_contacts.phone_observed_at AS phone_observed_at,
          coalesce(local_contacts.phone_status, local_state.state) AS phone_status
        FROM queria_runtime.core.companies AS base
        LEFT JOIN industry_agg
          ON industry_agg.corporate_number = base.corporate_number
        LEFT JOIN queria_runtime.search.company_documents AS contacts
          ON contacts.corporate_number = base.corporate_number
        LEFT JOIN company_contact_overrides AS local_contacts
          ON local_contacts.corporate_number = base.corporate_number
        LEFT JOIN company_phone_collection_state AS local_state
          ON local_state.entity_key = base.corporate_number;
        "#,
    )?;
    Ok(())
}

impl Db {
    pub fn save_phone_candidates(
        &self,
        entity_key: &str,
        website: &str,
        candidates: &[PhoneCandidateRecord],
        state: &str,
    ) -> Result<()> {
        let conn = self.connect()?;
        let completed_at = candidates.first().map(|item| item.observed_at.as_str());
        conn.execute(
            "INSERT OR REPLACE INTO company_phone_collection_state(entity_key,website,state,last_completed_at,last_error) VALUES(?,?,?,?,NULL)",
            duckdb::params![entity_key, website, state, completed_at],
        )?;
        for candidate in candidates {
            conn.execute(
                "INSERT OR REPLACE INTO company_phone_candidates(corporate_number,phone,phone_type,source_url,evidence_text,phone_confidence,phone_observed_at,phone_status) VALUES(?,?,?,?,?,?,?,?)",
                duckdb::params![entity_key, candidate.phone, candidate.phone_type, candidate.source_url, candidate.evidence_text, candidate.confidence, candidate.observed_at, candidate.status],
            )?;
        }
        if let Some(best) = candidates.first() {
            conn.execute(
                "INSERT OR REPLACE INTO company_contact_overrides(corporate_number,phone,source_url,evidence_text,phone_type,phone_confidence,phone_observed_at,phone_status) VALUES(?,?,?,?,?,?,?,?)",
                duckdb::params![entity_key, best.phone, best.source_url, best.evidence_text, best.phone_type, best.confidence, best.observed_at, best.status],
            )?;
        }
        Ok(())
    }
}

fn company_from_row(row: &Row<'_>) -> duckdb::Result<Company> {
    Ok(Company {
        corporate_number: row.get(0)?, entity_key: row.get(1)?, fuma_id: row.get(2)?, source_kind: row.get(3)?,
        name: row.get(4)?, prefecture: row.get(5)?, city: row.get(6)?, address: row.get(7)?, kind: row.get(8)?,
        industry_code: row.get(9)?, industry_name: row.get(10)?, industry_source: row.get(11)?,
        industry_middle_code: row.get(12)?, industry_middle_name: row.get(13)?,
        industry_small_code: row.get(14)?, industry_small_name: row.get(15)?,
        industry_detail_code: row.get(16)?, industry_detail_name: row.get(17)?,
        inferred_industry_code: row.get(18)?, inferred_industry_name: row.get(19)?,
        inferred_industry_confidence: row.get(20)?, employees: row.get(21)?, capital: row.get(22)?,
        established_year: row.get(23)?, website: row.get(24)?, phone: row.get(25)?, representative: row.get(26)?,
        business_summary: row.get(27)?, source_updated_at: row.get(28)?, phone_type: row.get(29)?,
        phone_source_url: row.get(30)?, phone_confidence: row.get(31)?, phone_evidence_text: row.get(32)?,
        phone_observed_at: row.get(33)?, phone_status: row.get(34)?,
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
            let numeric_major_variant = if x.chars().all(|ch| ch.is_ascii_digit()) {
                let prefixed = escape_like(&format!("G{}", x));
                format!(" OR c.industry_code ILIKE {} ESCAPE '\\' OR c.industry_code ILIKE {} ESCAPE '\\'",
                    sql_quote(&format!("%{}%", prefixed)),
                    sql_quote(&format!("%|{}%", prefixed)))
            } else { String::new() };
            format!("(c.industry_code ILIKE {} ESCAPE '\\' OR c.industry_code ILIKE {} ESCAPE '\\' OR c.industry_code ILIKE {} ESCAPE '\\'{} OR c.inferred_industry_code ILIKE {} ESCAPE '\\' OR c.business_items ILIKE {} ESCAPE '\\')",
                sql_quote(&format!("{}%", q)),
                sql_quote(&format!("%|{}%", q)),
                sql_quote(&format!("%{}%", q)),
                numeric_major_variant,
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
    let checks: Vec<String> = columns.iter().map(|col| format!("coalesce({col},'') ILIKE {pattern} ESCAPE '\\'" )).collect();
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
