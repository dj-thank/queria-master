use crate::models::{Company, SalesforceStatus};
use anyhow::{anyhow, Context, Result};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use keyring::Entry;
use rand::{distributions::Alphanumeric, Rng};
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use url::Url;

const SERVICE: &str = "CompanyMaster.Salesforce";
const LEGACY_SERVICE: &str = "CompanyScout.Salesforce";
const KEY_USER: &str = "oauth";
const API_VERSION: &str = "v67.0";
const OAUTH_CALLBACK: &str = "127.0.0.1:53682";

#[derive(Debug, Clone, Serialize)]
pub struct SalesforceLoginStart {
    pub auth_url: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SalesforceUpsertResult {
    pub accepted: u64,
    pub job_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FieldMapping {
    pub source: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SalesforceJobStatus {
    pub job_id: String,
    pub state: String,
    pub number_records_processed: u64,
    pub number_records_failed: u64,
    pub number_records_total: u64,
    pub error_message: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StoredCredentials {
    login_url: String,
    client_id: String,
    access_token: String,
    refresh_token: Option<String>,
    instance_url: String,
    username: Option<String>,
}

#[derive(Clone)]
struct StoredJob {
    companies: Vec<Company>,
    object_name: String,
    external_id_field: String,
    mapping: Vec<FieldMapping>,
}

#[derive(Clone)]
pub struct SalesforceManager {
    client: Client,
    jobs: Arc<Mutex<HashMap<String, StoredJob>>>,
}

impl SalesforceManager {
    pub fn new() -> Result<Self> {
        Ok(Self { client: Client::builder().user_agent("CompanyMaster/0.2").build()?, jobs: Arc::new(Mutex::new(HashMap::new())) })
    }

    pub async fn status(&self) -> SalesforceStatus {
        let creds = load_credentials().ok();
        SalesforceStatus {
            connected: creds.is_some(),
            username: creds.as_ref().and_then(|c| c.username.clone()),
            instance_url: creds.as_ref().map(|c| c.instance_url.clone()),
            api_version: API_VERSION.to_string(),
        }
    }

    pub async fn login_start(&self, login_url: &str, client_id: &str) -> Result<SalesforceLoginStart> {
        let login_url = normalize_login_url(login_url)?;
        let client_id = client_id.trim().to_string();
        if client_id.is_empty() { return Err(anyhow!("Client IDが空です")); }

        let listener = TcpListener::bind(OAUTH_CALLBACK).await
            .context("Salesforce OAuth callback port 53682を開けません")?;
        let redirect_uri = format!("http://{OAUTH_CALLBACK}/callback");
        let verifier: String = rand::thread_rng().sample_iter(&Alphanumeric).take(96).map(char::from).collect();
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        let state: String = rand::thread_rng().sample_iter(&Alphanumeric).take(40).map(char::from).collect();

        let mut url = Url::parse(&format!("{login_url}/services/oauth2/authorize"))?;
        url.query_pairs_mut()
            .append_pair("response_type", "code")
            .append_pair("client_id", &client_id)
            .append_pair("redirect_uri", &redirect_uri)
            .append_pair("code_challenge", &challenge)
            .append_pair("code_challenge_method", "S256")
            .append_pair("state", &state)
            .append_pair("scope", "api refresh_token offline_access");

        let client = self.client.clone();
        let task_login_url = login_url.clone();
        let task_client_id = client_id.clone();
        tokio::spawn(async move {
            if let Err(err) = complete_oauth(listener, client, task_login_url, task_client_id, redirect_uri, verifier, state).await {
                eprintln!("[salesforce oauth] {err:#}");
            }
        });
        Ok(SalesforceLoginStart { auth_url: url.to_string() })
    }

    pub async fn upsert(&self, companies: &[Company], object_name: &str, external_id_field: &str, mapping: &[FieldMapping]) -> Result<SalesforceUpsertResult> {
        if companies.is_empty() { return Err(anyhow!("Salesforceへ送る企業がありません")); }
        let object_name = safe_api_name(object_name, "Object API名")?;
        let external_id_field = safe_api_name(external_id_field, "外部ID項目")?;
        let mapping = normalize_mapping(mapping, &external_id_field)?;
        let mut creds = load_credentials().context("Salesforceに接続してください")?;

        let body = json!({
            "object": object_name,
            "operation": "upsert",
            "externalIdFieldName": external_id_field,
            "contentType": "CSV",
            "lineEnding": "LF"
        });
        let create_url = format!("{}/services/data/{}/jobs/ingest", creds.instance_url, API_VERSION);
        let mut response = self.client.post(&create_url).bearer_auth(&creds.access_token).json(&body).send().await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            creds = self.refresh(creds).await?;
            response = self.client.post(&create_url).bearer_auth(&creds.access_token).json(&body).send().await?;
        }
        let status = response.status();
        let value: Value = response.json().await.unwrap_or(Value::Null);
        if !status.is_success() { return Err(anyhow!("Bulk API job作成失敗 ({status}): {value}")); }
        let job_id = value.get("id").and_then(Value::as_str).ok_or_else(|| anyhow!("Bulk API job idがありません"))?.to_string();

        let csv = build_csv(companies, &mapping)?;
        let batch_url = format!("{}/services/data/{}/jobs/ingest/{}/batches", creds.instance_url, API_VERSION, job_id);
        let upload = self.client.put(&batch_url)
            .bearer_auth(&creds.access_token)
            .header("Content-Type", "text/csv; charset=UTF-8")
            .body(csv)
            .send().await?;
        if !upload.status().is_success() {
            let s = upload.status();
            let t = upload.text().await.unwrap_or_default();
            return Err(anyhow!("Bulk API CSVアップロード失敗 ({s}): {t}"));
        }
        let close_url = format!("{}/services/data/{}/jobs/ingest/{}", creds.instance_url, API_VERSION, job_id);
        let close = self.client.patch(&close_url).bearer_auth(&creds.access_token).json(&json!({"state":"UploadComplete"})).send().await?;
        if !close.status().is_success() {
            let s = close.status();
            let t = close.text().await.unwrap_or_default();
            return Err(anyhow!("Bulk API job確定失敗 ({s}): {t}"));
        }
        self.jobs.lock().await.insert(job_id.clone(), StoredJob {
            companies: companies.to_vec(), object_name, external_id_field, mapping,
        });
        Ok(SalesforceUpsertResult { accepted: companies.len() as u64, job_id })
    }

    pub async fn job_status(&self, job_id: &str) -> Result<SalesforceJobStatus> {
        let mut creds = load_credentials().context("Salesforceに接続してください")?;
        let url = format!("{}/services/data/{}/jobs/ingest/{}", creds.instance_url, API_VERSION, safe_job_id(job_id)?);
        let mut response = self.client.get(&url).bearer_auth(&creds.access_token).send().await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            creds = self.refresh(creds).await?;
            response = self.client.get(&url).bearer_auth(&creds.access_token).send().await?;
        }
        let status = response.status();
        let value: Value = response.json().await.unwrap_or(Value::Null);
        if !status.is_success() { return Err(anyhow!("Bulk APIジョブ状態取得失敗 ({status}): {value}")); }
        Ok(SalesforceJobStatus {
            job_id: job_id.to_string(),
            state: value.get("state").and_then(Value::as_str).unwrap_or("Unknown").to_string(),
            number_records_processed: json_u64(&value, "numberRecordsProcessed"),
            number_records_failed: json_u64(&value, "numberRecordsFailed"),
            number_records_total: json_u64(&value, "numberRecordsTotal"),
            error_message: value.get("errorMessage").and_then(Value::as_str).map(str::to_string),
        })
    }

    pub async fn retry_failed(&self, job_id: &str) -> Result<SalesforceUpsertResult> {
        let job = self.jobs.lock().await.get(job_id).cloned().ok_or_else(|| anyhow!("このアプリ起動後のジョブ情報がありません。元リストから再送してください"))?;
        let mut creds = load_credentials().context("Salesforceに接続してください")?;
        let url = format!("{}/services/data/{}/jobs/ingest/{}/failedResults", creds.instance_url, API_VERSION, safe_job_id(job_id)?);
        let mut response = self.client.get(&url).bearer_auth(&creds.access_token).send().await?;
        if response.status() == StatusCode::UNAUTHORIZED {
            creds = self.refresh(creds).await?;
            response = self.client.get(&url).bearer_auth(&creds.access_token).send().await?;
        }
        let status = response.status();
        let body = response.bytes().await?;
        if !status.is_success() { return Err(anyhow!("失敗行取得失敗 ({status}): {}", String::from_utf8_lossy(&body))); }
        let mut reader = csv::ReaderBuilder::new().flexible(true).from_reader(body.as_ref());
        let headers = reader.headers()?.clone();
        let key_index = headers.iter().position(|h| h == job.external_id_field)
            .ok_or_else(|| anyhow!("失敗結果に外部ID列 {} がありません", job.external_id_field))?;
        let mut failed_keys = HashSet::new();
        for row in reader.records() {
            let row = row?;
            if let Some(value) = row.get(key_index) { failed_keys.insert(value.to_string()); }
        }
        if failed_keys.is_empty() { return Err(anyhow!("Salesforceが返した失敗行は0件です")); }
        let failed_companies: Vec<Company> = job.companies.into_iter()
            .filter(|company| failed_keys.contains(&company.corporate_number))
            .collect();
        if failed_companies.is_empty() { return Err(anyhow!("失敗行の法人番号を元リストから特定できません")); }
        self.upsert(&failed_companies, &job.object_name, &job.external_id_field, &job.mapping).await
    }

    async fn refresh(&self, mut creds: StoredCredentials) -> Result<StoredCredentials> {
        let refresh = creds.refresh_token.clone().ok_or_else(|| anyhow!("Salesforce refresh tokenがありません。再ログインしてください。"))?;
        let url = format!("{}/services/oauth2/token", creds.login_url);
        let response = self.client.post(url).form(&[
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh.as_str()),
            ("client_id", creds.client_id.as_str()),
        ]).send().await?;
        let status = response.status();
        let value: Value = response.json().await.unwrap_or(Value::Null);
        if !status.is_success() { return Err(anyhow!("Salesforce token refresh失敗 ({status}): {value}")); }
        creds.access_token = value.get("access_token").and_then(Value::as_str).ok_or_else(|| anyhow!("access_tokenがありません"))?.to_string();
        if let Some(instance) = value.get("instance_url").and_then(Value::as_str) { creds.instance_url = instance.to_string(); }
        store_credentials(&creds)?;
        Ok(creds)
    }
}

async fn complete_oauth(
    listener: TcpListener,
    client: Client,
    login_url: String,
    client_id: String,
    redirect_uri: String,
    verifier: String,
    expected_state: String,
) -> Result<()> {
    let (mut socket, _) = listener.accept().await?;
    let mut buf = vec![0u8; 16384];
    let n = socket.read(&mut buf).await?;
    let request = String::from_utf8_lossy(&buf[..n]);
    let first = request.lines().next().ok_or_else(|| anyhow!("OAuth callbackが不正です"))?;
    let target = first.split_whitespace().nth(1).ok_or_else(|| anyhow!("OAuth callback pathがありません"))?;
    let callback = Url::parse(&format!("http://127.0.0.1{target}"))?;
    let params: std::collections::HashMap<_, _> = callback.query_pairs().into_owned().collect();
    let state = params.get("state").ok_or_else(|| anyhow!("OAuth stateがありません"))?;
    if state != &expected_state { return Err(anyhow!("OAuth state mismatch")); }
    if let Some(error) = params.get("error") {
        let desc = params.get("error_description").cloned().unwrap_or_default();
        respond_browser(&mut socket, false).await?;
        return Err(anyhow!("Salesforce OAuth: {error} {desc}"));
    }
    let code = params.get("code").ok_or_else(|| anyhow!("OAuth codeがありません"))?.clone();

    let token_url = format!("{login_url}/services/oauth2/token");
    let response = client.post(token_url).form(&[
        ("grant_type", "authorization_code"),
        ("code", code.as_str()),
        ("client_id", client_id.as_str()),
        ("redirect_uri", redirect_uri.as_str()),
        ("code_verifier", verifier.as_str()),
    ]).send().await?;
    let status = response.status();
    let value: Value = response.json().await.unwrap_or(Value::Null);
    if !status.is_success() {
        respond_browser(&mut socket, false).await?;
        return Err(anyhow!("Salesforce token exchange失敗 ({status}): {value}"));
    }
    let access_token = value.get("access_token").and_then(Value::as_str).ok_or_else(|| anyhow!("access_tokenがありません"))?.to_string();
    let instance_url = value.get("instance_url").and_then(Value::as_str).ok_or_else(|| anyhow!("instance_urlがありません"))?.to_string();
    let refresh_token = value.get("refresh_token").and_then(Value::as_str).map(str::to_string);
    let username = fetch_username(&client, &instance_url, &access_token).await.ok();
    let creds = StoredCredentials { login_url, client_id, access_token, refresh_token, instance_url, username };
    store_credentials(&creds)?;
    respond_browser(&mut socket, true).await?;
    Ok(())
}

async fn respond_browser(socket: &mut tokio::net::TcpStream, success: bool) -> Result<()> {
    let body = if success {
        "<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'><h2>Salesforce接続が完了しました</h2><p>このタブを閉じてCompanyMasterへ戻ってください。</p></body></html>"
    } else {
        "<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'><h2>Salesforce接続に失敗しました</h2><p>CompanyMasterへ戻って再度お試しください。</p></body></html>"
    };
    let response = format!("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.as_bytes().len(), body);
    socket.write_all(response.as_bytes()).await?;
    socket.shutdown().await?;
    Ok(())
}

async fn fetch_username(client: &Client, instance_url: &str, token: &str) -> Result<String> {
    let value: Value = client.get(format!("{instance_url}/services/oauth2/userinfo")).bearer_auth(token).send().await?.json().await?;
    Ok(value.get("preferred_username").or_else(|| value.get("email")).and_then(Value::as_str).unwrap_or("Salesforce user").to_string())
}

fn build_csv(companies: &[Company], mapping: &[FieldMapping]) -> Result<Vec<u8>> {
    let mut writer = csv::WriterBuilder::new().terminator(csv::Terminator::Any(b'\n')).from_writer(Vec::new());
    let headers: Vec<&str> = mapping.iter().map(|field| field.target.as_str()).collect();
    writer.write_record(&headers)?;
    for company in companies {
        let row: Vec<String> = mapping.iter().map(|field| value_for_source(company, &field.source)).collect();
        writer.write_record(&row)?;
    }
    writer.into_inner().map_err(|e| anyhow!(e.to_string()))
}

fn normalize_mapping(mapping: &[FieldMapping], external_id_field: &str) -> Result<Vec<FieldMapping>> {
    let mut out = if mapping.is_empty() {
        default_mapping(external_id_field)
    } else {
        mapping.to_vec()
    };
    let allowed = ["name", "corporate_number", "website", "phone", "prefecture", "city", "address", "industry", "industry_code", "employees", "capital", "established_year", "representative", "business_summary"];
    for field in &mut out {
        field.source = field.source.trim().to_string();
        field.target = safe_api_name(&field.target, "Salesforce項目")?;
        if !allowed.contains(&field.source.as_str()) { return Err(anyhow!("未対応のCompanyMaster項目です: {}", field.source)); }
    }
    if !out.iter().any(|field| field.source == "corporate_number" && field.target == external_id_field) {
        out.insert(0, FieldMapping { source: "corporate_number".to_string(), target: external_id_field.to_string() });
    }
    let mut targets = HashSet::new();
    for field in &out {
        if !targets.insert(field.target.clone()) { return Err(anyhow!("Salesforce項目が重複しています: {}", field.target)); }
    }
    Ok(out)
}

fn default_mapping(external_id_field: &str) -> Vec<FieldMapping> {
    vec![
        FieldMapping { source: "name".into(), target: "Name".into() },
        FieldMapping { source: "corporate_number".into(), target: external_id_field.into() },
        FieldMapping { source: "website".into(), target: "Website".into() },
        FieldMapping { source: "phone".into(), target: "Phone".into() },
        FieldMapping { source: "prefecture".into(), target: "BillingState".into() },
        FieldMapping { source: "city".into(), target: "BillingCity".into() },
        FieldMapping { source: "address".into(), target: "BillingStreet".into() },
        FieldMapping { source: "industry".into(), target: "Industry".into() },
        FieldMapping { source: "employees".into(), target: "NumberOfEmployees".into() },
        FieldMapping { source: "business_summary".into(), target: "Description".into() },
    ]
}

fn value_for_source(company: &Company, source: &str) -> String {
    match source {
        "name" => company.name.clone(),
        "corporate_number" => company.corporate_number.clone(),
        "website" => company.website.clone().unwrap_or_default(),
        "phone" => company.phone.clone().unwrap_or_default(),
        "prefecture" => company.prefecture.clone().unwrap_or_default(),
        "city" => company.city.clone().unwrap_or_default(),
        "address" => company.address.clone().unwrap_or_default(),
        "industry" => company.industry_name.clone().or_else(|| company.inferred_industry_name.clone()).unwrap_or_default(),
        "industry_code" => company.industry_code.clone().or_else(|| company.inferred_industry_code.clone()).unwrap_or_default(),
        "employees" => company.employees.map(|value| value.to_string()).unwrap_or_default(),
        "capital" => company.capital.map(|value| value.to_string()).unwrap_or_default(),
        "established_year" => company.established_year.map(|value| value.to_string()).unwrap_or_default(),
        "representative" => company.representative.clone().unwrap_or_default(),
        "business_summary" => company.business_summary.clone().unwrap_or_default(),
        _ => String::new(),
    }
}

fn json_u64(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).or_else(|| value.get(key).and_then(Value::as_i64).map(|v| v.max(0) as u64)).unwrap_or(0)
}

fn safe_job_id(value: &str) -> Result<String> {
    let value = value.trim();
    if value.is_empty() || !value.chars().all(|ch| ch.is_ascii_alphanumeric() || ch == '-') {
        return Err(anyhow!("Bulk API job idが不正です"));
    }
    Ok(value.to_string())
}

fn entry() -> Result<Entry> {
    Entry::new(SERVICE, KEY_USER).map_err(|e| anyhow!("Windows資格情報ストア: {e}"))
}

fn store_credentials(creds: &StoredCredentials) -> Result<()> {
    entry()?.set_password(&serde_json::to_string(creds)?).map_err(|e| anyhow!("Salesforce認証情報を保存できません: {e}"))
}

fn load_credentials() -> Result<StoredCredentials> {
    let raw = match entry()?.get_password() {
        Ok(raw) => raw,
        Err(_) => Entry::new(LEGACY_SERVICE, KEY_USER)
            .map_err(|e| anyhow!("Windows資格情報ストア: {e}"))?
            .get_password()
            .map_err(|e| anyhow!("Salesforce認証情報がありません: {e}"))?,
    };
    serde_json::from_str(&raw).context("Salesforce認証情報が壊れています")
}

fn normalize_login_url(value: &str) -> Result<String> {
    let url = Url::parse(value.trim())?;
    if url.scheme() != "https" { return Err(anyhow!("Salesforce Login URLはhttpsのみ許可します")); }
    let host = url.host_str().ok_or_else(|| anyhow!("Salesforce Login URLが不正です"))?;
    if !(host == "login.salesforce.com" || host == "test.salesforce.com" || host.ends_with(".my.salesforce.com")) {
        return Err(anyhow!("Salesforce login/test/My Domain URLを指定してください"));
    }
    Ok(format!("{}://{}{}", url.scheme(), host, if let Some(port) = url.port() { format!(":{port}") } else { String::new() }))
}

fn safe_api_name(value: &str, label: &str) -> Result<String> {
    let v = value.trim();
    if v.is_empty() || !v.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return Err(anyhow!("{label}が不正です"));
    }
    Ok(v.to_string())
}
