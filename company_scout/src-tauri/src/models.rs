use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
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
    pub phone_required: Option<bool>,
    #[serde(default)]
    pub keyword_any: Vec<String>,
    #[serde(default)]
    pub keyword_all: Vec<String>,
    pub sort_by: Option<String>,
    pub sort_direction: Option<String>,
    #[serde(default = "default_limit")]
    pub limit: u32,
}

fn default_limit() -> u32 {
    30_000
}

impl Default for SearchPlan {
    fn default() -> Self {
        Self {
            text: None,
            prefectures: Vec::new(),
            cities: Vec::new(),
            industry_codes: Vec::new(),
            industry_terms: Vec::new(),
            company_kinds: Vec::new(),
            min_employees: None,
            max_employees: None,
            min_capital: None,
            max_capital: None,
            established_from: None,
            established_to: None,
            website_required: None,
            phone_required: None,
            keyword_any: Vec::new(),
            keyword_all: Vec::new(),
            sort_by: None,
            sort_direction: None,
            limit: default_limit(),
        }
    }
}

/// The bundled public snapshot currently contains 5.8M corporations. Keep
/// the safety ceiling above that size so "全件抽出" does not silently stop at
/// two million rows while still preventing an accidental unbounded request.
pub const MAX_SEARCH_LIMIT: u32 = 10_000_000;

impl SearchPlan {
    pub fn normalize(mut self) -> Self {
        self.limit = self.limit.clamp(1, MAX_SEARCH_LIMIT);
        self.text = clean_optional(self.text);
        self.prefectures = clean(self.prefectures);
        self.cities = clean(self.cities);
        self.industry_codes = clean(self.industry_codes);
        self.industry_terms = clean(self.industry_terms);
        self.company_kinds = normalize_company_kinds(self.company_kinds);
        self.keyword_any = clean(self.keyword_any);
        self.keyword_all = clean(self.keyword_all);
        self.sort_by = clean_optional(self.sort_by).map(|value| value.to_ascii_lowercase());
        self.sort_direction =
            clean_optional(self.sort_direction).map(|value| value.to_ascii_lowercase());
        self
    }
}

fn clean(values: Vec<String>) -> Vec<String> {
    let mut out = Vec::new();
    for value in values {
        let v = value.trim().to_string();
        if !v.is_empty() && !out.contains(&v) {
            out.push(v);
        }
    }
    out
}

fn clean_optional(value: Option<String>) -> Option<String> {
    value.and_then(|value| {
        let value = value.trim().to_string();
        (!value.is_empty()).then_some(value)
    })
}

/// gBizINFO/NTA store a numeric corporate-kind code, while old saved UI
/// searches stored the Japanese label. Keep those searches usable while the
/// current UI sends codes directly.
fn normalize_company_kinds(values: Vec<String>) -> Vec<String> {
    clean(values)
        .into_iter()
        .map(|value| match value.as_str() {
            "国の機関" => "101".to_string(),
            "地方公共団体" => "201".to_string(),
            "株式会社" => "301".to_string(),
            "有限会社" => "302".to_string(),
            "合名会社" => "303".to_string(),
            "合資会社" => "304".to_string(),
            "合同会社" => "305".to_string(),
            "医療法人" | "学校法人" | "社会福祉法人" | "NPO法人" | "特定非営利活動法人" => {
                "399".to_string()
            }
            "外国会社等" => "401".to_string(),
            _ => value,
        })
        .fold(Vec::new(), |mut output, value| {
            if !output.contains(&value) {
                output.push(value);
            }
            output
        })
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
    pub engine: String,
    #[serde(default)]
    pub warnings: Vec<String>,
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
    pub search_index_available: bool,
    pub search_index_path: Option<String>,
    pub search_index_status: Option<String>,
    pub search_index_row_count: Option<u64>,
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

#[cfg(test)]
mod tests {
    use super::SearchPlan;

    #[test]
    fn normalizes_legacy_corporate_kind_labels_and_optional_fields() {
        let plan = SearchPlan {
            text: Some("  クラウド  ".to_string()),
            company_kinds: vec![
                "株式会社".to_string(),
                "301".to_string(),
                "医療法人".to_string(),
            ],
            sort_by: Some(" EMPLOYEES ".to_string()),
            sort_direction: Some(" DESC ".to_string()),
            ..SearchPlan::default()
        }
        .normalize();

        assert_eq!(plan.text.as_deref(), Some("クラウド"));
        assert_eq!(
            plan.company_kinds,
            vec!["301".to_string(), "399".to_string()]
        );
        assert_eq!(plan.sort_by.as_deref(), Some("employees"));
        assert_eq!(plan.sort_direction.as_deref(), Some("desc"));
        assert_eq!(plan.limit, 30_000);
    }
}
