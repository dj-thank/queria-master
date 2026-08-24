use crate::models::{
    CodexStatus, Company, IndustryGuess, ResearchReport, ResearchSource, SearchPlan,
};
use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{broadcast, oneshot, Mutex};
use tokio::time::{timeout, Duration};

const LUNA_MODEL: &str = "gpt-5.6-luna";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(40);
const TURN_TIMEOUT: Duration = Duration::from_secs(300);

#[derive(Clone)]
pub struct CodexManager {
    app_data_dir: PathBuf,
    workspace_dir: PathBuf,
    session: Arc<Mutex<Option<Arc<CodexSession>>>>,
}

struct CodexSession {
    _child: Arc<Mutex<Child>>,
    stdin: Arc<Mutex<ChildStdin>>,
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
    events: broadcast::Sender<Value>,
    next_id: AtomicU64,
    version: Option<String>,
}

impl CodexManager {
    pub fn new(app_data_dir: PathBuf, workspace_dir: PathBuf) -> Self {
        Self {
            app_data_dir,
            workspace_dir,
            session: Arc::new(Mutex::new(None)),
        }
    }

    async fn ensure(&self) -> Result<Arc<CodexSession>> {
        let mut guard = self.session.lock().await;
        if let Some(session) = guard.as_ref() {
            return Ok(session.clone());
        }
        let session = Arc::new(self.spawn().await?);
        session
            .request(
                "initialize",
                Some(json!({
                    "clientInfo": {
                        "name": "company_master",
                        "title": "CompanyMaster",
                        "version": env!("CARGO_PKG_VERSION")
                    },
                    "capabilities": {
                        "optOutNotificationMethods": ["item/agentMessage/delta"]
                    }
                })),
            )
            .await?;
        session.notify("initialized", json!({})).await?;
        *guard = Some(session.clone());
        Ok(session)
    }

    async fn spawn(&self) -> Result<CodexSession> {
        let codex_home = self.app_data_dir.join("codex-home");
        std::fs::create_dir_all(&codex_home)?;
        write_codex_config(&codex_home)?;
        std::fs::create_dir_all(&self.workspace_dir)?;

        let binary = find_codex_binary(&self.app_data_dir)?;
        let version = Command::new(&binary)
            .arg("--version")
            .env("CODEX_HOME", &codex_home)
            .output()
            .await
            .ok()
            .and_then(|o| {
                if o.status.success() {
                    Some(String::from_utf8_lossy(&o.stdout).trim().to_string())
                } else {
                    None
                }
            });

        let mut child = Command::new(&binary)
            .arg("app-server")
            .env("CODEX_HOME", &codex_home)
            .current_dir(&self.workspace_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .with_context(|| format!("Codex App Serverを起動できません: {}", binary.display()))?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("Codex stdinを取得できません"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("Codex stdoutを取得できません"))?;
        let stderr = child.stderr.take();
        let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let pending_reader = pending.clone();
        let (events, _) = broadcast::channel(512);
        let events_reader = events.clone();

        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if let Some(id) = value.get("id").and_then(|v| v.as_u64()) {
                    if let Some(tx) = pending_reader.lock().await.remove(&id) {
                        let _ = tx.send(value);
                        continue;
                    }
                }
                let _ = events_reader.send(value);
            }
        });

        if let Some(stderr) = stderr {
            tokio::spawn(async move {
                let mut lines = BufReader::new(stderr).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    eprintln!("[codex] {line}");
                }
            });
        }

        Ok(CodexSession {
            _child: Arc::new(Mutex::new(child)),
            stdin: Arc::new(Mutex::new(stdin)),
            pending,
            events,
            next_id: AtomicU64::new(1),
            version,
        })
    }

    pub async fn status(&self) -> CodexStatus {
        let session = match self.ensure().await {
            Ok(v) => v,
            Err(_) => {
                return CodexStatus {
                    running: false,
                    authenticated: false,
                    email: None,
                    plan_type: None,
                    luna_available: false,
                    model: LUNA_MODEL.into(),
                    version: None,
                }
            }
        };
        let account = session
            .request("account/read", Some(json!({"refreshToken": false})))
            .await
            .ok();
        let models = session
            .request(
                "model/list",
                Some(json!({"limit": 100, "includeHidden": true})),
            )
            .await
            .ok();
        let account_obj = account.as_ref().and_then(|v| v.get("account"));
        let account_type = account_obj
            .and_then(|v| v.get("type"))
            .and_then(|v| v.as_str());
        let authenticated = account_type == Some("chatgpt");
        let email = account_obj
            .and_then(|v| v.get("email"))
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let plan_type = account_obj
            .and_then(|v| v.get("planType"))
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let luna_available = models
            .as_ref()
            .and_then(|v| v.get("data"))
            .and_then(|v| v.as_array())
            .map(|items| {
                items.iter().any(|m| {
                    m.get("id").and_then(|v| v.as_str()) == Some(LUNA_MODEL)
                        || m.get("model").and_then(|v| v.as_str()) == Some(LUNA_MODEL)
                })
            })
            .unwrap_or(false);
        CodexStatus {
            running: true,
            authenticated,
            email,
            plan_type,
            luna_available,
            model: LUNA_MODEL.into(),
            version: session.version.clone(),
        }
    }

    pub async fn login(&self) -> Result<String> {
        let session = self.ensure().await?;
        let value = session
            .request(
                "account/login/start",
                Some(json!({
                    "type": "chatgpt",
                    "useHostedLoginSuccessPage": true,
                    "appBrand": "chatgpt"
                })),
            )
            .await?;
        value
            .get("authUrl")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .ok_or_else(|| anyhow!("CodexからログインURLが返りませんでした"))
    }

    pub async fn logout(&self) -> Result<()> {
        let session = self.ensure().await?;
        session.request("account/logout", None).await?;
        Ok(())
    }

    pub async fn plan_search(&self, query: &str) -> Result<SearchPlan> {
        self.require_luna().await?;
        let schema = search_plan_schema();
        let prompt = format!(
            r#"
あなたは日本企業データベースの検索プランナーです。ユーザーの要望をCompanyMasterのSearchPlanへ変換してください。

制約:
- 出力は指定JSON Schemaだけ。
- 業種は日本標準産業分類(JSIC)の大分類→中分類→小分類→細分類を意識する。
- 4桁などの業種コードに自信がない場合は、コードを捏造せずindustry_termsへ日本語キーワードを入れる。
- 「ITっぽい」「こういう会社」のような曖昧条件はindustry_terms/keyword_anyへ分解する。
- company_kindsは日本語名ではなく法人種別コードを使う（株式会社=301、有限会社=302、合名会社=303、合資会社=304、合同会社=305、その他の設立登記法人=399）。
- website_requiredとphone_requiredは明示された場合のみ設定する。
- 件数指定がなければlimit=30000。最大10000000（現在の全量スナップショットを上限で切らない）。
- textは会社名・住所・事業概要などに実際に含まれる自由検索語がある場合だけ設定する。元の要望文全体を入れない。
- sort_byはrelevance/name/employees/capital、sort_directionはasc/descだけを使う。
- 値が不明な条件は無理に補わない。

ユーザー要望:
{query}
"#
        );
        let (_, text) = self.run_turn(&prompt, Some(schema), "low").await?;
        let plan: SearchPlan = parse_json_text(&text)?;
        Ok(plan.normalize())
    }

    pub async fn research_company(
        &self,
        company: &Company,
        instruction: &str,
    ) -> Result<ResearchReport> {
        self.require_luna().await?;
        let company_json = serde_json::to_string_pretty(company)?;
        let prompt = format!(
            r#"
あなたは企業リサーチ担当です。以下の会社について公開Webを調査してください。Web検索を使い、企業の公式サイト・公式発表・官公庁/公的データを優先してください。

重要:
- 同名企業を取り違えない。法人番号・所在地・公式サイトを照合する。
- 事業内容、主要サービス/製品、顧客像、営業上の特徴を簡潔にまとめる。
- 日本標準産業分類の細分類まで推定できる場合は推定する。公式データとAI推定は混同しない。
- sourcesには実際に確認したURLだけを入れる。
- transcriptは「検索・確認した公開情報の作業ログ/根拠要約」。内部思考や逐語的な推論過程は書かない。
- 根拠が足りないことは「不明」とする。推測を事実として書かない。

追加指示:
{instruction}

会社データ:
{company_json}
"#
        );
        let (thread_id, text) = self
            .run_turn(&prompt, Some(research_schema()), "medium")
            .await?;
        let parsed: Value = parse_json_text(&text)?;
        let sources = parsed
            .get("sources")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| {
                        Some(ResearchSource {
                            url: v.get("url")?.as_str()?.to_string(),
                            title: v.get("title").and_then(Value::as_str).map(str::to_string),
                            note: v.get("note").and_then(Value::as_str).map(str::to_string),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        let findings = parsed
            .get("findings")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let industry_guess = parsed
            .get("industry_guess")
            .and_then(|v| if v.is_null() { None } else { Some(v) })
            .map(|v| IndustryGuess {
                code: v.get("code").and_then(Value::as_str).map(str::to_string),
                name: v.get("name").and_then(Value::as_str).map(str::to_string),
                confidence: v.get("confidence").and_then(Value::as_f64),
                rationale: v
                    .get("rationale")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            });
        Ok(ResearchReport {
            corporate_number: company.corporate_number.clone(),
            company_name: company.name.clone(),
            thread_id,
            summary: parsed
                .get("summary")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            transcript: parsed
                .get("transcript")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            industry_guess,
            findings,
            sources,
            created_at: chrono::Utc::now().to_rfc3339(),
        })
    }

    async fn require_luna(&self) -> Result<()> {
        let status = self.status().await;
        if !status.running {
            return Err(anyhow!(
                "Codex App Serverを起動できません。Codexランタイムを確認してください。"
            ));
        }
        if !status.authenticated {
            return Err(anyhow!(
                "ChatGPTでログインしてください。APIキー認証はこのアプリでは許可していません。"
            ));
        }
        if !status.luna_available {
            return Err(anyhow!("このChatGPTアカウントではGPT-5.6 Lunaを利用できません。CompanyMasterは他モデルへフォールバックしません。"));
        }
        Ok(())
    }

    async fn run_turn(
        &self,
        prompt: &str,
        output_schema: Option<Value>,
        effort: &str,
    ) -> Result<(String, String)> {
        let session = self.ensure().await?;
        let mut events = session.events.subscribe();
        let thread = session
            .request(
                "thread/start",
                Some(json!({
                    "model": LUNA_MODEL,
                    "cwd": self.workspace_dir.to_string_lossy(),
                    "approvalPolicy": "never",
                    // The bundled Codex App Server accepts the hyphenated sandbox
                    // variant on thread/start.  The camelCase value produces the
                    // user-visible `unknown variant readOnly` error on Windows.
                    "sandbox": "read-only",
                    "serviceName": "company_master"
                })),
            )
            .await?;
        let thread_id = thread
            .get("thread")
            .and_then(|v| v.get("id"))
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Codex thread/startの応答にthread.idがありません"))?
            .to_string();
        let mut params = json!({
            "threadId": thread_id,
            "input": [{"type":"text","text":prompt}],
            "model": LUNA_MODEL,
            "effort": effort,
            "summary": "concise",
            "sandboxPolicy": {"type":"readOnly","access":{"type":"fullAccess"}}
        });
        if let Some(schema) = output_schema {
            params["outputSchema"] = schema;
        }
        let turn = session.request("turn/start", Some(params)).await?;
        let turn_id = turn
            .get("turn")
            .and_then(|v| v.get("id"))
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Codex turn/startの応答にturn.idがありません"))?
            .to_string();

        let result = timeout(TURN_TIMEOUT, async {
            let mut final_text = String::new();
            loop {
                let event = events.recv().await.map_err(|e| anyhow!("Codexイベント受信エラー: {e}"))?;
                let method = event.get("method").and_then(Value::as_str).unwrap_or("");
                let params = event.get("params").unwrap_or(&Value::Null);
                if let Some(tid) = params.get("turnId").and_then(Value::as_str) {
                    if tid != turn_id { continue; }
                }
                if method == "model/rerouted" {
                    let to = params.get("toModel").and_then(Value::as_str).unwrap_or("");
                    if !to.is_empty() && to != LUNA_MODEL {
                        return Err(anyhow!("GPT-5.6 Lunaから別モデルへルーティングされたため処理を中止しました: {to}"));
                    }
                }
                if method == "item/completed" {
                    if let Some(item) = params.get("item") {
                        if item.get("type").and_then(Value::as_str) == Some("agentMessage") {
                            if item.get("phase").and_then(Value::as_str).map(|p| p == "final_answer").unwrap_or(true) {
                                if let Some(text) = item.get("text").and_then(Value::as_str) { final_text = text.to_string(); }
                            }
                        }
                    }
                }
                if method == "turn/completed" {
                    let turn = params.get("turn").unwrap_or(&Value::Null);
                    if turn.get("id").and_then(Value::as_str) != Some(turn_id.as_str()) { continue; }
                    match turn.get("status").and_then(Value::as_str).unwrap_or("") {
                        "completed" => {
                            if final_text.trim().is_empty() { return Err(anyhow!("Codexの最終応答が空でした")); }
                            return Ok(final_text);
                        }
                        "failed" => {
                            let msg = turn.get("error").and_then(|v| v.get("message")).and_then(Value::as_str).unwrap_or("Codex turn failed");
                            return Err(anyhow!(msg.to_string()));
                        }
                        "interrupted" => return Err(anyhow!("Codex処理が中断されました")),
                        _ => {}
                    }
                }
            }
        }).await.map_err(|_| anyhow!("Codex処理がタイムアウトしました"))??;
        Ok((thread_id, result))
    }
}

impl CodexSession {
    async fn request(&self, method: &str, params: Option<Value>) -> Result<Value> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let mut msg = json!({"method": method, "id": id});
        if let Some(params) = params {
            msg["params"] = params;
        }
        if let Err(err) = self.write(&msg).await {
            self.pending.lock().await.remove(&id);
            return Err(err);
        }
        let response = timeout(REQUEST_TIMEOUT, rx)
            .await
            .map_err(|_| anyhow!("Codex request timeout: {method}"))?
            .map_err(|_| anyhow!("Codex response channel closed: {method}"))?;
        if let Some(error) = response.get("error") {
            return Err(anyhow!("Codex {method}: {}", error));
        }
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    }

    async fn notify(&self, method: &str, params: Value) -> Result<()> {
        self.write(&json!({"method": method, "params": params}))
            .await
    }

    async fn write(&self, msg: &Value) -> Result<()> {
        let mut stdin = self.stdin.lock().await;
        let line = serde_json::to_vec(msg)?;
        stdin.write_all(&line).await?;
        stdin.write_all(b"\n").await?;
        stdin.flush().await?;
        Ok(())
    }
}

fn write_codex_config(codex_home: &Path) -> Result<()> {
    let config = r#"# CompanyMaster private Codex profile
# Authentication is intentionally restricted to each user's ChatGPT login.
forced_login_method = "chatgpt"
cli_auth_credentials_store = "keyring"
web_search = "live"
file_opener = "none"
"#;
    std::fs::write(codex_home.join("config.toml"), config)?;
    Ok(())
}

fn find_codex_binary(app_data_dir: &Path) -> Result<PathBuf> {
    for variable in ["COMPANYMASTER_CODEX_PATH", "COMPANYSCOUT_CODEX_PATH"] {
        if let Ok(value) = std::env::var(variable) {
            let p = PathBuf::from(value);
            if p.is_file() {
                return Ok(p);
            }
        }
    }
    for candidate in [
        app_data_dir.join("bin").join("codex.exe"),
        app_data_dir.join("bin").join("codex"),
    ] {
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    which::which("codex.exe").or_else(|_| which::which("codex"))
        .map_err(|_| anyhow!("Codexランタイムが見つかりません。セットアップスクリプトでcodex.exeを配置してください。"))
}

fn parse_json_text<T: serde::de::DeserializeOwned>(text: &str) -> Result<T> {
    let trimmed = text.trim();
    if let Ok(v) = serde_json::from_str(trimmed) {
        return Ok(v);
    }
    let cleaned = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```"))
        .and_then(|v| v.strip_suffix("```"))
        .map(str::trim)
        .unwrap_or(trimmed);
    serde_json::from_str(cleaned)
        .with_context(|| format!("LLMのJSONを解析できません: {}", truncate(cleaned, 400)))
}

fn truncate(value: &str, max: usize) -> String {
    if value.chars().count() <= max {
        value.to_string()
    } else {
        value.chars().take(max).collect::<String>() + "…"
    }
}

fn search_plan_schema() -> Value {
    json!({
      "type":"object",
      "properties":{
        "text":{"type":["string","null"]},
        "prefectures":{"type":"array","items":{"type":"string"}},
        "cities":{"type":"array","items":{"type":"string"}},
        "industry_codes":{"type":"array","items":{"type":"string"}},
        "industry_terms":{"type":"array","items":{"type":"string"}},
        "company_kinds":{"type":"array","items":{"type":"string"}},
        "min_employees":{"type":["integer","null"]},
        "max_employees":{"type":["integer","null"]},
        "min_capital":{"type":["integer","null"]},
        "max_capital":{"type":["integer","null"]},
        "established_from":{"type":["integer","null"]},
        "established_to":{"type":["integer","null"]},
        "website_required":{"type":["boolean","null"]},
        "phone_required":{"type":["boolean","null"]},
        "keyword_any":{"type":"array","items":{"type":"string"}},
        "keyword_all":{"type":"array","items":{"type":"string"}},
        "sort_by":{"type":["string","null"],"enum":["relevance","name","employees","capital",null]},
        "sort_direction":{"type":["string","null"],"enum":["asc","desc",null]},
        "limit":{"type":"integer","minimum":1,"maximum":10000000}
      },
      "required":["text","prefectures","cities","industry_codes","industry_terms","company_kinds","min_employees","max_employees","min_capital","max_capital","established_from","established_to","website_required","phone_required","keyword_any","keyword_all","sort_by","sort_direction","limit"],
      "additionalProperties":false
    })
}

fn research_schema() -> Value {
    json!({
      "type":"object",
      "properties":{
        "summary":{"type":"string"},
        "findings":{"type":"array","items":{"type":"string"}},
        "industry_guess":{
          "type":["object","null"],
          "properties":{
            "code":{"type":["string","null"]},
            "name":{"type":["string","null"]},
            "confidence":{"type":["number","null"],"minimum":0,"maximum":1},
            "rationale":{"type":["string","null"]}
          },
          "required":["code","name","confidence","rationale"],
          "additionalProperties":false
        },
        "sources":{"type":"array","items":{
          "type":"object","properties":{
            "url":{"type":"string"},"title":{"type":["string","null"]},"note":{"type":["string","null"]}
          },"required":["url","title","note"],"additionalProperties":false
        }},
        "transcript":{"type":"string"}
      },
      "required":["summary","findings","industry_guess","sources","transcript"],
      "additionalProperties":false
    })
}
