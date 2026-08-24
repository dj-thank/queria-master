mod codex;
mod db;
mod models;
mod duckdb_native;
mod salesforce;

use codex::CodexManager;
use db::{Db, PhoneCandidateRecord};
use models::{CodexStatus, Company, DataStatus, ResearchReport, SalesforceStatus, SavedSearch, SearchPlan, SearchResult};
use salesforce::SalesforceManager;
use chrono::{SecondsFormat, Utc};
use serde::Serialize;
use regex::Regex;
use std::collections::HashSet;
use std::net::IpAddr;
use std::path::PathBuf;
use tokio::net::lookup_host;
use tauri::{Manager, State};
use url::Url;

struct AppState {
    db: Db,
    codex: CodexManager,
    salesforce: SalesforceManager,
}

#[derive(Serialize)]
struct AuthUrl { auth_url: String }

#[derive(Serialize)]
struct PhoneCollectionResult {
    phone: Option<String>,
    source_url: String,
    candidates: Vec<PhoneCandidateResult>,
}

#[derive(Serialize, Clone)]
struct PhoneCandidateResult {
    phone: String,
    phone_type: String,
    source_url: String,
    evidence_text: String,
    confidence: f64,
    observed_at: String,
}

fn err<E: std::fmt::Display>(e: E) -> String { e.to_string() }

fn find_runtime_db() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::var("QUERIA_RUNTIME_DB") {
        candidates.push(PathBuf::from(path));
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        candidates.push(PathBuf::from(&home).join("data/queria_runtime_g_fuma.duckdb"));
        candidates.push(PathBuf::from(home).join("data/queria_runtime.duckdb"));
    }
    if let Ok(executable) = std::env::current_exe() {
        if let Some(parent) = executable.parent() {
            candidates.push(parent.join("data/queria_runtime_g_fuma.duckdb"));
            candidates.push(parent.join("data/queria_runtime.duckdb"));
            if let Some(release_root) = parent.parent() {
                candidates.push(release_root.join("data/queria_runtime_g_fuma.duckdb"));
                candidates.push(release_root.join("data/queria_runtime.duckdb"));
            }
        }
    }
    if let Ok(current_dir) = std::env::current_dir() {
        candidates.push(current_dir.join("data/queria_runtime_g_fuma.duckdb"));
        candidates.push(current_dir.join("data/queria_runtime.duckdb"));
    }
    candidates.into_iter().find(|path| path.is_file())
}

#[tauri::command]
async fn bootstrap(state: State<'_, AppState>) -> Result<DataStatus, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.init()).await.map_err(err)?.map_err(err)?;
    data_status(state).await
}

#[tauri::command]
async fn data_status(state: State<'_, AppState>) -> Result<DataStatus, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || {
        let native = duckdb_native::status(&db)?;
        db.status(Some(native.version))
    }).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn search_companies(state: State<'_, AppState>, plan: SearchPlan, page: u32, page_size: u32) -> Result<SearchResult, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.search(plan, page, page_size)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn save_search(state: State<'_, AppState>, name: String, query: String, plan: SearchPlan) -> Result<(), String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.save_search(&name, &query, &plan)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn recent_searches(state: State<'_, AppState>, limit: u32) -> Result<Vec<SavedSearch>, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.recent_searches(limit)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn export_search_csv(state: State<'_, AppState>, plan: SearchPlan, path: String) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.export_search_csv(plan, std::path::Path::new(&path))).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn export_search_xlsx(state: State<'_, AppState>, plan: SearchPlan, path: String) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.export_search_xlsx(plan, std::path::Path::new(&path))).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn add_to_list(state: State<'_, AppState>, list_name: String, corporate_numbers: Vec<String>) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.add_to_list(&list_name, &corporate_numbers)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn add_search_to_list(state: State<'_, AppState>, list_name: String, plan: SearchPlan) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.add_search_to_list(&list_name, plan)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn codex_status(state: State<'_, AppState>) -> Result<CodexStatus, String> { Ok(state.codex.status().await) }

#[tauri::command]
async fn codex_login(state: State<'_, AppState>) -> Result<AuthUrl, String> {
    Ok(AuthUrl { auth_url: state.codex.login().await.map_err(err)? })
}

#[tauri::command]
async fn codex_logout(state: State<'_, AppState>) -> Result<(), String> { state.codex.logout().await.map_err(err) }

#[tauri::command]
async fn codex_plan_search(state: State<'_, AppState>, query: String) -> Result<SearchPlan, String> {
    state.codex.plan_search(&query).await.map_err(err)
}

#[tauri::command]
async fn codex_research_company(state: State<'_, AppState>, company: Company, instruction: String) -> Result<ResearchReport, String> {
    let report = state.codex.research_company(&company, &instruction).await.map_err(err)?;
    let db = state.db.clone();
    let saved = report.clone();
    tokio::task::spawn_blocking(move || db.save_research(&saved)).await.map_err(err)?.map_err(err)?;
    Ok(report)
}

#[tauri::command]
async fn collect_company_phone(state: State<'_, AppState>, company: Company) -> Result<PhoneCollectionResult, String> {
    let website = company.website.clone().ok_or_else(|| "この会社には公式サイトURLがありません".to_string())?;
    let entity_key = company.entity_key.clone().unwrap_or_else(|| company.corporate_number.clone());
    if !Regex::new(r"^\d{13}$").map_err(err)?.is_match(&entity_key) {
        return Err("法人番号が確定していない企業は公式サイト電話収集の対象外です".to_string());
    }
    let parsed = Url::parse(&website).map_err(err)?;
    validate_public_url(&parsed).await.map_err(err)?;
    let allowed_host = normalized_host(&parsed).ok_or_else(|| "公式サイトURLのホスト名を取得できません".to_string())?;
    let redirect_host = allowed_host.clone();
    let client = reqwest::Client::builder()
        .user_agent("CompanyMaster/0.9.1 (+official contact discovery; low rate)")
        .timeout(std::time::Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::custom(move |attempt| {
            if attempt.previous().len() >= 5 {
                return attempt.stop();
            }
            let next_host = attempt.url().host_str().unwrap_or("").to_ascii_lowercase().trim_start_matches("www.").to_string();
            if next_host == redirect_host && matches!(attempt.url().scheme(), "http" | "https") {
                attempt.follow()
            } else {
                attempt.stop()
            }
        }))
        .build()
        .map_err(err)?;
    let robots = load_robots(&client, &parsed, &allowed_host).await;
    let mut queue = vec![parsed.clone()];
    let mut visited = HashSet::new();
    let mut candidates: Vec<PhoneCandidateRecord> = Vec::new();
    let mut last_source_url = website.clone();
    while let Some(page_url) = queue.pop() {
        if visited.len() >= 4 { break; }
        let canonical = page_url.to_string();
        if !visited.insert(canonical.clone()) || !same_host(&page_url, &allowed_host) || !robots.allows(page_url.path()) { continue; }
        let response = match client.get(page_url.clone()).send().await {
            Ok(value) => value,
            Err(_) => continue,
        };
        if !response.status().is_success() { continue; }
        if response.content_length().unwrap_or(0) > 2_000_000 { continue; }
        let response_url = response.url().clone();
        let source_url = response_url.to_string();
        last_source_url = source_url.clone();
        if !same_host(&response_url, &allowed_host) { continue; }
        let body = match response.bytes().await {
            Ok(value) if value.len() <= 2_000_000 => value,
            _ => continue,
        };
        let html = String::from_utf8_lossy(&body);
        let observed_at = now_iso();
        for candidate in extract_phone_candidates(&html, &source_url, &observed_at) {
            if !candidates.iter().any(|current| current.phone == candidate.phone && current.source_url == candidate.source_url) {
                candidates.push(candidate);
            }
        }
        if visited.len() < 4 {
            for link in extract_site_links(&html, &response_url, &allowed_host) {
                if !visited.contains(&link.to_string()) && !queue.iter().any(|item| item == &link) {
                    queue.push(link);
                }
            }
        }
    }
    candidates.sort_by(|left, right| right.confidence.partial_cmp(&left.confidence).unwrap_or(std::cmp::Ordering::Equal));
    candidates.truncate(5);
    let state_name = if candidates.is_empty() { "processed_no_phone" } else { "phone_candidate_found" };
    let db = state.db.clone();
    let db_candidates = candidates.clone();
    let db_entity_key = entity_key.clone();
    let db_website = website.clone();
    tokio::task::spawn_blocking(move || db.save_phone_candidates(&db_entity_key, &db_website, &db_candidates, state_name))
        .await.map_err(err)?.map_err(err)?;
    let result_candidates = candidates.into_iter().map(|candidate| PhoneCandidateResult {
        phone: candidate.phone,
        phone_type: candidate.phone_type,
        source_url: candidate.source_url,
        evidence_text: candidate.evidence_text,
        confidence: candidate.confidence,
        observed_at: candidate.observed_at,
    }).collect::<Vec<_>>();
    Ok(PhoneCollectionResult { phone: result_candidates.first().map(|item| item.phone.clone()), source_url: last_source_url, candidates: result_candidates })
}

#[tauri::command]
async fn duckdb_native_status(state: State<'_, AppState>) -> Result<duckdb_native::NativeDuckDbStatus, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || duckdb_native::status(&db)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn sync_duckdb_company_master(state: State<'_, AppState>) -> Result<duckdb_native::NativeSyncResult, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || duckdb_native::sync_company_master(&db)).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn import_company_file(state: State<'_, AppState>, path: String) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.import_canonical_file(std::path::Path::new(&path))).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn import_industry_taxonomy(state: State<'_, AppState>, path: String) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.import_taxonomy_file(std::path::Path::new(&path))).await.map_err(err)?.map_err(err)
}

#[tauri::command]
async fn salesforce_status(state: State<'_, AppState>) -> Result<SalesforceStatus, String> { Ok(state.salesforce.status().await) }

#[tauri::command]
async fn salesforce_login_start(state: State<'_, AppState>, login_url: String, client_id: String) -> Result<salesforce::SalesforceLoginStart, String> {
    state.salesforce.login_start(&login_url, &client_id).await.map_err(err)
}

#[tauri::command]
async fn salesforce_upsert_list(
    state: State<'_, AppState>,
    list_name: String,
    object_name: String,
    external_id_field: String,
    mapping: Vec<salesforce::FieldMapping>,
) -> Result<salesforce::SalesforceUpsertResult, String> {
    let db = state.db.clone();
    let companies = tokio::task::spawn_blocking(move || db.list_companies(&list_name)).await.map_err(err)?.map_err(err)?;
    state.salesforce.upsert(&companies, &object_name, &external_id_field, &mapping).await.map_err(err)
}

#[tauri::command]
async fn salesforce_job_status(state: State<'_, AppState>, job_id: String) -> Result<salesforce::SalesforceJobStatus, String> {
    state.salesforce.job_status(&job_id).await.map_err(err)
}

#[tauri::command]
async fn salesforce_retry_failed(state: State<'_, AppState>, job_id: String) -> Result<salesforce::SalesforceUpsertResult, String> {
    state.salesforce.retry_failed(&job_id).await.map_err(err)
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let app_data_dir = app.path().app_data_dir()?;
            std::fs::create_dir_all(&app_data_dir)?;
            let db_path = app_data_dir.join("company-master.duckdb");
            let db = match find_runtime_db() {
                Some(runtime_path) => Db::with_runtime(db_path, runtime_path),
                None => Db::new(db_path),
            };
            db.init().map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;

            // A pinned native Codex runtime is bundled with Windows releases. Copy it to app data
            // so upgrades are atomic and the runtime is writable independently of Program Files.
            let resource_dir = app.path().resource_dir()?;
            let bundled_codex = resource_dir.join("bin").join("codex.exe");
            if bundled_codex.is_file() {
                let bin_dir = app_data_dir.join("bin");
                std::fs::create_dir_all(&bin_dir)?;
                std::fs::copy(&bundled_codex, bin_dir.join("codex.exe"))?;
            }

            let workspace = app_data_dir.join("agent-workspace");
            std::fs::create_dir_all(&workspace)?;
            std::fs::write(workspace.join("AGENTS.md"), include_str!("../../agent-workspace/AGENTS.md"))?;

            let codex = CodexManager::new(app_data_dir.clone(), workspace);
            let salesforce = SalesforceManager::new()
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
            app.manage(AppState { db, codex, salesforce });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bootstrap, data_status, search_companies, save_search, recent_searches, export_search_csv, export_search_xlsx, add_to_list, add_search_to_list,
            codex_status, codex_login, codex_logout, codex_plan_search, codex_research_company,
            collect_company_phone,
            duckdb_native_status, sync_duckdb_company_master, import_company_file, import_industry_taxonomy,
            salesforce_status, salesforce_login_start, salesforce_upsert_list, salesforce_job_status, salesforce_retry_failed
        ])
        .run(tauri::generate_context!())
        .expect("error while running CompanyMaster");
}

#[derive(Default)]
struct RobotsRules {
    disallow: Vec<String>,
    allow: Vec<String>,
}

impl RobotsRules {
    fn allows(&self, path: &str) -> bool {
        let allowed_len = self.allow.iter().filter(|rule| path.starts_with(rule.as_str())).map(String::len).max().unwrap_or(0);
        let disallowed_len = self.disallow.iter().filter(|rule| !rule.is_empty() && path.starts_with(rule.as_str())).map(String::len).max().unwrap_or(0);
        allowed_len >= disallowed_len
    }
}

fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn normalized_host(url: &Url) -> Option<String> {
    url.host_str().map(|host| host.to_ascii_lowercase().trim_start_matches("www.").to_string())
}

fn same_host(url: &Url, expected: &str) -> bool {
    normalized_host(url).as_deref() == Some(expected)
}

fn is_public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(value) => {
            let octets = value.octets();
            !value.is_private()
                && !value.is_loopback()
                && !value.is_link_local()
                && !value.is_unspecified()
                && !value.is_multicast()
                && !value.is_broadcast()
                && !(octets[0] == 100 && (64..=127).contains(&octets[1]))
                && !(octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
                && !(octets[0] == 192 && octets[1] == 0 && octets[2] == 2)
                && !(octets[0] == 198 && octets[1] == 18)
                && !(octets[0] == 198 && octets[1] == 19)
                && !(octets[0] == 198 && octets[1] == 51 && octets[2] == 100)
                && !(octets[0] == 203 && octets[1] == 0 && octets[2] == 113)
                && octets[0] < 224
        }
        IpAddr::V6(value) => {
            let segments = value.segments();
            !value.is_loopback()
                && !value.is_unspecified()
                && !value.is_multicast()
                && (segments[0] & 0xfe00) != 0xfc00
                && (segments[0] & 0xffc0) != 0xfe80
        }
    }
}

async fn validate_public_url(url: &Url) -> Result<(), String> {
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() || url.username() != "" || url.password().is_some() {
        return Err("公式サイトURLが不正です".to_string());
    }
    let host = normalized_host(url).ok_or_else(|| "公式サイトURLのホスト名を取得できません".to_string())?;
    if host == "localhost" || host.ends_with(".localhost") || host.ends_with(".local") || host.ends_with(".internal") {
        return Err("ローカル・内部ホストは電話収集の対象外です".to_string());
    }
    let port = url.port_or_known_default().ok_or_else(|| "公式サイトURLのポートを取得できません".to_string())?;
    let addresses = lookup_host((host.as_str(), port)).await.map_err(|_| "公式サイトのホストを解決できません".to_string())?.collect::<Vec<_>>();
    if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(address.ip())) {
        return Err("非公開ネットワークのホストは電話収集の対象外です".to_string());
    }
    Ok(())
}

async fn load_robots(client: &reqwest::Client, website: &Url, host: &str) -> RobotsRules {
    let mut robots_url = website.clone();
    robots_url.set_path("/robots.txt");
    robots_url.set_query(None);
    if !same_host(&robots_url, host) { return RobotsRules::default(); }
    let response = match client.get(robots_url).send().await {
        Ok(value) if value.status().is_success() => value,
        _ => return RobotsRules::default(),
    };
    let body = match response.bytes().await {
        Ok(value) if value.len() <= 512_000 => value,
        _ => return RobotsRules::default(),
    };
    let text = String::from_utf8_lossy(&body);
    let mut rules = RobotsRules::default();
    let mut relevant = false;
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        let Some((key, value)) = line.split_once(':') else { continue; };
        let key = key.trim().to_ascii_lowercase();
        let value = value.trim().to_string();
        if key == "user-agent" {
            relevant = value == "*" || value.to_ascii_lowercase().contains("companymaster");
        } else if relevant && key == "disallow" {
            rules.disallow.push(value);
        } else if relevant && key == "allow" {
            rules.allow.push(value);
        }
    }
    rules
}

fn extract_site_links(html: &str, base: &Url, host: &str) -> Vec<Url> {
    let Ok(pattern) = Regex::new(r#"(?is)href\s*=\s*[\"']([^\"']+)[\"']"#) else { return Vec::new(); };
    let hints = ["company", "corporate", "about", "profile", "contact", "outline", "overview", "会社", "企業", "概要", "問い合わせ", "連絡先"];
    let mut links = Vec::new();
    for captures in pattern.captures_iter(html) {
        let href = captures.get(1).map(|value| value.as_str().trim()).unwrap_or("");
        if href.is_empty() || href.starts_with("#") || href.starts_with("mailto:") || href.starts_with("tel:") || href.starts_with("javascript:") { continue; }
        let Ok(url) = base.join(href) else { continue; };
        let lower = format!("{} {}", url, href).to_ascii_lowercase();
        if !same_host(&url, host) || !matches!(url.scheme(), "http" | "https") || !hints.iter().any(|hint| lower.contains(&hint.to_ascii_lowercase())) { continue; }
        if !links.iter().any(|item: &Url| item == &url) { links.push(url); }
        if links.len() >= 12 { break; }
    }
    links
}

fn classify_phone(context: &str, tel_link: bool) -> (String, f64) {
    let lower = context.to_ascii_lowercase();
    if tel_link && (lower.contains("代表") || lower.contains("本社")) { return ("head_office".to_string(), 0.90); }
    if lower.contains("fax") || lower.contains("ｆａｘ") { return ("fax".to_string(), 0.50); }
    if lower.contains("採用") || lower.contains("求人") || lower.contains("応募") { return ("recruit".to_string(), 0.65); }
    if lower.contains("支店") || lower.contains("営業所") || lower.contains("店舗") { return ("branch".to_string(), 0.65); }
    if lower.contains("サポート") || lower.contains("ヘルプ") { return ("support".to_string(), 0.70); }
    if lower.contains("代表") || lower.contains("大代表") || lower.contains("本社") { return ("head_office".to_string(), 0.90); }
    if tel_link { return ("unclassified".to_string(), 0.85); }
    ("unclassified".to_string(), 0.70)
}

fn extract_phone_candidates(html: &str, source_url: &str, observed_at: &str) -> Vec<PhoneCandidateRecord> {
    let Ok(tel_pattern) = Regex::new(r"(?i)tel:\s*([+0-9０-９\s\-‐‑‒–—−ー()（）]{7,})") else { return Vec::new(); };
    let Ok(visible_pattern) = Regex::new(r"(?:\+81|0)[0-9０-９\s\-‐‑‒–—−ー()（）]{7,}[0-9０-９]") else { return Vec::new(); };
    let mut candidates = Vec::new();
    let mut add_candidate = |raw: &str, matched_start: usize, matched_end: usize, tel_link: bool| {
        let Some(phone) = normalize_phone(raw) else { return; };
        if candidates.iter().any(|candidate: &PhoneCandidateRecord| candidate.phone == phone) { return; }
        let start = matched_start.saturating_sub(100);
        let end = (matched_end + 100).min(html.len());
        let context = html.get(start..end).unwrap_or(raw).replace(['\r', '\n', '\t'], " ");
        let (phone_type, confidence) = classify_phone(&context, tel_link);
        candidates.push(PhoneCandidateRecord {
            phone,
            phone_type,
            source_url: source_url.to_string(),
            evidence_text: context,
            confidence,
            observed_at: observed_at.to_string(),
            status: "official_site_candidate".to_string(),
        });
    };
    for matched in tel_pattern.captures_iter(html) {
        if let Some(value) = matched.get(1) { add_candidate(value.as_str(), matched.get(0).unwrap().start(), matched.get(0).unwrap().end(), true); }
    }
    for matched in visible_pattern.find_iter(html) {
        add_candidate(matched.as_str(), matched.start(), matched.end(), false);
    }
    candidates
}

fn normalize_phone(value: &str) -> Option<String> {
    let mut normalized = String::new();
    for ch in value.chars() {
        let digit = match ch {
            '０'..='９' => char::from_u32('0' as u32 + (ch as u32 - '０' as u32)),
            '+' if normalized.is_empty() => Some('+'),
            '0'..='9' => Some(ch),
            _ => None,
        };
        if let Some(ch) = digit { normalized.push(ch); }
    }
    let mut digits = normalized.chars().filter(|ch| ch.is_ascii_digit()).collect::<String>();
    if digits.starts_with("81") && digits.len() >= 11 { digits = format!("0{}", &digits[2..]); }
    if (10..=11).contains(&digits.len()) && digits.starts_with('0') { Some(digits) } else { None }
}

#[cfg(test)]
mod tests {
    use super::*;
    use duckdb::Connection;
    use std::fs;

    #[test]
    fn phone_candidates_are_normalized_and_classified() {
        let html = r#"<a href="tel:+81-3-1234-5678">本社代表</a><p>FAX 03-5555-6666</p>"#;
        let candidates = extract_phone_candidates(html, "https://example.com/contact", "2026-08-21T00:00:00Z");
        assert_eq!(candidates[0].phone, "0312345678");
        assert_eq!(candidates[0].phone_type, "head_office");
        assert_eq!(candidates[1].phone, "0355556666");
        assert_eq!(candidates[1].phone_type, "fax");
    }

    #[test]
    fn private_hosts_are_rejected() {
        assert!(!is_public_ip("127.0.0.1".parse().unwrap()));
        assert!(!is_public_ip("10.0.0.1".parse().unwrap()));
        assert!(!is_public_ip("::1".parse().unwrap()));
        assert!(is_public_ip("8.8.8.8".parse().unwrap()));
    }

    #[test]
    fn g_runtime_view_supports_industry_and_region_search() {
        let root = std::env::current_dir().unwrap().join(format!(".test-rust-g3741-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let runtime = root.join("runtime.duckdb");
        let sidecar = root.join("sidecar.duckdb");
        let source = Connection::open(&runtime).unwrap();
        source.execute_batch(
            r#"
            CREATE SCHEMA core;
            CREATE TABLE core.g_companies(
              entity_key VARCHAR, corporate_number VARCHAR, fuma_id VARCHAR, source_kind VARCHAR,
              name VARCHAR, prefecture VARCHAR, city VARCHAR, address VARCHAR, kind VARCHAR,
              industry_code VARCHAR, industry_name VARCHAR, industry_source VARCHAR,
              industry_middle_code VARCHAR, industry_middle_name VARCHAR,
              industry_small_code VARCHAR, industry_small_name VARCHAR,
              industry_detail_code VARCHAR, industry_detail_name VARCHAR,
              employees BIGINT, capital BIGINT, established_year BIGINT, website VARCHAR, phone VARCHAR,
              representative VARCHAR, business_summary VARCHAR, source_updated_at VARCHAR,
              phone_type VARCHAR, phone_source_url VARCHAR, phone_confidence DOUBLE,
              phone_evidence_text VARCHAR, phone_observed_at VARCHAR, phone_status VARCHAR
            );
            INSERT INTO core.g_companies VALUES
              ('1234567890123','1234567890123','fuma-test','fuma+national','テスト株式会社','東京都','千代田区','東京都千代田区永田町1-1','株式会社','G|39|G39|391|G391|3911|G3911','情報通信業','FUMA/JSIC2023','39','情報サービス業','391','ソフトウェア業','3911','受託開発ソフトウェア業',10,1000000,2020,'https://example.com',NULL,'代表者','SaaS','2026-08-21',NULL,NULL,NULL,NULL,NULL,'pending_official_site');
            "#,
        ).unwrap();
        drop(source);

        let db = Db::with_runtime(sidecar, runtime);
        let mut plan = SearchPlan::default();
        plan.industry_codes = vec!["G".to_string()];
        plan.limit = 10;
        let result = db.search(plan, 1, 10).unwrap();
        assert_eq!(result.total, 1);
        assert_eq!(result.rows[0].prefecture.as_deref(), Some("東京都"));

        let mut regional = SearchPlan::default();
        regional.industry_codes = vec!["3911".to_string()];
        regional.prefectures = vec!["東京都".to_string()];
        regional.limit = 10;
        assert_eq!(db.search(regional, 1, 10).unwrap().total, 1);

        let _ = fs::remove_dir_all(&root);
    }
}
