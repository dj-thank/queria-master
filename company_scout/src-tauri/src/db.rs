use crate::models::{Company, DataStatus, ResearchReport, SavedSearch, SearchPlan, SearchResult};
use anyhow::{anyhow, Context, Result};
use duckdb::{Connection, Row};
use rusqlite::types::Value as SqliteValue;
use rusqlite::{params_from_iter, Connection as SqliteConnection, OpenFlags};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Instant;
use unicode_normalization::UnicodeNormalization;

const SEARCH_INDEX_VERSION: &str = "8";
const MAX_PAGE_SIZE: u32 = 100;
const MAX_SEARCH_TEXT_CHARS: usize = 256;

#[derive(Debug)]
struct SearchIndexState {
    available: bool,
    path: Option<PathBuf>,
    status: String,
    row_count: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SortBy {
    Relevance,
    Name,
    Employees,
    Capital,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SortDirection {
    Asc,
    Desc,
}

#[derive(Debug, Clone, Copy)]
struct SortSpec {
    by: SortBy,
    direction: SortDirection,
}

#[derive(Clone)]
pub struct Db {
    path: PathBuf,
    runtime_path: Option<PathBuf>,
    search_index_path: Option<PathBuf>,
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
            search_index_path: None,
        }
    }

    /// Open the CompanyMaster sidecar DB while reading the existing Queria
    /// runtime DB through a read-only DuckDB attachment. The large runtime DB
    /// is never copied or modified by the GUI.
    pub fn with_runtime(path: PathBuf, runtime_path: PathBuf) -> Self {
        Self {
            path,
            runtime_path: Some(runtime_path),
            search_index_path: None,
        }
    }

    pub fn with_search_index(mut self, search_index_path: Option<PathBuf>) -> Self {
        self.search_index_path = search_index_path;
        self
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
            migrate_company_relation(&conn, true)?;
        } else {
            migrate_company_relation(&conn, false)?;
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
        let search_index = self.inspect_search_index(Some(&conn));
        let company_count: i64 = if self.runtime_path.is_some() {
            let table = if g_runtime {
                "queria_runtime.core.g_companies"
            } else {
                "queria_runtime.core.companies"
            };
            conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?
        } else {
            conn.query_row("SELECT count(*) FROM companies", [], |r| r.get(0))?
        };
        let taxonomy_count: i64 = if g_runtime {
            conn.query_row(
                "SELECT count(*) FROM queria_runtime.meta.industry_taxonomy",
                [],
                |r| r.get(0),
            )?
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
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                    ))
                },
            )?
        } else if self.runtime_path.is_some() {
            conn.query_row(
                "SELECT
                   (SELECT count(DISTINCT corporate_number) FROM queria_runtime.core.company_industries),
                   count(*) FILTER (WHERE employee_number IS NOT NULL),
                   count(*) FILTER (WHERE capital_stock IS NOT NULL),
                   (SELECT count(*) FROM queria_runtime.search.company_documents WHERE nullif(trim(effective_company_url), '') IS NOT NULL),
                   (SELECT count(*) FROM queria_runtime.search.company_documents WHERE nullif(trim(phone), '') IS NOT NULL),
                   (SELECT count(*) FROM queria_runtime.search.company_documents WHERE nullif(trim(resolved_address), '') IS NOT NULL)
                 FROM queria_runtime.core.companies",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
            )?
        } else {
            conn.query_row(
                "SELECT
                   count(*) FILTER (WHERE nullif(trim(industry_code), '') IS NOT NULL OR nullif(trim(business_items), '') IS NOT NULL),
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
        let research_count: i64 =
            conn.query_row("SELECT count(*) FROM research_reports", [], |r| r.get(0))?;
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
            search_index_available: search_index.available,
            search_index_path: search_index
                .path
                .as_ref()
                .map(|path| path.display().to_string()),
            search_index_status: Some(search_index.status),
            search_index_row_count: search_index.row_count,
        })
    }

    pub fn search(&self, plan: SearchPlan, page: u32, page_size: u32) -> Result<SearchResult> {
        let start = Instant::now();
        let plan = plan.normalize();
        validate_search_text(&plan)?;
        let sort = validate_sort(&plan)?;
        let page = page.max(1);
        // Keep each IPC payload bounded and let the frontend page through an
        // exact result count. Large exports use the dedicated export methods.
        let page_size = page_size.clamp(1, MAX_PAGE_SIZE);
        let conn = self.connect()?;
        let mut warnings = Vec::new();

        let sqlite_capability = sqlite_supported(&plan)
            .map_err(str::to_string)
            .and_then(|_| {
                let searches_overridden_phone = plan.phone_required.is_some()
                    || plan.text.as_deref().is_some_and(|text| {
                        corporate_number_query(text).is_none() && phone_like_query(text)
                    });
                if searches_overridden_phone {
                    let override_count: i64 = conn
                        .query_row(
                            "SELECT count(*) FROM company_contact_overrides",
                            [],
                            |row| row.get(0),
                        )
                        .map_err(|error| error.to_string())?;
                    if override_count > 0 {
                        return Err(
                            "ローカル電話番号の上書きを含めて検索するためDuckDBが必要です"
                                .to_string(),
                        );
                    }
                }
                Ok(())
            });
        if let Err(reason) = sqlite_capability {
            if self.search_index_path.is_some() {
                warnings.push(format!(
                    "SQLite検索索引を使用せずDuckDBで検索しました: {reason}"
                ));
            }
        } else {
            let index_state = self.inspect_search_index(Some(&conn));
            if index_state.available {
                if let Some(index_path) = index_state.path.as_deref() {
                    match self.search_sqlite_index(index_path, &conn, &plan, sort, page, page_size)
                    {
                        Ok((rows, total)) => {
                            return Ok(SearchResult {
                                rows,
                                total,
                                page,
                                page_size,
                                elapsed_ms: start.elapsed().as_millis(),
                                engine: "sqlite_fts5".to_string(),
                                warnings,
                            });
                        }
                        Err(error) => warnings.push(format!(
                            "SQLite検索索引で検索できなかったためDuckDBへ切り替えました: {error:#}"
                        )),
                    }
                }
            } else if self.search_index_path.is_some() {
                warnings.push(format!(
                    "SQLite検索索引を使用できないためDuckDBへ切り替えました: {}",
                    index_state.status
                ));
            }
        }

        self.search_duckdb(&conn, &plan, sort, page, page_size, start, warnings)
    }

    fn search_duckdb(
        &self,
        conn: &Connection,
        plan: &SearchPlan,
        sort: SortSpec,
        page: u32,
        page_size: u32,
        start: Instant,
        warnings: Vec<String>,
    ) -> Result<SearchResult> {
        let where_sql = build_where(plan);

        let count_sql = format!("SELECT count(*) FROM companies c WHERE {}", where_sql);
        let matching: i64 = conn.query_row(&count_sql, [], |r| r.get(0))?;
        let total = matching.max(0) as u64;
        let offset = (page.saturating_sub(1) as u64 * page_size as u64).min(total);
        let remaining = total.saturating_sub(offset);
        let take = (page_size as u64).min(remaining);
        let order_by = duckdb_order_by(sort, plan.text.as_deref());

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
            ORDER BY {order_by}
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
            engine: "duckdb".to_string(),
            warnings,
        })
    }

    fn inspect_search_index(&self, runtime_conn: Option<&Connection>) -> SearchIndexState {
        let Some(configured_path) = self.search_index_path.as_ref() else {
            return SearchIndexState {
                available: false,
                path: None,
                status: "not_found".to_string(),
                row_count: None,
            };
        };
        let path = match configured_path.canonicalize() {
            Ok(path) if path.is_file() => path,
            Ok(path) => {
                return SearchIndexState {
                    available: false,
                    path: Some(path),
                    status: "not_a_file".to_string(),
                    row_count: None,
                }
            }
            Err(error) => {
                return SearchIndexState {
                    available: false,
                    path: Some(configured_path.clone()),
                    status: format!("unreadable: {error}"),
                    row_count: None,
                }
            }
        };

        match self.validate_search_index(&path, runtime_conn) {
            Ok(row_count) => SearchIndexState {
                available: true,
                path: Some(path),
                status: "ready".to_string(),
                row_count: Some(row_count),
            },
            Err(error) => SearchIndexState {
                available: false,
                path: Some(path),
                status: format!("incompatible: {error:#}"),
                row_count: None,
            },
        }
    }

    fn validate_search_index(&self, path: &Path, runtime_conn: Option<&Connection>) -> Result<u64> {
        if self.runtime_path.is_none() {
            return Err(anyhow!(
                "検索索引を照合するQueriaランタイムDBが接続されていません"
            ));
        }
        let sqlite = open_search_index(path)?;
        let metadata = read_index_metadata(&sqlite)?;
        let version = metadata
            .get("index_version")
            .map(String::as_str)
            .unwrap_or_default();
        if version != SEARCH_INDEX_VERSION {
            return Err(anyhow!(
                "index_versionが不一致です (expected={SEARCH_INDEX_VERSION}, actual={version})"
            ));
        }
        if metadata.get("tokenizer").map(String::as_str) != Some("trigram")
            || metadata.get("detail").map(String::as_str) != Some("full")
        {
            return Err(anyhow!(
                "検索索引のFTS設定が不一致です (tokenizer=trigram, detail=fullが必要です)"
            ));
        }
        validate_index_schema(&sqlite)?;
        let row_count = metadata
            .get("row_count")
            .context("row_countメタデータがありません")?
            .parse::<u64>()
            .context("row_countメタデータが数値ではありません")?;

        let runtime_path = self
            .runtime_path
            .as_ref()
            .context("ランタイムDBがありません")?;
        let conn = runtime_conn.context("ランタイムDB接続がありません")?;
        let runtime_row_count: i64 = conn
            .query_row(
                "SELECT count(*) FROM queria_runtime.core.companies",
                [],
                |row| row.get(0),
            )
            .context("Runtime DBの法人件数を読めません")?;
        if runtime_row_count.max(0) as u64 != row_count {
            return Err(anyhow!(
                "Runtime DBと検索索引の法人件数が一致しません (runtime={}, index={row_count})",
                runtime_row_count.max(0)
            ));
        }
        if let Some(expected_generation) = metadata.get("runtime_generation_id") {
            let manifest_json: String = conn
                .query_row(
                    "SELECT manifest_json FROM queria_runtime.meta.runtime_manifest ORDER BY built_at DESC LIMIT 1",
                    [],
                    |row| row.get(0),
                )
                .context("Runtime DBのgeneration_idを読めません")?;
            let manifest: serde_json::Value =
                serde_json::from_str(&manifest_json).context("Runtime manifestが不正です")?;
            let actual_generation = manifest
                .get("generation_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default();
            if actual_generation.is_empty() || actual_generation != expected_generation {
                return Err(anyhow!("Runtime DBと検索索引のgeneration_idが一致しません"));
            }
            if let Some(expected_bytes) = metadata.get("source_database_bytes") {
                let expected_bytes = expected_bytes
                    .parse::<u64>()
                    .context("source_database_bytesが数値ではありません")?;
                if runtime_path.metadata()?.len() != expected_bytes {
                    return Err(anyhow!(
                        "Runtime DBと検索索引のファイルサイズが一致しません"
                    ));
                }
            }
        } else if let Some(expected_bytes) = metadata.get("source_database_bytes") {
            let expected_bytes = expected_bytes
                .parse::<u64>()
                .context("source_database_bytesが数値ではありません")?;
            if runtime_path.metadata()?.len() != expected_bytes {
                return Err(anyhow!(
                    "Runtime DBと検索索引のファイルサイズが一致しません"
                ));
            }
            if metadata
                .get("source_database")
                .and_then(|source| Path::new(source).canonicalize().ok())
                .as_deref()
                == runtime_path.canonicalize().ok().as_deref()
            {
                if let Some(expected_mtime) = metadata.get("source_database_mtime_ns") {
                    let expected_mtime = expected_mtime
                        .parse::<u128>()
                        .context("source_database_mtime_nsが数値ではありません")?;
                    let actual_mtime = runtime_path
                        .metadata()?
                        .modified()?
                        .duration_since(std::time::UNIX_EPOCH)
                        .context("Runtime DBの更新日時がUNIX epochより前です")?
                        .as_nanos();
                    if actual_mtime != expected_mtime {
                        return Err(anyhow!("Runtime DBと検索索引の更新日時が一致しません"));
                    }
                }
            }
        } else {
            return Err(anyhow!(
                "検索索引にgeneration_idまたは原本サイズがなく安全に照合できません"
            ));
        }
        Ok(row_count)
    }

    fn search_sqlite_index(
        &self,
        path: &Path,
        duckdb_conn: &Connection,
        plan: &SearchPlan,
        sort: SortSpec,
        page: u32,
        page_size: u32,
    ) -> Result<(Vec<Company>, u64)> {
        let sqlite = open_search_index(path)?;
        let query = build_sqlite_search(plan)?;
        let count_sql = format!(
            "SELECT count(*) FROM {} WHERE {}",
            query.from_sql, query.where_sql
        );
        let total: i64 =
            sqlite.query_row(&count_sql, params_from_iter(query.params.iter()), |row| {
                row.get(0)
            })?;
        let total = total.max(0) as u64;
        let offset = (page.saturating_sub(1) as u64 * page_size as u64).min(total);
        if offset >= total {
            return Ok((Vec::new(), total));
        }

        let order_by = sqlite_order_by(sort, query.has_fts);
        let page_sql = format!(
            "SELECT d.corporate_number FROM {} WHERE {} ORDER BY {} LIMIT ? OFFSET ?",
            query.from_sql, query.where_sql, order_by
        );
        let mut page_params = query.params.clone();
        page_params.push(SqliteValue::Integer(i64::from(page_size)));
        page_params.push(SqliteValue::Integer(offset as i64));
        let mut statement = sqlite.prepare(&page_sql)?;
        let numbers = statement
            .query_map(params_from_iter(page_params.iter()), |row| {
                row.get::<_, String>(0)
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let rows = hydrate_companies(duckdb_conn, &numbers)?;
        if rows.len() != numbers.len() {
            return Err(anyhow!(
                "検索索引とRuntime DBの法人集合が一致しません (index={}, runtime={})",
                numbers.len(),
                rows.len()
            ));
        }
        Ok((rows, total))
    }

    pub fn save_search(&self, name: &str, query: &str, plan: &SearchPlan) -> Result<()> {
        let conn = self.connect()?;
        let id = format!(
            "search-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
        );
        let normalized = plan.clone().normalize();
        validate_search_text(&normalized)?;
        validate_sort(&normalized)?;
        let json = serde_json::to_string(&normalized)?;
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
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    pub fn export_search_csv(&self, plan: SearchPlan, path: &Path) -> Result<u64> {
        let plan = plan.normalize();
        validate_search_text(&plan)?;
        let sort = validate_sort(&plan)?;
        let where_sql = build_where(&plan);
        let order_by = duckdb_order_by(sort, plan.text.as_deref());
        let safe_path = sql_quote(&path.to_string_lossy());
        let sql = format!(
            r#"COPY (
              SELECT {corporate_number},{entity_key},{fuma_id},{source_kind},{name},{prefecture},{city},{address},{kind},
                     {industry_code},{industry_name},{industry_source},{industry_middle_code},{industry_middle_name},
                     {industry_small_code},{industry_small_name},{industry_detail_code},{industry_detail_name},
                     {inferred_industry_code},{inferred_industry_name},inferred_industry_confidence,
                     employees,capital,established_year,{website},{phone},{representative},{business_summary},
                     {source_updated_at},{phone_type},{phone_source_url},phone_confidence,{phone_evidence_text},
                     {phone_observed_at},{phone_status}
              FROM companies c WHERE {where_sql}
              ORDER BY {order_by}
              LIMIT {limit}
            ) TO {safe_path} (HEADER, DELIMITER ',')"#,
            limit = plan.limit,
            corporate_number = csv_safe_text_sql("corporate_number"),
            entity_key = csv_safe_text_sql("entity_key"),
            fuma_id = csv_safe_text_sql("fuma_id"),
            source_kind = csv_safe_text_sql("source_kind"),
            name = csv_safe_text_sql("name"),
            prefecture = csv_safe_text_sql("prefecture"),
            city = csv_safe_text_sql("city"),
            address = csv_safe_text_sql("address"),
            kind = csv_safe_text_sql("kind"),
            industry_code = csv_safe_text_sql("industry_code"),
            industry_name = csv_safe_text_sql("industry_name"),
            industry_source = csv_safe_text_sql("industry_source"),
            industry_middle_code = csv_safe_text_sql("industry_middle_code"),
            industry_middle_name = csv_safe_text_sql("industry_middle_name"),
            industry_small_code = csv_safe_text_sql("industry_small_code"),
            industry_small_name = csv_safe_text_sql("industry_small_name"),
            industry_detail_code = csv_safe_text_sql("industry_detail_code"),
            industry_detail_name = csv_safe_text_sql("industry_detail_name"),
            inferred_industry_code = csv_safe_text_sql("inferred_industry_code"),
            inferred_industry_name = csv_safe_text_sql("inferred_industry_name"),
            website = csv_safe_text_sql("website"),
            phone = csv_safe_text_sql("phone"),
            representative = csv_safe_text_sql("representative"),
            business_summary = csv_safe_text_sql("business_summary"),
            source_updated_at = csv_safe_text_sql("source_updated_at"),
            phone_type = csv_safe_text_sql("phone_type"),
            phone_source_url = csv_safe_text_sql("phone_source_url"),
            phone_evidence_text = csv_safe_text_sql("phone_evidence_text"),
            phone_observed_at = csv_safe_text_sql("phone_observed_at"),
            phone_status = csv_safe_text_sql("phone_status"),
        );
        let conn = self.connect()?;
        conn.execute_batch(&sql)?;
        let count_sql = format!(
            "SELECT least(count(*), {}) FROM companies c WHERE {}",
            plan.limit, where_sql
        );
        let count: i64 = conn.query_row(&count_sql, [], |r| r.get(0))?;
        Ok(count.max(0) as u64)
    }

    pub fn export_search_xlsx(&self, plan: SearchPlan, path: &Path) -> Result<u64> {
        let plan = plan.normalize();
        validate_search_text(&plan)?;
        let sort = validate_sort(&plan)?;
        if plan.limit > 1_048_575 {
            return Err(anyhow!(
                "XLSXは1シートあたり最大1,048,575社です。全件出力はCSVを使用してください"
            ));
        }
        let where_sql = build_where(&plan);
        let order_by = duckdb_order_by(sort, plan.text.as_deref());
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
            ORDER BY {order_by}
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
            "法人番号",
            "Entity Key",
            "FUMA_ID",
            "データソース",
            "会社名",
            "都道府県",
            "市区町村",
            "住所",
            "法人種別",
            "業種コード",
            "業種名",
            "業種ソース",
            "中分類コード",
            "中分類名",
            "小分類コード",
            "小分類名",
            "細分類コード",
            "細分類名",
            "AI推定業種コード",
            "AI推定業種名",
            "AI推定信頼度",
            "従業員数",
            "資本金",
            "設立年",
            "Webサイト",
            "電話",
            "代表者",
            "事業概要",
            "更新日時",
            "電話用途",
            "電話根拠URL",
            "電話信頼度",
            "電話証拠",
            "電話取得日時",
            "電話状態",
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
                company
                    .inferred_industry_confidence
                    .map(|v| v.to_string())
                    .unwrap_or_default(),
                company.employees.map(|v| v.to_string()).unwrap_or_default(),
                company.capital.map(|v| v.to_string()).unwrap_or_default(),
                company
                    .established_year
                    .map(|v| v.to_string())
                    .unwrap_or_default(),
                company.website.unwrap_or_default(),
                company.phone.unwrap_or_default(),
                company.representative.unwrap_or_default(),
                company.business_summary.unwrap_or_default(),
                company.source_updated_at.unwrap_or_default(),
                company.phone_type.unwrap_or_default(),
                company.phone_source_url.unwrap_or_default(),
                company
                    .phone_confidence
                    .map(|v| v.to_string())
                    .unwrap_or_default(),
                company.phone_evidence_text.unwrap_or_default(),
                company.phone_observed_at.unwrap_or_default(),
                company.phone_status.unwrap_or_default(),
            ];
            for (column, value) in values.iter().enumerate() {
                worksheet.write_string(output_row, column as u16, value.as_str())?;
            }
            output_row = output_row.saturating_add(1);
        }
        workbook
            .save(path)
            .with_context(|| format!("XLSXを書き出せません: {}", path.display()))?;
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
        if list_name.is_empty() {
            return Err(anyhow!("リスト名が空です"));
        }
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
        validate_search_text(&plan)?;
        let sort = validate_sort(&plan)?;
        let where_sql = build_where(&plan);
        let order_by = duckdb_order_by(sort, plan.text.as_deref());
        let sql = format!(
            "INSERT OR IGNORE INTO company_list_items(list_id,corporate_number) SELECT {}, corporate_number FROM companies c WHERE {} ORDER BY {} LIMIT {}",
            sql_quote(&actual_id), where_sql, order_by, plan.limit
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
        while let Some(row) = rows.next()? {
            out.push(company_from_row(row)?);
        }
        Ok(out)
    }

    pub fn save_research(&self, report: &ResearchReport) -> Result<()> {
        let conn = self.connect()?;
        let id = format!(
            "research-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
        );
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
                    duckdb::params![
                        guess.code,
                        guess.name,
                        guess.confidence,
                        report.corporate_number
                    ],
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
        let ext = path
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let conn = self.connect()?;
        let mut attached_alias: Option<&str> = None;
        let reader = match ext.as_str() {
            "parquet" => format!("read_parquet({})", sql_quote(&path.to_string_lossy())),
            "csv" => format!(
                "read_csv_auto({}, header=true, all_varchar=false)",
                sql_quote(&path.to_string_lossy())
            ),
            "json" | "jsonl" | "ndjson" => {
                format!("read_json_auto({})", sql_quote(&path.to_string_lossy()))
            }
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
        let reader = match path
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase()
            .as_str()
        {
            "parquet" => format!("read_parquet({})", sql_quote(&path.to_string_lossy())),
            _ => format!(
                "read_csv_auto({}, header=true, all_varchar=true)",
                sql_quote(&path.to_string_lossy())
            ),
        };
        let sql = format!(
            r#"INSERT OR REPLACE INTO industry_taxonomy
               SELECT CAST(code AS VARCHAR),CAST(name AS VARCHAR),CAST(level AS INTEGER),
                      try_cast(parent_code AS VARCHAR),try_cast(revision AS VARCHAR),try_cast(source_url AS VARCHAR)
               FROM {reader}"#
        );
        conn.execute_batch(&sql)?;
        let count: i64 =
            conn.query_row("SELECT count(*) FROM industry_taxonomy", [], |r| r.get(0))?;
        Ok(count.max(0) as u64)
    }
}

#[derive(Debug)]
struct SqliteSearchQuery {
    from_sql: String,
    where_sql: String,
    params: Vec<SqliteValue>,
    has_fts: bool,
}

fn open_search_index(path: &Path) -> Result<SqliteConnection> {
    let connection = SqliteConnection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| {
        format!(
            "SQLite検索索引を読み取り専用で開けません: {}",
            path.display()
        )
    })?;
    connection.execute_batch(
        "PRAGMA query_only=ON; PRAGMA trusted_schema=OFF; PRAGMA temp_store=MEMORY;",
    )?;
    Ok(connection)
}

fn read_index_metadata(connection: &SqliteConnection) -> Result<HashMap<String, String>> {
    let table_exists: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_metadata')",
        [],
        |row| row.get(0),
    )?;
    if !table_exists {
        return Err(anyhow!("index_metadataテーブルがありません"));
    }
    let mut statement = connection.prepare("SELECT key, value FROM index_metadata")?;
    let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
    let mut metadata = HashMap::new();
    for row in rows {
        let (key, value): (String, String) = row?;
        metadata.insert(key, value);
    }
    Ok(metadata)
}

fn validate_index_schema(connection: &SqliteConnection) -> Result<()> {
    let required_columns = [
        "doc_id",
        "corporate_number",
        "company_name",
        "full_address",
        "prefecture_name",
        "city_name",
        "employee_number",
        "capital_stock",
        "company_url",
        "phone",
        "corporate_kind_code",
    ];
    let mut statement = connection.prepare("PRAGMA table_info(company_docs)")?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for required in required_columns {
        if !columns.iter().any(|column| column == required) {
            return Err(anyhow!("company_docs.{required}がありません"));
        }
    }

    for table in ["company_categories", "company_fts"] {
        let exists: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name=?)",
            [table],
            |row| row.get(0),
        )?;
        if !exists {
            return Err(anyhow!("{table}がありません"));
        }
    }
    let mut category_statement = connection.prepare("PRAGMA table_info(company_categories)")?;
    let category_columns = category_statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for required in ["doc_id", "major_code", "middle_code"] {
        if !category_columns.iter().any(|column| column == required) {
            return Err(anyhow!("company_categories.{required}がありません"));
        }
    }

    let mut fts_statement = connection.prepare("PRAGMA table_info(company_fts)")?;
    let fts_columns = fts_statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for required in [
        "company_name",
        "full_address",
        "business_summary",
        "business_items_raw",
        "company_url",
        "phone",
        "email",
        "inquiry_form_url",
    ] {
        if !fts_columns.iter().any(|column| column == required) {
            return Err(anyhow!("company_fts.{required}がありません"));
        }
    }
    // Preparing a MATCH query verifies that this SQLite build can load the
    // FTS5 virtual table without scanning the multi-gigabyte index.
    connection.prepare("SELECT rowid FROM company_fts WHERE company_fts MATCH ? LIMIT 0")?;
    Ok(())
}

fn sqlite_supported(plan: &SearchPlan) -> std::result::Result<(), &'static str> {
    if !plan.industry_terms.is_empty() {
        return Err("業種名キーワードは索引に専用列がありません");
    }
    if !plan.keyword_any.is_empty() || !plan.keyword_all.is_empty() {
        return Err("複合キーワード条件はDuckDBで厳密に評価します");
    }
    if plan.established_from.is_some() || plan.established_to.is_some() {
        return Err("設立年は検索索引に収録されていません");
    }
    if plan
        .industry_codes
        .iter()
        .any(|code| sqlite_industry_code(code).is_none())
    {
        return Err("小分類・細分類コードはDuckDBで境界を確認します");
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum SqliteIndustryCode {
    Major(String),
    Middle(String),
    QualifiedMiddle { major: String, middle: String },
}

fn sqlite_industry_code(value: &str) -> Option<SqliteIndustryCode> {
    let value = value.trim().to_ascii_uppercase();
    let mut chars = value.chars();
    if value.len() == 1 && chars.next()?.is_ascii_alphabetic() {
        return Some(SqliteIndustryCode::Major(value));
    }
    if value.len() == 2 && value.chars().all(|ch| ch.is_ascii_digit()) {
        return Some(SqliteIndustryCode::Middle(value));
    }
    if value.len() == 3
        && value.as_bytes()[0].is_ascii_alphabetic()
        && value.as_bytes()[1..].iter().all(u8::is_ascii_digit)
    {
        return Some(SqliteIndustryCode::QualifiedMiddle {
            major: value[..1].to_string(),
            middle: value[1..].to_string(),
        });
    }
    None
}

fn build_sqlite_search(plan: &SearchPlan) -> Result<SqliteSearchQuery> {
    validate_search_text(plan)?;
    sqlite_supported(plan).map_err(|reason| anyhow!("{reason}"))?;
    let mut from_sql = "company_docs AS d".to_string();
    let mut conditions = Vec::new();
    let mut params = Vec::new();
    let mut has_fts = false;

    if let Some(text) = plan.text.as_deref() {
        if let Some(corporate_number) = corporate_number_query(text) {
            conditions.push("d.corporate_number=?".to_string());
            params.push(SqliteValue::Text(corporate_number));
        } else if let Some(query) = fts_phrase(text) {
            from_sql.push_str(" JOIN company_fts ON company_fts.rowid=d.doc_id");
            conditions.push("company_fts MATCH ?".to_string());
            params.push(SqliteValue::Text(query));
            has_fts = true;
        } else {
            // The trigram tokenizer cannot answer one- or two-character
            // terms. Never turn those into an eight-column `%LIKE%` scan over
            // the 5.8M-row index; the indexed company-name prefix is the
            // predictable fast-path used by the CLI as well.
            let prefix = normalize_index_text(text);
            conditions.push("d.company_name LIKE ? ESCAPE '\\'".to_string());
            params.push(SqliteValue::Text(format!("{}%", escape_like(&prefix))));
        }
    }

    push_sqlite_in(
        &mut conditions,
        &mut params,
        "d.prefecture_name",
        &plan.prefectures,
    );
    if !plan.cities.is_empty() {
        let mut items = Vec::new();
        for city in &plan.cities {
            items.push("coalesce(d.city_name,'') LIKE ? ESCAPE '\\'".to_string());
            params.push(SqliteValue::Text(format!("%{}%", escape_like(city))));
        }
        conditions.push(format!("({})", items.join(" OR ")));
    }

    if !plan.industry_codes.is_empty() {
        let mut category_terms = Vec::new();
        for code in &plan.industry_codes {
            match sqlite_industry_code(code).expect("validated industry code") {
                SqliteIndustryCode::Major(code) => {
                    category_terms.push("cc.major_code=?".to_string());
                    params.push(SqliteValue::Text(code));
                }
                SqliteIndustryCode::Middle(code) => {
                    category_terms.push("cc.middle_code=?".to_string());
                    params.push(SqliteValue::Text(code));
                }
                SqliteIndustryCode::QualifiedMiddle { major, middle } => {
                    category_terms.push("(cc.major_code=? AND cc.middle_code=?)".to_string());
                    params.push(SqliteValue::Text(major));
                    params.push(SqliteValue::Text(middle));
                }
            }
        }
        conditions.push(format!(
            "EXISTS (SELECT 1 FROM company_categories AS cc WHERE cc.doc_id=d.doc_id AND ({}))",
            category_terms.join(" OR ")
        ));
    }

    push_sqlite_in(
        &mut conditions,
        &mut params,
        "d.corporate_kind_code",
        &plan.company_kinds,
    );
    push_sqlite_range(
        &mut conditions,
        &mut params,
        "d.employee_number",
        plan.min_employees,
        plan.max_employees,
    );
    push_sqlite_range(
        &mut conditions,
        &mut params,
        "d.capital_stock",
        plan.min_capital,
        plan.max_capital,
    );
    push_presence(&mut conditions, "d.company_url", plan.website_required);
    push_presence(&mut conditions, "d.phone", plan.phone_required);

    if conditions.is_empty() {
        conditions.push("1=1".to_string());
    }
    Ok(SqliteSearchQuery {
        from_sql,
        where_sql: conditions.join(" AND "),
        params,
        has_fts,
    })
}

fn push_sqlite_in(
    conditions: &mut Vec<String>,
    params: &mut Vec<SqliteValue>,
    column: &str,
    values: &[String],
) {
    if values.is_empty() {
        return;
    }
    conditions.push(format!(
        "{column} IN ({})",
        std::iter::repeat("?")
            .take(values.len())
            .collect::<Vec<_>>()
            .join(",")
    ));
    params.extend(values.iter().cloned().map(SqliteValue::Text));
}

fn push_sqlite_range(
    conditions: &mut Vec<String>,
    params: &mut Vec<SqliteValue>,
    column: &str,
    minimum: Option<i64>,
    maximum: Option<i64>,
) {
    if let Some(value) = minimum {
        conditions.push(format!("{column}>=?"));
        params.push(SqliteValue::Integer(value.max(0)));
    }
    if let Some(value) = maximum {
        conditions.push(format!("{column}<=?"));
        params.push(SqliteValue::Integer(value.max(0)));
    }
}

fn push_presence(conditions: &mut Vec<String>, column: &str, required: Option<bool>) {
    match required {
        Some(true) => conditions.push(format!("{column} IS NOT NULL AND trim({column})<>''")),
        Some(false) => conditions.push(format!("({column} IS NULL OR trim({column})='')")),
        None => {}
    }
}

fn normalize_index_text(value: &str) -> String {
    value
        .nfkc()
        .collect::<String>()
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn corporate_number_query(value: &str) -> Option<String> {
    let normalized = normalize_index_text(value);
    (normalized.len() == 13 && normalized.bytes().all(|byte| byte.is_ascii_digit()))
        .then_some(normalized)
}

fn phone_like_query(value: &str) -> bool {
    let normalized = value.nfkc().collect::<String>();
    let mut digits = 0usize;
    for character in normalized.chars() {
        if character.is_ascii_digit() {
            digits += 1;
        } else if !matches!(character, '+' | '-' | '(' | ')' | '.' | ' ' | '\t') {
            return false;
        }
    }
    digits >= 6
}

fn validate_search_text(plan: &SearchPlan) -> Result<()> {
    let Some(text) = plan.text.as_deref() else {
        return Ok(());
    };
    let normalized_length = text.nfkc().count();
    if text.chars().count().max(normalized_length) > MAX_SEARCH_TEXT_CHARS {
        return Err(anyhow!(
            "検索キーワードは{MAX_SEARCH_TEXT_CHARS}文字以内で指定してください"
        ));
    }
    if text.contains('\0') {
        return Err(anyhow!("検索キーワードにNUL文字は使用できません"));
    }
    Ok(())
}

fn fts_phrase(value: &str) -> Option<String> {
    let value = normalize_index_text(value);
    let length = value.chars().count();
    if !(3..=MAX_SEARCH_TEXT_CHARS).contains(&length) || value.contains('\0') {
        return None;
    }
    Some(format!("\"{}\"", value.replace('"', "\"\"")))
}

fn sqlite_order_by(sort: SortSpec, has_fts: bool) -> String {
    let direction = match sort.direction {
        SortDirection::Asc => "ASC",
        SortDirection::Desc => "DESC",
    };
    match sort.by {
        SortBy::Relevance if has_fts => format!(
            "bm25(company_fts) {direction}, d.company_name COLLATE NOCASE, d.corporate_number"
        ),
        SortBy::Employees => format!(
            "d.employee_number IS NULL, d.employee_number {direction}, d.company_name COLLATE NOCASE, d.corporate_number"
        ),
        SortBy::Capital => format!(
            "d.capital_stock IS NULL, d.capital_stock {direction}, d.company_name COLLATE NOCASE, d.corporate_number"
        ),
        SortBy::Name | SortBy::Relevance => format!(
            "d.company_name IS NULL, d.company_name COLLATE NOCASE {direction}, d.corporate_number"
        ),
    }
}

fn hydrate_companies(
    connection: &Connection,
    corporate_numbers: &[String],
) -> Result<Vec<Company>> {
    if corporate_numbers.is_empty() {
        return Ok(Vec::new());
    }
    let numbers = corporate_numbers
        .iter()
        .map(|number| sql_quote(number))
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!(
        r#"SELECT
          corporate_number,entity_key,fuma_id,source_kind,name,prefecture,city,address,kind,
          industry_code,industry_name,industry_source,
          industry_middle_code,industry_middle_name,industry_small_code,industry_small_name,
          industry_detail_code,industry_detail_name,
          inferred_industry_code,inferred_industry_name,inferred_industry_confidence,
          employees,capital,established_year,website,phone,representative,business_summary,source_updated_at,
          phone_type,phone_source_url,phone_confidence,phone_evidence_text,phone_observed_at,phone_status
        FROM companies c WHERE corporate_number IN ({numbers})"#
    );
    let mut statement = connection.prepare(&sql)?;
    let mut rows = statement.query([])?;
    let mut companies = HashMap::new();
    while let Some(row) = rows.next()? {
        let company = company_from_row(row)?;
        companies.insert(company.corporate_number.clone(), company);
    }
    Ok(corporate_numbers
        .iter()
        .filter_map(|number| companies.remove(number))
        .collect())
}

fn validate_sort(plan: &SearchPlan) -> Result<SortSpec> {
    let by = match plan.sort_by.as_deref() {
        None => {
            if plan.text.is_some() {
                SortBy::Relevance
            } else {
                SortBy::Name
            }
        }
        Some("relevance") => SortBy::Relevance,
        Some("name") => SortBy::Name,
        Some("employees") => SortBy::Employees,
        Some("capital") => SortBy::Capital,
        Some(value) => {
            return Err(anyhow!(
                "sort_byが不正です: {value} (relevance/name/employees/capitalを指定してください)"
            ))
        }
    };
    let direction = match plan.sort_direction.as_deref() {
        None => match by {
            SortBy::Employees | SortBy::Capital => SortDirection::Desc,
            SortBy::Relevance | SortBy::Name => SortDirection::Asc,
        },
        Some("asc") => SortDirection::Asc,
        Some("desc") => SortDirection::Desc,
        Some(value) => {
            return Err(anyhow!(
                "sort_directionが不正です: {value} (asc/descを指定してください)"
            ))
        }
    };
    Ok(SortSpec { by, direction })
}

fn duckdb_order_by(sort: SortSpec, text: Option<&str>) -> String {
    let direction = match sort.direction {
        SortDirection::Asc => "ASC",
        SortDirection::Desc => "DESC",
    };
    match sort.by {
        SortBy::Employees => format!(
            "c.employees IS NULL, c.employees {direction}, c.name {direction}, c.corporate_number"
        ),
        SortBy::Capital => format!(
            "c.capital IS NULL, c.capital {direction}, c.name {direction}, c.corporate_number"
        ),
        SortBy::Relevance if text.is_some() => {
            let text = text.expect("checked above");
            let exact = sql_quote(text);
            let prefix = sql_quote(&format!("{}%", escape_like(text)));
            let contains = sql_quote(&format!("%{}%", escape_like(text)));
            format!(
                "CASE WHEN c.corporate_number={exact} THEN 0 WHEN lower(c.name)=lower({exact}) THEN 1 WHEN c.name ILIKE {prefix} ESCAPE '\\' THEN 2 WHEN c.name ILIKE {contains} ESCAPE '\\' THEN 3 WHEN coalesce(c.industry_name,'') ILIKE {contains} ESCAPE '\\' THEN 4 ELSE 5 END {direction}, c.name, c.corporate_number"
            )
        }
        SortBy::Name | SortBy::Relevance => {
            format!("c.name {direction}, c.corporate_number")
        }
    }
}

/// `companies` is a writable table in standalone mode and a read-only view in
/// runtime mode. Preserve the standalone snapshot while switching modes so a
/// runtime install/uninstall never strands the app with the wrong relation
/// type or destroys locally synchronized data.
fn migrate_company_relation(connection: &Connection, runtime_mode: bool) -> Result<()> {
    let relation_type = local_relation_type(connection, "companies")?;
    let backup_type = local_relation_type(connection, "companies_local_snapshot")?;
    if runtime_mode {
        match relation_type.as_deref() {
            Some("table") if backup_type.is_none() => connection.execute_batch(
                "ALTER TABLE companies RENAME TO companies_local_snapshot;",
            )?,
            Some("table") => {
                return Err(anyhow!(
                    "companiesとcompanies_local_snapshotが同時に存在するためRuntime表示へ安全に移行できません"
                ))
            }
            Some("view") | None => {}
            Some(kind) => return Err(anyhow!("companiesのrelation種別が不明です: {kind}")),
        }
    } else {
        if relation_type.as_deref() == Some("view") {
            connection.execute_batch("DROP VIEW companies;")?;
        }
        if local_relation_type(connection, "companies")?.is_none()
            && backup_type.as_deref() == Some("table")
        {
            connection
                .execute_batch("ALTER TABLE companies_local_snapshot RENAME TO companies;")?;
        }
    }
    Ok(())
}

fn local_relation_type(connection: &Connection, relation: &str) -> Result<Option<String>> {
    let sql = format!(
        r#"SELECT max(relation_type) FROM (
             SELECT 'table' AS relation_type
             FROM duckdb_tables()
             WHERE database_name=current_database() AND schema_name='main' AND table_name={name}
             UNION ALL
             SELECT 'view' AS relation_type
             FROM duckdb_views()
             WHERE database_name=current_database() AND schema_name='main' AND view_name={name}
           ) relations"#,
        name = sql_quote(relation),
    );
    let value: Option<String> = connection.query_row(&sql, [], |row| row.get(0))?;
    Ok(value)
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
    .with_context(|| {
        format!(
            "QueriaランタイムDBをREAD_ONLY接続できません: {}",
            runtime_path.display()
        )
    })?;
    Ok(())
}

fn is_g_fuma_runtime(conn: &Connection) -> bool {
    conn.query_row(
        "SELECT count(*) FROM queria_runtime.core.g_companies",
        [],
        |r| r.get::<_, i64>(0),
    )
    .is_ok()
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
          coalesce(nullif(contacts.resolved_prefecture_name, ''), nullif(base.prefecture_name, '')) AS prefecture,
          coalesce(nullif(contacts.resolved_city_name, ''), nullif(base.city_name, '')) AS city,
          coalesce(nullif(contacts.resolved_address, ''), nullif(base.full_address, '')) AS address,
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
          coalesce(nullif(contacts.effective_company_url, ''), nullif(base.company_url, ''), nullif(contacts.company_url, '')) AS website,
          coalesce(nullif(local_contacts.phone, ''), nullif(contacts.phone, '')) AS phone,
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
    pub fn save_phone_override(
        &self,
        corporate_number: &str,
        phone: &str,
        source_url: &str,
        evidence: &str,
    ) -> Result<()> {
        if corporate_number_query(corporate_number).is_none() {
            return Err(anyhow!("電話番号の保存には13桁の法人番号が必要です"));
        }
        let conn = self.connect()?;
        conn.execute(
            "INSERT OR REPLACE INTO company_contact_overrides(corporate_number,phone,source_url,evidence_text,collected_at) VALUES(?,?,?,?,current_timestamp)",
            duckdb::params![corporate_number, phone, source_url, evidence],
        )?;
        Ok(())
    }

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
        corporate_number: row.get(0)?,
        entity_key: row.get(1)?,
        fuma_id: row.get(2)?,
        source_kind: row.get(3)?,
        name: row.get(4)?,
        prefecture: row.get(5)?,
        city: row.get(6)?,
        address: row.get(7)?,
        kind: row.get(8)?,
        industry_code: row.get(9)?,
        industry_name: row.get(10)?,
        industry_source: row.get(11)?,
        industry_middle_code: row.get(12)?,
        industry_middle_name: row.get(13)?,
        industry_small_code: row.get(14)?,
        industry_small_name: row.get(15)?,
        industry_detail_code: row.get(16)?,
        industry_detail_name: row.get(17)?,
        inferred_industry_code: row.get(18)?,
        inferred_industry_name: row.get(19)?,
        inferred_industry_confidence: row.get(20)?,
        employees: row.get(21)?,
        capital: row.get(22)?,
        established_year: row.get(23)?,
        website: row.get(24)?,
        phone: row.get(25)?,
        representative: row.get(26)?,
        business_summary: row.get(27)?,
        source_updated_at: row.get(28)?,
        phone_type: row.get(29)?,
        phone_source_url: row.get(30)?,
        phone_confidence: row.get(31)?,
        phone_evidence_text: row.get(32)?,
        phone_observed_at: row.get(33)?,
        phone_status: row.get(34)?,
    })
}

fn build_where(plan: &SearchPlan) -> String {
    let mut c: Vec<String> = vec!["1=1".into()];
    if let Some(text) = plan.text.as_deref() {
        if let Some(corporate_number) = corporate_number_query(text) {
            c.push(format!(
                "c.corporate_number = {}",
                sql_quote(&corporate_number)
            ));
        } else {
            c.push(contains_any_columns(
                text,
                &[
                    "c.corporate_number",
                    "c.name",
                    "c.address",
                    "c.business_summary",
                    "c.business_items",
                    "c.industry_name",
                    "c.inferred_industry_name",
                    "c.website",
                    "c.phone",
                ],
            ));
        }
    }
    if !plan.prefectures.is_empty() {
        c.push(in_list("c.prefecture", &plan.prefectures));
    }
    if !plan.cities.is_empty() {
        c.push(format!(
            "({})",
            plan.cities
                .iter()
                .map(|city| contains_any_columns(city, &["c.city"]))
                .collect::<Vec<_>>()
                .join(" OR ")
        ));
    }
    if !plan.company_kinds.is_empty() {
        c.push(in_list("c.kind", &plan.company_kinds));
    }
    if !plan.industry_codes.is_empty() {
        let terms: Vec<String> = plan
            .industry_codes
            .iter()
            .map(|code| industry_code_condition(code))
            .collect();
        c.push(format!("({})", terms.join(" OR ")));
    }
    for term in &plan.industry_terms {
        c.push(contains_any_columns(
            term,
            &[
                "c.industry_name",
                "c.inferred_industry_name",
                "c.business_summary",
                "c.business_items",
            ],
        ));
    }
    if let Some(v) = plan.min_employees {
        c.push(format!("c.employees >= {}", v.max(0)));
    }
    if let Some(v) = plan.max_employees {
        c.push(format!("c.employees <= {}", v.max(0)));
    }
    if let Some(v) = plan.min_capital {
        c.push(format!("c.capital >= {}", v.max(0)));
    }
    if let Some(v) = plan.max_capital {
        c.push(format!("c.capital <= {}", v.max(0)));
    }
    if let Some(v) = plan.established_from {
        c.push(format!("c.established_year >= {}", v.clamp(1800, 2200)));
    }
    if let Some(v) = plan.established_to {
        c.push(format!("c.established_year <= {}", v.clamp(1800, 2200)));
    }
    if plan.website_required == Some(true) {
        c.push("c.website IS NOT NULL AND trim(c.website) <> ''".into());
    }
    if plan.website_required == Some(false) {
        c.push("(c.website IS NULL OR trim(c.website) = '')".into());
    }
    if plan.phone_required == Some(true) {
        c.push("c.phone IS NOT NULL AND trim(c.phone) <> ''".into());
    }
    if plan.phone_required == Some(false) {
        c.push("(c.phone IS NULL OR trim(c.phone) = '')".into());
    }
    for term in &plan.keyword_all {
        c.push(contains_any_columns(
            term,
            &[
                "c.corporate_number",
                "c.name",
                "c.address",
                "c.business_summary",
                "c.industry_name",
                "c.inferred_industry_name",
                "c.website",
                "c.phone",
            ],
        ));
    }
    if !plan.keyword_any.is_empty() {
        let items: Vec<String> = plan
            .keyword_any
            .iter()
            .map(|term| {
                contains_any_columns(
                    term,
                    &[
                        "c.corporate_number",
                        "c.name",
                        "c.address",
                        "c.business_summary",
                        "c.industry_name",
                        "c.inferred_industry_name",
                        "c.website",
                        "c.phone",
                    ],
                )
            })
            .collect();
        c.push(format!("({})", items.join(" OR ")));
    }
    c.join(" AND ")
}

fn industry_code_condition(value: &str) -> String {
    let normalized = value.trim().to_ascii_uppercase();
    let (pattern, business_pattern) = if normalized.len() == 1
        && normalized
            .chars()
            .all(|character| character.is_ascii_alphabetic())
    {
        (
            format!("^{}[0-9]*$", normalized),
            Some(format!("(^|[|]){}:", normalized)),
        )
    } else {
        let (major_hint, digits) = if normalized.len() >= 3
            && normalized.as_bytes()[0].is_ascii_alphabetic()
            && normalized.as_bytes()[1..].iter().all(u8::is_ascii_digit)
        {
            (Some(&normalized[..1]), &normalized[1..])
        } else if normalized
            .chars()
            .all(|character| character.is_ascii_digit())
        {
            (None, normalized.as_str())
        } else {
            let exact = sql_quote(&normalized);
            return format!(
                "EXISTS (SELECT 1 FROM unnest(string_split(concat_ws('|', coalesce(c.industry_code,''), coalesce(c.inferred_industry_code,'')), '|')) AS industry_token(value) WHERE upper(trim(industry_token.value)) = {exact})"
            );
        };
        // Queria's qualified hierarchy tokens can contain a major letter,
        // two middle digits, and up to three descendant digits (for example
        // H42421). A middle-code query such as H42 or 42 must therefore allow
        // all three trailing digits without matching unrelated code 142.
        let remaining = 5usize.saturating_sub(digits.len());
        let major = major_hint.unwrap_or("[A-T]");
        (
            format!("^({major})?{}[0-9]{{0,{remaining}}}$", digits),
            Some(format!("(^|[|-]){}:", digits)),
        )
    };
    let token_condition = format!(
        "EXISTS (SELECT 1 FROM unnest(string_split(concat_ws('|', coalesce(c.industry_code,''), coalesce(c.inferred_industry_code,'')), '|')) AS industry_token(value) WHERE regexp_matches(upper(trim(industry_token.value)), {}))",
        sql_quote(&pattern),
    );
    match business_pattern {
        Some(pattern) => format!(
            "({token_condition} OR regexp_matches(coalesce(c.business_items,''), {}))",
            sql_quote(&pattern)
        ),
        None => token_condition,
    }
}

fn contains_any_columns(term: &str, columns: &[&str]) -> String {
    let pattern = sql_quote(&format!("%{}%", escape_like(term)));
    let checks: Vec<String> = columns
        .iter()
        .map(|col| format!("coalesce({col},'') ILIKE {pattern} ESCAPE '\\'"))
        .collect();
    format!("({})", checks.join(" OR "))
}

fn in_list(column: &str, values: &[String]) -> String {
    let values = values
        .iter()
        .map(|v| sql_quote(v))
        .collect::<Vec<_>>()
        .join(",");
    format!("{column} IN ({values})")
}

fn sql_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

/// Spreadsheet applications treat several leading characters as formulas.
/// Prefix only textual CSV cells that begin with one of those characters;
/// SQL NULL must remain NULL and typed numeric columns bypass this helper.
fn csv_safe_text_sql(column: &str) -> String {
    format!(
        "CASE WHEN {column} IS NULL THEN NULL WHEN substr({column}, 1, 1) IN ('=', '+', '-', '@', chr(9), chr(13), chr(10)) THEN '''' || {column} ELSE {column} END AS {column}"
    )
}

fn stable_hash(value: &str) -> u64 {
    let mut hash = 1469598103934665603u64;
    for b in value.as_bytes() {
        hash ^= *b as u64;
        hash = hash.wrapping_mul(1099511628211);
    }
    hash
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn empty_database() -> (TempDir, Db) {
        let directory = tempfile::tempdir().expect("temp dir");
        let db = Db::new(directory.path().join("company-master.duckdb"));
        db.init().expect("initialize database");
        let connection = db.connect().expect("connect database");
        connection
            .execute_batch("DELETE FROM companies")
            .expect("clear samples");
        drop(connection);
        (directory, db)
    }

    #[test]
    fn duckdb_search_has_exact_pagination_independent_of_export_limit() {
        let (_directory, db) = empty_database();
        let connection = db.connect().expect("connect database");
        for index in 0..130 {
            let number = format!("1{index:012}");
            let name = format!("テスト会社{index:03}");
            let employees = i64::from(index);
            connection
                .execute(
                    "INSERT INTO companies(corporate_number,name,prefecture,city,kind,industry_code,employees,capital,business_summary) VALUES(?,?,?,?,?,?,?,?,?)",
                    duckdb::params![number, name, "東京都", "千代田区", "301", "3911", employees, employees * 1000, "クラウド"],
                )
                .expect("insert company");
        }
        drop(connection);

        let plan = SearchPlan {
            limit: 1,
            ..SearchPlan::default()
        };
        let first = db.search(plan.clone(), 1, 500).expect("first page");
        assert_eq!(first.engine, "duckdb");
        assert_eq!(first.total, 130);
        assert_eq!(first.page_size, 100);
        assert_eq!(first.rows.len(), 100);

        let second = db.search(plan, 2, 100).expect("second page");
        assert_eq!(second.total, 130);
        assert_eq!(second.rows.len(), 30);
    }

    #[test]
    fn csv_text_sanitizer_preserves_null_and_escapes_only_dangerous_prefixes() {
        let connection = Connection::open_in_memory().expect("in-memory DuckDB");
        let expression = csv_safe_text_sql("value");
        let sql = format!(
            r#"SELECT {expression} FROM (VALUES
                 (NULL), ('=corp'), ('+81'), ('-1'), ('@command'),
                 (chr(9) || 'tab'), (chr(13) || 'cr'), (chr(10) || 'lf'),
                 ('安全な文字列')
               ) AS source(value)"#
        );
        let mut statement = connection.prepare(&sql).expect("prepare sanitizer query");
        let rows = statement
            .query_map([], |row| row.get::<_, Option<String>>(0))
            .expect("run sanitizer query")
            .collect::<duckdb::Result<Vec<_>>>()
            .expect("collect sanitizer rows");
        assert_eq!(
            rows,
            vec![
                None,
                Some("'=corp".to_string()),
                Some("'+81".to_string()),
                Some("'-1".to_string()),
                Some("'@command".to_string()),
                Some("'\ttab".to_string()),
                Some("'\rcr".to_string()),
                Some("'\nlf".to_string()),
                Some("安全な文字列".to_string()),
            ]
        );
    }

    #[test]
    fn duckdb_search_applies_text_partial_city_kind_and_industry_boundaries() {
        let (_directory, db) = empty_database();
        let connection = db.connect().expect("connect database");
        connection
            .execute_batch(
                r#"
                INSERT INTO companies(corporate_number,name,prefecture,city,address,kind,industry_code,employees,business_summary,phone)
                VALUES
                  ('1000000000001','渋谷クラウド株式会社','東京都','渋谷区','東京都渋谷区','301','3911',20,'クラウド基盤','03-1111-2222'),
                  ('1000000000002','別業種株式会社','東京都','渋谷区','東京都渋谷区','301','1391',20,'クラウド基盤','03-1111-3333'),
                  ('1000000000003','港食品合同会社','東京都','港区','東京都港区','305','0972',30,'食品製造',NULL);
                "#,
            )
            .expect("insert fixtures");
        drop(connection);

        let plan = SearchPlan {
            text: Some("クラウド".to_string()),
            cities: vec!["渋谷".to_string()],
            industry_codes: vec!["39".to_string()],
            company_kinds: vec!["株式会社".to_string()],
            phone_required: Some(true),
            ..SearchPlan::default()
        };
        let result = db.search(plan, 1, 100).expect("filtered search");
        assert_eq!(result.total, 1);
        assert_eq!(result.rows[0].corporate_number, "1000000000001");
    }

    #[test]
    fn duckdb_search_finds_exact_corporate_number_and_qualified_middle_codes() {
        let (_directory, db) = empty_database();
        let connection = db.connect().expect("connect database");
        connection
            .execute_batch(
                r#"
                INSERT INTO companies(corporate_number,name,kind,industry_code,business_summary)
                VALUES
                  ('1000000000001','対象株式会社','301','H42421','対象'),
                  ('1000000000002','別部門株式会社','301','H1421','対象'),
                  ('1000000000003','別大分類株式会社','301','G42421','対象');
                "#,
            )
            .expect("insert fixtures");
        drop(connection);

        let exact = db
            .search(
                SearchPlan {
                    text: Some("1000000000001".to_string()),
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("corporate-number search");
        assert_eq!(exact.total, 1);
        assert_eq!(exact.rows[0].corporate_number, "1000000000001");

        let qualified = db
            .search(
                SearchPlan {
                    industry_codes: vec!["H42".to_string()],
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("qualified middle-code search");
        assert_eq!(qualified.total, 1);
        assert_eq!(qualified.rows[0].corporate_number, "1000000000001");

        let numeric = db
            .search(
                SearchPlan {
                    industry_codes: vec!["42".to_string()],
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("numeric middle-code search");
        assert_eq!(numeric.total, 2);
        assert!(numeric
            .rows
            .iter()
            .any(|company| company.corporate_number == "1000000000001"));

        let nonstandard = db
            .search(
                SearchPlan {
                    industry_codes: vec!["G-39".to_string()],
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("nonstandard code must not produce an unbound SQL alias");
        assert_eq!(nonstandard.total, 0);
    }

    #[test]
    fn rejects_search_text_over_256_characters_before_engine_selection() {
        let (_directory, db) = empty_database();
        let plan = SearchPlan {
            text: Some("あ".repeat(257)),
            ..SearchPlan::default()
        };
        let error = db
            .search(plan, 1, 100)
            .expect_err("oversized text must fail");
        assert!(error.to_string().contains("256文字以内"));

        let nul_error = db
            .search(
                SearchPlan {
                    text: Some("会社\0検索".to_string()),
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect_err("NUL text must fail");
        assert!(nul_error.to_string().contains("NUL"));
    }

    #[test]
    fn rejects_unknown_sort_values() {
        let (_directory, db) = empty_database();
        let plan = SearchPlan {
            sort_by: Some("drop table companies".to_string()),
            ..SearchPlan::default()
        };
        let error = db.search(plan, 1, 100).expect_err("invalid sort must fail");
        assert!(error.to_string().contains("sort_by"));
    }

    #[test]
    fn unsupported_index_filters_report_duckdb_fallback() {
        let (directory, db) = empty_database();
        let fake_index = directory.path().join("search.sqlite");
        std::fs::write(&fake_index, b"not a sqlite database").expect("write fake index");
        let db = db.with_search_index(Some(fake_index));
        let plan = SearchPlan {
            established_from: Some(2020),
            ..SearchPlan::default()
        };
        let result = db.search(plan, 1, 100).expect("DuckDB fallback");
        assert_eq!(result.engine, "duckdb");
        assert_eq!(result.total, 0);
        assert!(result
            .warnings
            .iter()
            .any(|warning| warning.contains("設立年")));
    }

    #[test]
    fn data_status_exposes_search_index_state() {
        let (_directory, db) = empty_database();
        let status = db.status(None).expect("data status");
        assert!(!status.search_index_available);
        assert_eq!(status.search_index_status.as_deref(), Some("not_found"));
        assert_eq!(status.search_index_row_count, None);
    }

    #[test]
    fn standalone_table_survives_runtime_view_mode_round_trip() {
        let directory = tempfile::tempdir().expect("temp dir");
        let connection = Connection::open(directory.path().join("migration.duckdb"))
            .expect("open migration database");
        connection
            .execute_batch("CREATE TABLE companies(id INTEGER); INSERT INTO companies VALUES (7);")
            .expect("create local table");

        migrate_company_relation(&connection, true).expect("enter runtime mode");
        assert_eq!(
            local_relation_type(&connection, "companies_local_snapshot").unwrap(),
            Some("table".to_string())
        );
        connection
            .execute_batch("CREATE VIEW companies AS SELECT * FROM companies_local_snapshot;")
            .expect("create runtime stand-in view");

        migrate_company_relation(&connection, false).expect("return to standalone mode");
        assert_eq!(
            local_relation_type(&connection, "companies").unwrap(),
            Some("table".to_string())
        );
        let value: i64 = connection
            .query_row("SELECT id FROM companies", [], |row| row.get(0))
            .expect("read restored row");
        assert_eq!(value, 7);
    }

    #[test]
    fn v8_sqlite_index_search_hydrates_runtime_rows_in_index_order() {
        let directory = tempfile::tempdir().expect("temp dir");
        let runtime_path = directory.path().join("queria_runtime.duckdb");
        let runtime = Connection::open(&runtime_path).expect("open runtime fixture");
        runtime
            .execute_batch(
                r#"
                CREATE SCHEMA core;
                CREATE SCHEMA search;
                CREATE SCHEMA meta;
                CREATE TABLE core.companies (
                  corporate_number VARCHAR, company_name VARCHAR,
                  prefecture_name VARCHAR, city_name VARCHAR, full_address VARCHAR,
                  corporate_kind_code VARCHAR, jsic_major_code VARCHAR,
                  jsic_middle_codes VARCHAR, jsic_codes_all_raw VARCHAR,
                  jsic_major_name VARCHAR, employee_number BIGINT, capital_stock BIGINT,
                  founding_year INTEGER, company_url VARCHAR, representative_name VARCHAR,
                  business_summary VARCHAR, business_items_raw VARCHAR,
                  subsidy_count BIGINT, subsidy_total_amount DOUBLE,
                  procurement_count BIGINT, procurement_total_award DOUBLE,
                  latest_fiscal_year INTEGER, latest_net_sales DOUBLE,
                  latest_ordinary_income DOUBLE, latest_net_income DOUBLE,
                  latest_total_assets DOUBLE, latest_net_assets DOUBLE, extracted_at VARCHAR
                );
                CREATE TABLE core.company_industries (
                  corporate_number VARCHAR, jsic_code VARCHAR, jsic_major_name VARCHAR,
                  jsic_middle_name VARCHAR, jsic_small_name VARCHAR
                );
                CREATE TABLE search.company_documents (
                  corporate_number VARCHAR, resolved_prefecture_name VARCHAR,
                  resolved_city_name VARCHAR, resolved_address VARCHAR,
                  employee_number BIGINT, capital_stock BIGINT, founding_year INTEGER,
                  effective_company_url VARCHAR, company_url VARCHAR, phone VARCHAR,
                  representative_name VARCHAR, business_summary VARCHAR,
                  business_items_raw VARCHAR
                );
                CREATE TABLE meta.runtime_manifest (manifest_json VARCHAR, built_at TIMESTAMP);
                INSERT INTO meta.runtime_manifest VALUES
                  ('{"generation_id":"fixture-v8"}', current_timestamp);
                INSERT INTO core.companies (
                  corporate_number, company_name, prefecture_name, city_name,
                  full_address, corporate_kind_code, jsic_major_code,
                  jsic_middle_codes, employee_number, capital_stock, extracted_at
                ) VALUES
                  ('1000000000001','Runtime 一社','東京都','渋谷区','東京都渋谷区','301','G','39',10,1000,'fixture'),
                  ('1000000000002','Runtime 二社','東京都','港区','東京都港区','301','G','39',50,2000,'fixture');
                INSERT INTO search.company_documents (
                  corporate_number, resolved_prefecture_name, resolved_city_name,
                  resolved_address, employee_number, capital_stock, business_summary
                ) VALUES
                  ('1000000000001','東京都','渋谷区','東京都渋谷区',10,1000,'クラウド'),
                  ('1000000000002','東京都','港区','東京都港区',50,2000,'クラウド');
                "#,
            )
            .expect("create runtime fixture");
        drop(runtime);

        let runtime_bytes = std::fs::metadata(&runtime_path)
            .expect("runtime metadata")
            .len()
            .to_string();
        let index_path = directory.path().join("search.sqlite");
        let index = SqliteConnection::open(&index_path).expect("open index fixture");
        index
            .execute_batch(
                r#"
                CREATE TABLE index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE company_docs (
                  doc_id INTEGER PRIMARY KEY, corporate_number TEXT NOT NULL UNIQUE,
                  company_name TEXT, full_address TEXT, prefecture_name TEXT, city_name TEXT,
                  jsic_major_codes TEXT, jsic_middle_codes TEXT, employee_number INTEGER,
                  capital_stock REAL, representative_name TEXT, company_url TEXT,
                  business_summary TEXT, business_items_raw TEXT, phone TEXT, email TEXT,
                  inquiry_form_url TEXT, corporate_kind_code TEXT
                );
                CREATE TABLE company_categories(
                  doc_id INTEGER, major_code TEXT, middle_code TEXT, prefecture_name TEXT
                );
                CREATE VIRTUAL TABLE company_fts USING fts5(
                  company_name, full_address, business_summary, business_items_raw,
                  company_url, phone, email, inquiry_form_url,
                  content='', tokenize='trigram', detail='full'
                );
                INSERT INTO company_docs VALUES
                  (1,'1000000000001','索引 一社','東京都渋谷区','東京都','渋谷区','G','39',10,1000,NULL,NULL,'クラウド',NULL,NULL,NULL,NULL,'301'),
                  (2,'1000000000002','索引 二社','東京都港区','東京都','港区','G','39',50,2000,NULL,NULL,'クラウド',NULL,NULL,NULL,NULL,'301');
                INSERT INTO company_fts(
                  rowid,company_name,full_address,business_summary,business_items_raw,
                  company_url,phone,email,inquiry_form_url
                ) VALUES
                  (1,'索引 一社','東京都渋谷区','クラウド','','','','',''),
                  (2,'索引 二社','東京都港区','クラウド','','','','','');
                "#,
            )
            .expect("create index fixture");
        for (key, value) in [
            ("index_version", "8".to_string()),
            ("row_count", "2".to_string()),
            ("tokenizer", "trigram".to_string()),
            ("detail", "full".to_string()),
            ("runtime_generation_id", "fixture-v8".to_string()),
            ("source_database_bytes", runtime_bytes),
        ] {
            index
                .execute(
                    "INSERT INTO index_metadata(key,value) VALUES(?,?)",
                    rusqlite::params![key, value],
                )
                .expect("insert index metadata");
        }
        drop(index);

        let db = Db::with_runtime(directory.path().join("company-master.duckdb"), runtime_path)
            .with_search_index(Some(index_path));
        let result = db
            .search(
                SearchPlan {
                    text: Some("クラウド".to_string()),
                    sort_by: Some("employees".to_string()),
                    sort_direction: Some("desc".to_string()),
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("search through v8 index");
        assert_eq!(result.engine, "sqlite_fts5", "{:?}", result.warnings);
        assert_eq!(result.total, 2);
        assert_eq!(result.rows.len(), 2);
        assert_eq!(result.rows[0].corporate_number, "1000000000002");
        assert_eq!(result.rows[0].name, "Runtime 二社");
        assert_eq!(result.rows[1].corporate_number, "1000000000001");

        db.save_phone_override(
            "1000000000001",
            "0312345678",
            "https://example.com/contact",
            "fixture",
        )
        .expect("save local phone override");
        let ordinary_text = db
            .search(
                SearchPlan {
                    text: Some("クラウド".to_string()),
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("ordinary text still uses index");
        assert_eq!(ordinary_text.engine, "sqlite_fts5");

        let overridden_phone = db
            .search(
                SearchPlan {
                    text: Some("0312345678".to_string()),
                    ..SearchPlan::default()
                },
                1,
                100,
            )
            .expect("phone text uses runtime override");
        assert_eq!(overridden_phone.engine, "duckdb");
        assert_eq!(overridden_phone.total, 1);
        assert_eq!(overridden_phone.rows[0].corporate_number, "1000000000001");
    }

    #[test]
    fn sqlite_fts_query_combines_supported_filters_without_duplicate_companies() {
        let connection = SqliteConnection::open_in_memory().expect("sqlite memory database");
        connection
            .execute_batch(
                r#"
                CREATE TABLE company_docs (
                  doc_id INTEGER PRIMARY KEY, corporate_number TEXT UNIQUE,
                  company_name TEXT, full_address TEXT, prefecture_name TEXT, city_name TEXT,
                  jsic_major_codes TEXT, jsic_middle_codes TEXT, employee_number INTEGER,
                  capital_stock REAL, representative_name TEXT, company_url TEXT,
                  business_summary TEXT, business_items_raw TEXT, phone TEXT, email TEXT,
                  inquiry_form_url TEXT, corporate_kind_code TEXT
                );
                CREATE TABLE company_categories(doc_id INTEGER, major_code TEXT, middle_code TEXT, prefecture_name TEXT);
                CREATE VIRTUAL TABLE company_fts USING fts5(
                  company_name, full_address, business_summary, business_items_raw,
                  company_url, phone, email, inquiry_form_url,
                  content='', tokenize='trigram', detail='full'
                );
                INSERT INTO company_docs VALUES
                  (1,'1000000000001','東京クラウド株式会社','東京都渋谷区','東京都','渋谷区','G','39',20,1000000,NULL,'https://cloud.example','クラウド基盤',NULL,'03-1111-2222',NULL,NULL,'301'),
                  (2,'1000000000002','東京食品株式会社','東京都渋谷区','東京都','渋谷区','E','09',10,500000,NULL,NULL,'食品製造',NULL,NULL,NULL,NULL,'301');
                INSERT INTO company_categories VALUES
                  (1,'G','39','東京都'),(1,'G',NULL,'東京都'),(2,'E','09','東京都'),
                  (1,'H','42','東京都'),(2,'G','42','東京都');
                INSERT INTO company_fts(rowid,company_name,full_address,business_summary,business_items_raw,company_url,phone,email,inquiry_form_url)
                VALUES
                  (1,'東京クラウド株式会社','東京都渋谷区','クラウド基盤','','https://cloud.example','03-1111-2222','',''),
                  (2,'東京食品株式会社','東京都渋谷区','食品製造','','','','','');
                "#,
            )
            .expect("create FTS fixture");

        let plan = SearchPlan {
            text: Some("クラウド".to_string()),
            prefectures: vec!["東京都".to_string()],
            cities: vec!["渋谷".to_string()],
            industry_codes: vec!["G39".to_string()],
            company_kinds: vec!["株式会社".to_string()],
            min_employees: Some(10),
            website_required: Some(true),
            phone_required: Some(true),
            ..SearchPlan::default()
        }
        .normalize();
        let query = build_sqlite_search(&plan).expect("build indexed query");
        let sql = format!(
            "SELECT count(*) FROM {} WHERE {}",
            query.from_sql, query.where_sql
        );
        let count: i64 = connection
            .query_row(&sql, params_from_iter(query.params.iter()), |row| {
                row.get(0)
            })
            .expect("execute indexed query");
        assert_eq!(count, 1);

        let corporate_number_plan = SearchPlan {
            text: Some("1000000000002".to_string()),
            ..SearchPlan::default()
        }
        .normalize();
        let exact_query =
            build_sqlite_search(&corporate_number_plan).expect("build corporate-number query");
        assert!(!exact_query.has_fts);
        let exact_sql = format!(
            "SELECT count(*) FROM {} WHERE {}",
            exact_query.from_sql, exact_query.where_sql
        );
        let exact_count: i64 = connection
            .query_row(
                &exact_sql,
                params_from_iter(exact_query.params.iter()),
                |row| row.get(0),
            )
            .expect("execute corporate-number query");
        assert_eq!(exact_count, 1);

        let short_plan = SearchPlan {
            text: Some("AI".to_string()),
            ..SearchPlan::default()
        }
        .normalize();
        let short_query = build_sqlite_search(&short_plan).expect("build short prefix query");
        assert!(!short_query.has_fts);
        assert_eq!(short_query.where_sql, "d.company_name LIKE ? ESCAPE '\\'");
        assert_eq!(
            short_query.params,
            vec![SqliteValue::Text("ai%".to_string())]
        );

        let quoted_plan = SearchPlan {
            text: Some("製造\"業".to_string()),
            ..SearchPlan::default()
        }
        .normalize();
        let quoted_query = build_sqlite_search(&quoted_plan).expect("build quoted FTS query");
        assert!(quoted_query.has_fts);
        assert_eq!(
            quoted_query.params,
            vec![SqliteValue::Text("\"製造\"\"業\"".to_string())]
        );

        let qualified_middle_plan = SearchPlan {
            industry_codes: vec!["H42".to_string()],
            ..SearchPlan::default()
        }
        .normalize();
        let qualified_middle_query =
            build_sqlite_search(&qualified_middle_plan).expect("build qualified middle-code query");
        assert!(qualified_middle_query
            .where_sql
            .contains("cc.major_code=? AND cc.middle_code=?"));
        let qualified_middle_sql = format!(
            "SELECT d.corporate_number FROM {} WHERE {}",
            qualified_middle_query.from_sql, qualified_middle_query.where_sql
        );
        let mut qualified_statement = connection
            .prepare(&qualified_middle_sql)
            .expect("prepare qualified middle-code query");
        let qualified_numbers = qualified_statement
            .query_map(
                params_from_iter(qualified_middle_query.params.iter()),
                |row| row.get::<_, String>(0),
            )
            .expect("execute qualified middle-code query")
            .collect::<rusqlite::Result<Vec<_>>>()
            .expect("collect qualified middle-code rows");
        assert_eq!(qualified_numbers, vec!["1000000000001".to_string()]);
    }
}
