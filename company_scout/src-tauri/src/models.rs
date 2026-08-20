use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SearchPlan {
    pub text: Option<String>,
    #[serde(default)]
    pub prefectures: Vec<String>,
    #[serde(default)]
    pub cities: Vec<String>,
    #[serde(default)]
    pub industry_codes: Vec<String>,
    #[serde(default)]
    pub industry_terms: Vec<String>,
    #[serde(default)]
    pub company_kinds: Vec<String>,
    pub min_employees: Option<i64>,
    pub max_employees: Option<i64>,
    pub min_capital: Option<i64>,
    pub max_capital: Option<i64>,
    pub established_from: Option<i32>,
    pub established_to: Option<i32>,
    pub website_required: Option<bool>,
    #[serde(default)]
    pub keyword_any: Vec<String>,
    #[serde(default)]
    pub keyword_all: Vec<String>,
    #[serde(default = "default_limit")]
    pub limit: u32,
}

fn default_limit() -> u32 { 30_000 }

/// The bundled public snapshot currently contains 5.8M corporations. Keep
/// the safety ceiling above that size so "全件抽出" does not silently stop at
/// two million rows while still preventing an accidental unbounded request.
pub const MAX_SEARCH_LIMIT: u32 = 10_000_000;

impl SearchPlan {
    pub fn normalize(mut self) -> Self {
        self.limit = self.limit.clamp(1, MAX_SEARCH_LIMIT);
        self.prefectures = clean(self.prefectures);
        self.cities = clean(self.cities);
        self.industry_codes = clean(self.industry_codes);
        self.industry_terms = clean(self.industry_terms);
        self.company_kinds = clean(self.company_kinds);
        self.keyword_any = clean(self.keyword_any);
        self.keyword_all = clean(self.keyword_all);
        self
    }
}

fn clean(values: Vec<String>) -> Vec<String> {
    let mut out = Vec::new();
    for value in values {
        let v = value.trim().to_string();
        if !v.is_empty() && !out.contains(&v) { out.push(v); }
    }
    out
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SavedSearch {
    pub id: String,
    pub name: String,
    pub query: String,
    pub plan: SearchPlan,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Company {
    pub corporate_number: String,
    pub name: String,
    pub prefecture: Option<String>,
    pub city: Option<String>,
    pub address: Option<String>,
    pub kind: Option<String>,
    pub industry_code: Option<String>,
    pub industry_name: Option<String>,
    pub industry_source: Option<String>,
    pub inferred_industry_code: Option<String>,
    pub inferred_industry_name: Option<String>,
    pub inferred_industry_confidence: Option<f64>,
    pub employees: Option<i64>,
    pub capital: Option<i64>,
    pub established_year: Option<i32>,
    pub website: Option<String>,
    pub phone: Option<String>,
    pub representative: Option<String>,
    pub business_summary: Option<String>,
    pub source_updated_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub rows: Vec<Company>,
    pub total: u64,
    pub page: u32,
    pub page_size: u32,
    pub elapsed_ms: u128,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataStatus {
    pub company_count: u64,
    pub taxonomy_count: u64,
    pub industry_count: u64,
    pub employee_count: u64,
    pub capital_count: u64,
    pub website_count: u64,
    pub phone_count: u64,
    pub address_count: u64,
    pub research_count: u64,
    pub db_path: String,
    pub duckdb_native: bool,
    pub runtime_attached: bool,
    pub duckdb_version: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexStatus {
    pub running: bool,
    pub authenticated: bool,
    pub email: Option<String>,
    pub plan_type: Option<String>,
    pub luna_available: bool,
    pub model: String,
    pub version: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResearchSource {
    pub url: String,
    pub title: Option<String>,
    pub note: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndustryGuess {
    pub code: Option<String>,
    pub name: Option<String>,
    pub confidence: Option<f64>,
    pub rationale: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResearchReport {
    pub corporate_number: String,
    pub company_name: String,
    pub thread_id: String,
    pub summary: String,
    pub transcript: String,
    pub industry_guess: Option<IndustryGuess>,
    #[serde(default)]
    pub findings: Vec<String>,
    #[serde(default)]
    pub sources: Vec<ResearchSource>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SalesforceStatus {
    pub connected: bool,
    pub username: Option<String>,
    pub instance_url: Option<String>,
    pub api_version: String,
}
