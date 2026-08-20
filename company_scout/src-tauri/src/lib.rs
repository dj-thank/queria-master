mod codex;
mod db;
mod models;
mod duckdb_native;
mod salesforce;

use codex::CodexManager;
use db::Db;
use models::{CodexStatus, Company, DataStatus, ResearchReport, SalesforceStatus, SavedSearch, SearchPlan, SearchResult};
use salesforce::SalesforceManager;
use serde::Serialize;
use std::path::PathBuf;
use tauri::{Manager, State};

struct AppState {
    db: Db,
    codex: CodexManager,
    salesforce: SalesforceManager,
}

#[derive(Serialize)]
struct AuthUrl { auth_url: String }

fn err<E: std::fmt::Display>(e: E) -> String { e.to_string() }

fn find_runtime_db() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::var("QUERIA_RUNTIME_DB") {
        candidates.push(PathBuf::from(path));
    }
    if let Ok(home) = std::env::var("QUERIA_MASTER_HOME") {
        candidates.push(PathBuf::from(home).join("data/queria_runtime.duckdb"));
    }
    if let Ok(executable) = std::env::current_exe() {
        if let Some(parent) = executable.parent() {
            candidates.push(parent.join("data/queria_runtime.duckdb"));
            if let Some(release_root) = parent.parent() {
                candidates.push(release_root.join("data/queria_runtime.duckdb"));
            }
        }
    }
    if let Ok(current_dir) = std::env::current_dir() {
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
async fn codex_status(state: State<'_, AppState>) -> CodexStatus { state.codex.status().await }

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
async fn salesforce_status(state: State<'_, AppState>) -> SalesforceStatus { state.salesforce.status().await }

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
) -> Result<salesforce::SalesforceUpsertResult, String> {
    let db = state.db.clone();
    let companies = tokio::task::spawn_blocking(move || db.list_companies(&list_name)).await.map_err(err)?.map_err(err)?;
    state.salesforce.upsert(&companies, &object_name, &external_id_field).await.map_err(err)
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
            bootstrap, data_status, search_companies, save_search, recent_searches, export_search_csv, add_to_list, add_search_to_list,
            codex_status, codex_login, codex_logout, codex_plan_search, codex_research_company,
            duckdb_native_status, sync_duckdb_company_master, import_company_file, import_industry_taxonomy,
            salesforce_status, salesforce_login_start, salesforce_upsert_list
        ])
        .run(tauri::generate_context!())
        .expect("error while running CompanyMaster");
}
