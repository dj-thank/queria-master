mod codex;
mod db;
mod duckdb_native;
mod models;
mod public_enrichment;
mod salesforce;

use anyhow::Context;
use codex::CodexManager;
use db::Db;
use models::{
    CodexStatus, Company, DataStatus, ResearchReport, SalesforceStatus, SavedSearch, SearchPlan,
    SearchResult,
};
use public_enrichment::{
    PublicEnrichmentManager, PublicEnrichmentOperation, PublicEnrichmentStatus,
};
use regex::Regex;
use salesforce::SalesforceManager;
use serde::Serialize;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::path::{Path, PathBuf};
use tauri::{Manager, State};
use url::Url;

struct AppState {
    db: Db,
    codex: CodexManager,
    public_enrichment: PublicEnrichmentManager,
    salesforce: SalesforceManager,
}

#[derive(Serialize)]
struct AuthUrl {
    auth_url: String,
}

#[derive(Serialize)]
struct PhoneCollectionResult {
    phone: Option<String>,
    source_url: String,
}

fn err<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

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
        // `tauri dev` can start from either company_scout/ or src-tauri/.
        // Check only the nearby project roots rather than walking arbitrary
        // ancestors, while release builds continue to prefer bundled paths.
        for root in current_dir.ancestors().take(3) {
            candidates.push(root.join("data/queria_runtime_g_fuma.duckdb"));
            candidates.push(root.join("data/queria_runtime.duckdb"));
        }
    }
    candidates.into_iter().find(|path| path.is_file())
}

fn find_search_index(runtime_path: Option<&Path>) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::var("QUERIA_SEARCH_INDEX") {
        candidates.push(PathBuf::from(path));
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        candidates.push(PathBuf::from(&home).join("data/search_g_fuma.sqlite"));
        candidates.push(PathBuf::from(home).join("data/search.sqlite"));
    }
    if let Some(parent) = runtime_path.and_then(Path::parent) {
        candidates.push(parent.join("search_g_fuma.sqlite"));
        candidates.push(parent.join("search.sqlite"));
    }
    if let Ok(executable) = std::env::current_exe() {
        if let Some(parent) = executable.parent() {
            candidates.push(parent.join("data/search_g_fuma.sqlite"));
            candidates.push(parent.join("data/search.sqlite"));
            if let Some(release_root) = parent.parent() {
                candidates.push(release_root.join("data/search_g_fuma.sqlite"));
                candidates.push(release_root.join("data/search.sqlite"));
            }
        }
    }
    if let Ok(current_dir) = std::env::current_dir() {
        for root in current_dir.ancestors().take(3) {
            candidates.push(root.join("data/search_g_fuma.sqlite"));
            candidates.push(root.join("data/search.sqlite"));
        }
    }
    candidates.into_iter().find_map(|path| {
        if path.is_file() {
            path.canonicalize().ok()
        } else {
            None
        }
    })
}

fn find_runtime_target(app_data_dir: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("QUERIA_RUNTIME_DB") {
        return PathBuf::from(path);
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        return PathBuf::from(home).join("data/queria_runtime.duckdb");
    }
    default_runtime_target(app_data_dir)
}

fn default_runtime_target(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("data").join("queria_runtime.duckdb")
}

fn find_canonical_db(
    existing_runtime_path: Option<&Path>,
    runtime_target: Option<&Path>,
) -> Option<PathBuf> {
    // The canonical enrichment publisher consumes core.companies. A v0.10 G
    // runtime remains a valid read fallback, but its core.g_companies source
    // is not advertised as publish-capable through this bridge.
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::var("QUERIA_CANONICAL_DB") {
        candidates.push(PathBuf::from(path));
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        candidates.push(PathBuf::from(home).join("data/queria_master.duckdb"));
    }
    for runtime_path in [existing_runtime_path, runtime_target]
        .into_iter()
        .flatten()
    {
        if let Some(parent) = runtime_path.parent() {
            candidates.push(parent.join("queria_master.duckdb"));
        }
    }
    if let Ok(current_dir) = std::env::current_dir() {
        for root in current_dir.ancestors().take(3) {
            candidates.push(root.join("data/queria_master.duckdb"));
        }
    }
    candidates.into_iter().find(|path| path.is_file())
}

fn find_enrichment_target(runtime_path: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("QUERIA_ENRICHMENT_DB") {
        return PathBuf::from(path);
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        return PathBuf::from(home).join("data/queria_enrichment.duckdb");
    }
    runtime_path
        .parent()
        .map(|parent| parent.join("queria_enrichment.duckdb"))
        .unwrap_or_else(|| PathBuf::from("queria_enrichment.duckdb"))
}

fn find_search_index_target(runtime_path: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("QUERIA_SEARCH_INDEX") {
        return PathBuf::from(path);
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        return PathBuf::from(home).join("data/search.sqlite");
    }
    runtime_path
        .parent()
        .map(|parent| parent.join("search.sqlite"))
        .unwrap_or_else(|| PathBuf::from("search.sqlite"))
}

#[tauri::command]
async fn bootstrap(state: State<'_, AppState>) -> Result<DataStatus, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.init())
        .await
        .map_err(err)?
        .map_err(err)?;
    data_status(state).await
}

#[tauri::command]
async fn data_status(state: State<'_, AppState>) -> Result<DataStatus, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || {
        let native = duckdb_native::status(&db)?;
        db.status(Some(native.version))
    })
    .await
    .map_err(err)?
    .map_err(err)
}

#[tauri::command]
async fn search_companies(
    state: State<'_, AppState>,
    plan: SearchPlan,
    page: u32,
    page_size: u32,
) -> Result<SearchResult, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.search(plan, page, page_size))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn save_search(
    state: State<'_, AppState>,
    name: String,
    query: String,
    plan: SearchPlan,
) -> Result<(), String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.save_search(&name, &query, &plan))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn recent_searches(
    state: State<'_, AppState>,
    limit: u32,
) -> Result<Vec<SavedSearch>, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.recent_searches(limit))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn export_search_csv(
    state: State<'_, AppState>,
    plan: SearchPlan,
    path: String,
) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.export_search_csv(plan, std::path::Path::new(&path)))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn export_search_xlsx(
    state: State<'_, AppState>,
    plan: SearchPlan,
    path: String,
) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.export_search_xlsx(plan, std::path::Path::new(&path)))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn add_to_list(
    state: State<'_, AppState>,
    list_name: String,
    corporate_numbers: Vec<String>,
) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.add_to_list(&list_name, &corporate_numbers))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn add_search_to_list(
    state: State<'_, AppState>,
    list_name: String,
    plan: SearchPlan,
) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.add_search_to_list(&list_name, plan))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn codex_status(state: State<'_, AppState>) -> Result<CodexStatus, String> {
    Ok(state.codex.status().await)
}

#[tauri::command]
async fn codex_login(state: State<'_, AppState>) -> Result<AuthUrl, String> {
    Ok(AuthUrl {
        auth_url: state.codex.login().await.map_err(err)?,
    })
}

#[tauri::command]
async fn codex_logout(state: State<'_, AppState>) -> Result<(), String> {
    state.codex.logout().await.map_err(err)
}

#[tauri::command]
async fn codex_plan_search(
    state: State<'_, AppState>,
    query: String,
) -> Result<SearchPlan, String> {
    state.codex.plan_search(&query).await.map_err(err)
}

#[tauri::command]
async fn codex_research_company(
    state: State<'_, AppState>,
    company: Company,
    instruction: String,
) -> Result<ResearchReport, String> {
    let report = state
        .codex
        .research_company(&company, &instruction)
        .await
        .map_err(err)?;
    let db = state.db.clone();
    let saved = report.clone();
    tokio::task::spawn_blocking(move || db.save_research(&saved))
        .await
        .map_err(err)?
        .map_err(err)?;
    Ok(report)
}

#[tauri::command]
async fn collect_company_phone(
    state: State<'_, AppState>,
    company: Company,
) -> Result<PhoneCollectionResult, String> {
    if !Regex::new(r"^\d{13}$")
        .map_err(err)?
        .is_match(&company.corporate_number)
    {
        return Err("法人番号を確認できないFUMAレコードには電話番号を保存できません".to_string());
    }
    let website = company
        .website
        .clone()
        .ok_or_else(|| "この会社には公式サイトURLがありません".to_string())?;
    let parsed = Url::parse(&website).map_err(err)?;
    let (final_url, body) = fetch_public_html(parsed).await.map_err(err)?;
    let source_url = final_url.to_string();
    let html = String::from_utf8_lossy(&body);
    let phone = extract_phone(&html);
    if let Some(phone_value) = &phone {
        let db = state.db.clone();
        let corporate_number = company.corporate_number.clone();
        let evidence = format!("公式サイト {} から電話番号候補を抽出", source_url);
        let phone_for_db = phone_value.clone();
        let source_for_db = source_url.clone();
        tokio::task::spawn_blocking(move || {
            db.save_phone_override(&corporate_number, &phone_for_db, &source_for_db, &evidence)
        })
        .await
        .map_err(err)?
        .map_err(err)?;
    }
    Ok(PhoneCollectionResult { phone, source_url })
}

#[tauri::command]
async fn duckdb_native_status(
    state: State<'_, AppState>,
) -> Result<duckdb_native::NativeDuckDbStatus, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || duckdb_native::status(&db))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn sync_duckdb_company_master(
    state: State<'_, AppState>,
) -> Result<duckdb_native::NativeSyncResult, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || duckdb_native::sync_company_master(&db))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn import_company_file(state: State<'_, AppState>, path: String) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.import_canonical_file(std::path::Path::new(&path)))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn import_industry_taxonomy(state: State<'_, AppState>, path: String) -> Result<u64, String> {
    let db = state.db.clone();
    tokio::task::spawn_blocking(move || db.import_taxonomy_file(std::path::Path::new(&path)))
        .await
        .map_err(err)?
        .map_err(err)
}

#[tauri::command]
async fn public_enrichment_status(
    state: State<'_, AppState>,
) -> Result<PublicEnrichmentStatus, String> {
    Ok(state.public_enrichment.status().await)
}

#[tauri::command]
async fn public_enrichment_prepare(
    state: State<'_, AppState>,
    source_path: String,
    sheet_name: Option<String>,
    replace: bool,
) -> Result<PublicEnrichmentOperation, String> {
    state
        .public_enrichment
        .prepare(
            std::path::Path::new(&source_path),
            sheet_name.as_deref(),
            replace,
        )
        .await
        .map_err(err)
}

#[tauri::command]
async fn public_enrichment_make_assignment(
    state: State<'_, AppState>,
    output_path: String,
    chunk_size: u32,
) -> Result<PublicEnrichmentOperation, String> {
    state
        .public_enrichment
        .make_assignment(std::path::Path::new(&output_path), chunk_size)
        .await
        .map_err(err)
}

#[tauri::command]
async fn public_enrichment_run_all(
    state: State<'_, AppState>,
    input_dir: String,
) -> Result<PublicEnrichmentOperation, String> {
    state
        .public_enrichment
        .run_all(std::path::Path::new(&input_dir), false)
        .await
        .map_err(err)
}

#[tauri::command]
async fn salesforce_status(state: State<'_, AppState>) -> Result<SalesforceStatus, String> {
    Ok(state.salesforce.status().await)
}

#[tauri::command]
async fn salesforce_login_start(
    state: State<'_, AppState>,
    login_url: String,
    client_id: String,
) -> Result<salesforce::SalesforceLoginStart, String> {
    state
        .salesforce
        .login_start(&login_url, &client_id)
        .await
        .map_err(err)
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
    let companies = tokio::task::spawn_blocking(move || db.list_companies(&list_name))
        .await
        .map_err(err)?
        .map_err(err)?;
    state
        .salesforce
        .upsert(&companies, &object_name, &external_id_field, &mapping)
        .await
        .map_err(err)
}

#[tauri::command]
async fn salesforce_job_status(
    state: State<'_, AppState>,
    job_id: String,
) -> Result<salesforce::SalesforceJobStatus, String> {
    state.salesforce.job_status(&job_id).await.map_err(err)
}

#[tauri::command]
async fn salesforce_retry_failed(
    state: State<'_, AppState>,
    job_id: String,
) -> Result<salesforce::SalesforceUpsertResult, String> {
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
            let runtime_path = find_runtime_db();
            let search_index_path = find_search_index(runtime_path.as_deref());
            let runtime_target = find_runtime_target(&app_data_dir);
            let canonical_path =
                find_canonical_db(runtime_path.as_deref(), Some(runtime_target.as_path()));
            let enrichment_path = find_enrichment_target(&runtime_target);
            let search_index_target = find_search_index_target(&runtime_target);
            if let Some(parent) = runtime_target
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty())
            {
                std::fs::create_dir_all(parent)?;
            }
            let db = Db::with_runtime(db_path, runtime_target.clone())
                .with_runtime_fallback(runtime_path.clone())
                .with_search_index(Some(search_index_target.clone()))
                .with_search_index_fallback(search_index_path.clone());
            db.init()
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;

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
            std::fs::write(
                workspace.join("AGENTS.md"),
                include_str!("../../agent-workspace/AGENTS.md"),
            )?;

            let codex = CodexManager::new(app_data_dir.clone(), workspace);
            let public_enrichment = PublicEnrichmentManager::new(
                app_data_dir.clone(),
                resource_dir.clone(),
                canonical_path,
                Some(enrichment_path),
                Some(runtime_target),
                Some(search_index_target),
            )
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
            let salesforce = SalesforceManager::new()
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
            app.manage(AppState {
                db,
                codex,
                public_enrichment,
                salesforce,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            bootstrap,
            data_status,
            search_companies,
            save_search,
            recent_searches,
            export_search_csv,
            export_search_xlsx,
            add_to_list,
            add_search_to_list,
            codex_status,
            codex_login,
            codex_logout,
            codex_plan_search,
            codex_research_company,
            collect_company_phone,
            duckdb_native_status,
            sync_duckdb_company_master,
            import_company_file,
            import_industry_taxonomy,
            public_enrichment_status,
            public_enrichment_prepare,
            public_enrichment_make_assignment,
            public_enrichment_run_all,
            salesforce_status,
            salesforce_login_start,
            salesforce_upsert_list,
            salesforce_job_status,
            salesforce_retry_failed
        ])
        .run(tauri::generate_context!())
        .expect("error while running CompanyMaster");
}

const MAX_PHONE_PAGE_BYTES: usize = 8_000_000;
const MAX_PHONE_REDIRECTS: usize = 5;

#[derive(Debug)]
struct ResolvedPublicTarget {
    host: Option<String>,
    addresses: Vec<SocketAddr>,
}

async fn fetch_public_html(mut url: Url) -> anyhow::Result<(Url, Vec<u8>)> {
    for redirect_count in 0..=MAX_PHONE_REDIRECTS {
        let target = resolve_public_target(&url).await?;
        let mut builder = reqwest::Client::builder()
            .user_agent("CompanyMaster/0.10 contact-enrichment")
            .timeout(std::time::Duration::from_secs(15))
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy();
        if let Some(host) = target.host.as_deref() {
            if url.host_str() != Some(host) {
                url.set_host(Some(host))
                    .map_err(|_| anyhow::anyhow!("公式サイトのホスト名を正規化できません"))?;
            }
            builder = builder.resolve_to_addrs(host, &target.addresses);
        }
        let client = builder.build()?;
        let mut response = client.get(url.clone()).send().await?;

        if response.status().is_redirection() {
            if redirect_count == MAX_PHONE_REDIRECTS {
                return Err(anyhow::anyhow!("公式サイトのリダイレクト回数が多すぎます"));
            }
            let location = response
                .headers()
                .get(reqwest::header::LOCATION)
                .context("公式サイトのリダイレクト先がありません")?
                .to_str()
                .context("公式サイトのリダイレクト先が不正です")?;
            let next = url
                .join(location)
                .context("公式サイトのリダイレクト先URLが不正です")?;
            if url.scheme() == "https" && next.scheme() != "https" {
                return Err(anyhow::anyhow!(
                    "HTTPSから安全でないURLへのリダイレクトを拒否しました"
                ));
            }
            url = next;
            continue;
        }
        if !response.status().is_success() {
            return Err(anyhow::anyhow!(
                "公式サイトの取得に失敗しました: {}",
                response.status()
            ));
        }
        if response.content_length().unwrap_or(0) > MAX_PHONE_PAGE_BYTES as u64 {
            return Err(anyhow::anyhow!(
                "公式サイトが大きすぎるため電話番号調査を中止しました"
            ));
        }
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .map(str::to_ascii_lowercase);
        if content_type.as_deref().is_some_and(|value| {
            !value.starts_with("text/html") && !value.starts_with("application/xhtml+xml")
        }) {
            return Err(anyhow::anyhow!("公式サイトの応答がHTMLではありません"));
        }

        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if body.len().saturating_add(chunk.len()) > MAX_PHONE_PAGE_BYTES {
                return Err(anyhow::anyhow!(
                    "公式サイトが大きすぎるため電話番号調査を中止しました"
                ));
            }
            body.extend_from_slice(&chunk);
        }
        if content_type.is_none() && !looks_like_html(&body) {
            return Err(anyhow::anyhow!("公式サイトの応答がHTMLではありません"));
        }
        return Ok((url, body));
    }
    unreachable!("redirect loop always returns or continues within the bound")
}

async fn resolve_public_target(url: &Url) -> anyhow::Result<ResolvedPublicTarget> {
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(anyhow::anyhow!(
            "公式サイトURLはHTTPまたはHTTPSで指定してください"
        ));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(anyhow::anyhow!(
            "認証情報を含む公式サイトURLは使用できません"
        ));
    }
    let port = url
        .port_or_known_default()
        .context("公式サイトURLのポート番号が不正です")?;
    if !matches!(port, 80 | 443) {
        return Err(anyhow::anyhow!(
            "公式サイトURLの非標準ポートは使用できません"
        ));
    }

    match url.host().context("公式サイトURLにホスト名がありません")? {
        url::Host::Ipv4(address) => {
            let address = IpAddr::V4(address);
            ensure_public_ip(address)?;
            Ok(ResolvedPublicTarget {
                host: None,
                addresses: vec![SocketAddr::new(address, port)],
            })
        }
        url::Host::Ipv6(address) => {
            let address = IpAddr::V6(address);
            ensure_public_ip(address)?;
            Ok(ResolvedPublicTarget {
                host: None,
                addresses: vec![SocketAddr::new(address, port)],
            })
        }
        url::Host::Domain(domain) => {
            let domain = domain.trim_end_matches('.').to_ascii_lowercase();
            if is_local_hostname(&domain) {
                return Err(anyhow::anyhow!(
                    "ローカルまたは内部向けホスト名には接続できません"
                ));
            }
            let mut addresses = tokio::net::lookup_host((domain.as_str(), port))
                .await
                .with_context(|| format!("公式サイトのDNS解決に失敗しました: {domain}"))?
                .collect::<Vec<_>>();
            addresses.sort_unstable();
            addresses.dedup();
            if addresses.is_empty() {
                return Err(anyhow::anyhow!("公式サイトのDNS応答が空です"));
            }
            // Reject a mixed public/private answer rather than allowing the
            // HTTP client to choose an unsafe address. The validated public
            // answers are then pinned into reqwest to close the DNS-rebinding
            // gap between validation and connection.
            for address in &addresses {
                ensure_public_ip(address.ip())?;
            }
            Ok(ResolvedPublicTarget {
                host: Some(domain),
                addresses,
            })
        }
    }
}

fn is_local_hostname(host: &str) -> bool {
    host == "localhost"
        || [
            ".localhost",
            ".local",
            ".internal",
            ".home",
            ".lan",
            ".onion",
        ]
        .iter()
        .any(|suffix| host.ends_with(suffix))
}

fn ensure_public_ip(address: IpAddr) -> anyhow::Result<()> {
    let public = match address {
        IpAddr::V4(address) => is_public_ipv4(address),
        IpAddr::V6(address) => is_public_ipv6(address),
    };
    if public {
        Ok(())
    } else {
        Err(anyhow::anyhow!(
            "ローカル・プライベート・予約済みIPアドレスには接続できません"
        ))
    }
}

fn is_public_ipv4(address: Ipv4Addr) -> bool {
    let [a, b, c, _] = address.octets();
    !(a == 0
        || a == 10
        || a == 127
        || a >= 224
        || (a == 100 && (64..=127).contains(&b))
        || (a == 169 && b == 254)
        || (a == 172 && (16..=31).contains(&b))
        || (a == 192 && b == 0 && c == 0)
        || (a == 192 && b == 0 && c == 2)
        || (a == 192 && b == 168)
        || (a == 198 && (b == 18 || b == 19))
        || (a == 198 && b == 51 && c == 100)
        || (a == 203 && b == 0 && c == 113))
}

fn is_public_ipv6(address: Ipv6Addr) -> bool {
    if let Some(ipv4) = address.to_ipv4() {
        return is_public_ipv4(ipv4);
    }
    let segments = address.segments();
    !(address.is_unspecified()
        || address.is_loopback()
        || (segments[0] == 0x0064 && segments[1] == 0xff9b)
        || (segments[0] & 0xfe00) == 0xfc00
        || (segments[0] & 0xffc0) == 0xfe80
        || (segments[0] & 0xffc0) == 0xfec0
        || (segments[0] & 0xff00) == 0xff00
        || (segments[0] == 0x2001 && segments[1] == 0)
        || (segments[0] == 0x2001 && segments[1] == 0x0db8)
        || segments[0] == 0x2002
        || (segments[0] == 0x0100 && segments[1..].iter().all(|segment| *segment == 0)))
}

fn looks_like_html(body: &[u8]) -> bool {
    let prefix = String::from_utf8_lossy(&body[..body.len().min(512)]).to_ascii_lowercase();
    let prefix = prefix.trim_start_matches('\u{feff}').trim_start();
    prefix.starts_with("<!doctype html")
        || prefix.starts_with("<html")
        || prefix.starts_with("<!--")
}

fn extract_phone(html: &str) -> Option<String> {
    let tel_pattern = Regex::new(r"(?i)tel:\s*([+0-9０-９\s\-‐‑‒–—−ー()（）]{7,})").ok()?;
    let visible_pattern =
        Regex::new(r"(?:\+81|0)[0-9０-９\s\-‐‑‒–—−ー()（）]{7,}[0-9０-９]").ok()?;
    for matched in tel_pattern.find_iter(html) {
        if let Some(phone) = normalize_phone(matched.as_str()) {
            return Some(phone);
        }
    }
    for matched in visible_pattern.find_iter(html) {
        if let Some(phone) = normalize_phone(matched.as_str()) {
            return Some(phone);
        }
    }
    None
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
        if let Some(ch) = digit {
            normalized.push(ch);
        }
    }
    let digit_count = normalized.chars().filter(|ch| ch.is_ascii_digit()).count();
    if (10..=15).contains(&digit_count) {
        Some(normalized)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_publication_target_is_under_app_data() {
        let app_data_dir = Path::new("app-data");
        assert_eq!(
            default_runtime_target(app_data_dir),
            app_data_dir.join("data").join("queria_runtime.duckdb")
        );
    }

    #[test]
    fn ssrf_guard_rejects_private_reserved_and_transition_addresses() {
        for address in [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "192.168.1.1",
            "100.64.0.1",
            "::1",
            "fd00::1",
            "fe80::1",
            "2001:db8::1",
            "64:ff9b::7f00:1",
        ] {
            let address: IpAddr = address.parse().expect("valid test IP");
            assert!(ensure_public_ip(address).is_err(), "accepted {address}");
        }
        assert!(ensure_public_ip("1.1.1.1".parse().unwrap()).is_ok());
        assert!(ensure_public_ip("2606:4700:4700::1111".parse().unwrap()).is_ok());
    }

    #[test]
    fn ssrf_guard_rejects_internal_hostnames_and_detects_html() {
        for host in [
            "localhost",
            "service.local",
            "metadata.internal",
            "router.lan",
            "hidden.onion",
        ] {
            assert!(is_local_hostname(host), "accepted {host}");
        }
        assert!(!is_local_hostname("example.com"));
        assert!(looks_like_html(b"  <!doctype html><html></html>"));
        assert!(!looks_like_html(b"{\"phone\":\"03-1234-5678\"}"));
    }

    #[tokio::test]
    async fn ssrf_guard_rejects_credentials_ports_and_literal_loopback_before_fetch() {
        for value in [
            "http://127.0.0.1/",
            "https://[::1]/",
            "https://user:pass@example.com/",
            "https://example.com:8443/",
        ] {
            let url = Url::parse(value).expect("valid URL");
            assert!(
                resolve_public_target(&url).await.is_err(),
                "accepted {value}"
            );
        }
    }

    #[test]
    fn phone_normalization_accepts_japanese_full_width_digits() {
        assert_eq!(
            normalize_phone("ＴＥＬ：０３－１２３４－５６７８"),
            Some("0312345678".to_string())
        );
        assert_eq!(normalize_phone("123"), None);
    }
}
