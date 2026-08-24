use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::sync::Mutex;
use tokio::time::timeout;

const PUBLIC_SCRIPT: &str = "public_data_enricher.py";
const MAX_OUTPUT_BYTES: usize = 8 * 1024 * 1024;

struct BoundedOutput {
    status: std::process::ExitStatus,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

#[derive(Clone)]
pub struct PublicEnrichmentManager {
    inner: Arc<Inner>,
}

struct Inner {
    script_dir: PathBuf,
    workspace_dir: PathBuf,
    db_path: PathBuf,
    input_dir: PathBuf,
    output_dir: PathBuf,
    python: Option<PythonRuntime>,
    publisher: Option<PublisherRuntime>,
    canonical_db: Option<PathBuf>,
    enrichment_db: Option<PathBuf>,
    runtime_db: Option<PathBuf>,
    search_index: Option<PathBuf>,
    gate: Mutex<()>,
}

#[derive(Clone)]
struct PythonRuntime {
    program: PathBuf,
    prefix_args: Vec<String>,
    version: String,
}

#[derive(Clone)]
struct PublisherRuntime {
    program: PathBuf,
    prefix_args: Vec<String>,
    working_dir: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
pub struct PublicEnrichmentStatus {
    pub available: bool,
    pub publish_available: bool,
    pub python_version: Option<String>,
    pub script_path: String,
    pub workspace_dir: String,
    pub db_path: String,
    pub input_dir: String,
    pub output_dir: String,
    pub canonical_db: Option<String>,
    pub enrichment_db: Option<String>,
    pub runtime_db: Option<String>,
    pub search_index: Option<String>,
    pub companies: u64,
    pub accepted_matches: u64,
    pub review_matches: u64,
    pub public_master: u64,
    pub financial_history: u64,
    pub workplace_info: u64,
    pub edinet_metrics: u64,
    pub site_contacts: u64,
    pub source_audit: u64,
    pub integrity: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PublicEnrichmentOperation {
    pub action: String,
    pub result: Value,
    pub status: PublicEnrichmentStatus,
}

#[derive(Debug, Default, Deserialize)]
struct CliStatus {
    companies: Option<u64>,
    accepted_matches: Option<u64>,
    review_matches: Option<u64>,
    public_master: Option<u64>,
    financial_history: Option<u64>,
    workplace_info: Option<u64>,
    edinet_metrics: Option<u64>,
    site_contacts: Option<u64>,
    source_audit: Option<u64>,
    integrity: Option<String>,
}

impl PublicEnrichmentManager {
    pub fn new(
        app_data_dir: PathBuf,
        resource_dir: PathBuf,
        canonical_db: Option<PathBuf>,
        enrichment_db: Option<PathBuf>,
        runtime_db: Option<PathBuf>,
        search_index: Option<PathBuf>,
    ) -> Result<Self> {
        let workspace_dir = app_data_dir.join("public-enrichment");
        let input_dir = workspace_dir.join("input");
        let output_dir = workspace_dir.join("output").join("csv");
        let db_path = workspace_dir
            .join("output")
            .join("company_public_data.sqlite3");
        std::fs::create_dir_all(&input_dir).context("公開データ入力フォルダを作成できません")?;
        std::fs::create_dir_all(&output_dir).context("公開データ出力フォルダを作成できません")?;

        let script_dir = discover_script_dir(&resource_dir);
        let python = discover_python();
        let publisher = discover_publisher(&script_dir, python.as_ref());
        Ok(Self {
            inner: Arc::new(Inner {
                script_dir,
                workspace_dir,
                db_path,
                input_dir,
                output_dir,
                python,
                publisher,
                canonical_db,
                enrichment_db,
                runtime_db,
                search_index,
                gate: Mutex::new(()),
            }),
        })
    }

    pub async fn status(&self) -> PublicEnrichmentStatus {
        let script_path = self.inner.script_dir.join(PUBLIC_SCRIPT);
        let available = script_path.is_file() && self.inner.python.is_some();
        let publish_available = self.inner.publisher.is_some()
            && self
                .inner
                .canonical_db
                .as_ref()
                .is_some_and(|path| path.is_file())
            && self.inner.enrichment_db.is_some()
            && self.inner.runtime_db.is_some()
            && self.inner.search_index.is_some();
        let mut status = PublicEnrichmentStatus {
            available,
            publish_available,
            python_version: self
                .inner
                .python
                .as_ref()
                .map(|runtime| runtime.version.clone()),
            script_path: path_text(&script_path),
            workspace_dir: path_text(&self.inner.workspace_dir),
            db_path: path_text(&self.inner.db_path),
            input_dir: path_text(&self.inner.input_dir),
            output_dir: path_text(&self.inner.output_dir),
            canonical_db: self.inner.canonical_db.as_ref().map(|path| path_text(path)),
            enrichment_db: self
                .inner
                .enrichment_db
                .as_ref()
                .map(|path| path_text(path)),
            runtime_db: self.inner.runtime_db.as_ref().map(|path| path_text(path)),
            search_index: self.inner.search_index.as_ref().map(|path| path_text(path)),
            companies: 0,
            accepted_matches: 0,
            review_matches: 0,
            public_master: 0,
            financial_history: 0,
            workplace_info: 0,
            edinet_metrics: 0,
            site_contacts: 0,
            source_audit: 0,
            integrity: if self.inner.db_path.is_file() {
                "unknown".to_string()
            } else {
                "not_initialized".to_string()
            },
            error: None,
        };

        if !script_path.is_file() {
            status.error = Some("公開データ統合スクリプトが見つかりません".to_string());
            return status;
        }
        if self.inner.python.is_none() {
            status.error = Some("Python 3.11以上が見つかりません".to_string());
            return status;
        }
        if !self.inner.db_path.is_file() {
            return status;
        }

        match self
            .run_public_cli(vec![OsString::from("status")], Duration::from_secs(45))
            .await
        {
            Ok(value) => match serde_json::from_value::<CliStatus>(value) {
                Ok(payload) => {
                    status.companies = payload.companies.unwrap_or(0);
                    status.accepted_matches = payload.accepted_matches.unwrap_or(0);
                    status.review_matches = payload.review_matches.unwrap_or(0);
                    status.public_master = payload.public_master.unwrap_or(0);
                    status.financial_history = payload.financial_history.unwrap_or(0);
                    status.workplace_info = payload.workplace_info.unwrap_or(0);
                    status.edinet_metrics = payload.edinet_metrics.unwrap_or(0);
                    status.site_contacts = payload.site_contacts.unwrap_or(0);
                    status.source_audit = payload.source_audit.unwrap_or(0);
                    status.integrity = payload.integrity.unwrap_or_else(|| "unknown".to_string());
                }
                Err(error) => {
                    status.error = Some(format!("状態JSONを解釈できません: {error}"));
                }
            },
            Err(error) => status.error = Some(error.to_string()),
        }
        status
    }

    pub async fn prepare(
        &self,
        source_path: &Path,
        sheet_name: Option<&str>,
        replace: bool,
    ) -> Result<PublicEnrichmentOperation> {
        ensure_source_file(source_path)?;
        let mut args = vec![
            OsString::from("prepare"),
            source_path.as_os_str().to_owned(),
        ];
        if let Some(sheet) = sheet_name.map(str::trim).filter(|value| !value.is_empty()) {
            if sheet.chars().count() > 128 || sheet.contains('\0') {
                return Err(anyhow!("Excelシート名が不正です"));
            }
            args.push(OsString::from("--sheet"));
            args.push(OsString::from(sheet));
        }
        if replace {
            args.push(OsString::from("--replace"));
        }
        let result = self
            .run_public_cli(args, Duration::from_secs(30 * 60))
            .await?;
        self.operation("prepare", result).await
    }

    pub async fn make_assignment(
        &self,
        output_path: &Path,
        chunk_size: u32,
    ) -> Result<PublicEnrichmentOperation> {
        if !(100..=100_000).contains(&chunk_size) {
            return Err(anyhow!("分割件数は100〜100000の範囲で指定してください"));
        }
        if let Some(parent) = output_path.parent() {
            std::fs::create_dir_all(parent).context("出力先フォルダを作成できません")?;
        }
        let args = vec![
            OsString::from("make-assignment"),
            OsString::from("--output"),
            output_path.as_os_str().to_owned(),
            OsString::from("--chunk-size"),
            OsString::from(chunk_size.to_string()),
        ];
        let result = self
            .run_public_cli(args, Duration::from_secs(10 * 60))
            .await?;
        self.operation("make-assignment", result).await
    }

    pub async fn run_all(
        &self,
        input_dir: &Path,
        accept_prefix: bool,
    ) -> Result<PublicEnrichmentOperation> {
        if !input_dir.is_dir() {
            return Err(anyhow!("公開データ入力フォルダが見つかりません"));
        }
        std::fs::create_dir_all(&self.inner.output_dir)
            .context("公開データ出力フォルダを作成できません")?;
        let mut args = vec![
            OsString::from("run-all"),
            OsString::from("--input-dir"),
            input_dir.as_os_str().to_owned(),
            OsString::from("--output-dir"),
            self.inner.output_dir.as_os_str().to_owned(),
        ];
        if accept_prefix {
            args.push(OsString::from("--accept-prefix"));
        }
        let (result, published) = {
            // The staging database and its published runtime/index generation
            // are one logical operation. Do not let another enrichment action
            // replace staging between these two subprocesses.
            let _guard = self.inner.gate.lock().await;
            let result = self
                .run_public_cli_unlocked(args, Duration::from_secs(2 * 60 * 60))
                .await?;
            let published = self
                .run_publish_cli_unlocked(Duration::from_secs(2 * 60 * 60))
                .await?;
            (result, published)
        };
        self.operation(
            "run-all",
            serde_json::json!({"staging": result, "published": published}),
        )
        .await
    }

    async fn operation(&self, action: &str, result: Value) -> Result<PublicEnrichmentOperation> {
        Ok(PublicEnrichmentOperation {
            action: action.to_string(),
            result,
            status: self.status().await,
        })
    }

    async fn run_public_cli(&self, args: Vec<OsString>, limit: Duration) -> Result<Value> {
        let _guard = self.inner.gate.lock().await;
        self.run_public_cli_unlocked(args, limit).await
    }

    async fn run_public_cli_unlocked(&self, args: Vec<OsString>, limit: Duration) -> Result<Value> {
        let python = self
            .inner
            .python
            .clone()
            .ok_or_else(|| anyhow!("Python 3.11以上が見つかりません"))?;
        let script = self.inner.script_dir.join(PUBLIC_SCRIPT);
        if !script.is_file() {
            return Err(anyhow!("公開データ統合スクリプトが見つかりません"));
        }

        let mut command = Command::new(&python.program);
        command
            .args(&python.prefix_args)
            .arg(&script)
            .arg("--db")
            .arg(&self.inner.db_path)
            .args(args)
            .current_dir(&self.inner.workspace_dir)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8");

        let output = bounded_output(
            command,
            limit,
            "公開データ統合処理が制限時間を超えました",
            "公開データ統合プロセスを起動できません",
            "公開データ統合プロセスの出力が大きすぎます",
        )
        .await?;
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        if !output.status.success() {
            let detail = if stderr.trim().is_empty() {
                stdout.trim()
            } else {
                stderr.trim()
            };
            return Err(anyhow!(
                "公開データ統合処理に失敗しました: {}",
                truncate_tail(detail, 4_000)
            ));
        }
        let start = stdout
            .find('{')
            .ok_or_else(|| anyhow!("公開データ統合処理からJSONが返されませんでした"))?;
        serde_json::from_str(stdout[start..].trim())
            .context("公開データ統合処理のJSONを解釈できません")
    }

    async fn run_publish_cli_unlocked(&self, limit: Duration) -> Result<Value> {
        let runtime = self.inner.publisher.clone().ok_or_else(|| {
            anyhow!("Queria統合CLIが見つかりません。QUERIA_MASTER_CLIを設定してください")
        })?;
        let canonical = self
            .inner
            .canonical_db
            .as_ref()
            .filter(|path| path.is_file())
            .ok_or_else(|| anyhow!("canonical DBが見つかりません"))?;
        let enrichment = self
            .inner
            .enrichment_db
            .as_ref()
            .ok_or_else(|| anyhow!("enrichment DBの出力先が未設定です"))?;
        let runtime_db = self
            .inner
            .runtime_db
            .as_ref()
            .ok_or_else(|| anyhow!("runtime DBの出力先が未設定です"))?;
        let search_index = self
            .inner
            .search_index
            .as_ref()
            .ok_or_else(|| anyhow!("検索索引の出力先が未設定です"))?;
        let mut command = Command::new(&runtime.program);
        command
            .args(&runtime.prefix_args)
            .arg("--db")
            .arg(canonical)
            .arg("integrate-public-enrichment")
            .arg("--staging-db")
            .arg(&self.inner.db_path)
            .arg("--enrichment-db")
            .arg(enrichment)
            .arg("--runtime-db")
            .arg(runtime_db)
            .arg("--search-index")
            .arg(search_index)
            .current_dir(&runtime.working_dir)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8");
        let output = bounded_output(
            command,
            limit,
            "canonical runtime/index公開が制限時間を超えました",
            "Queria統合CLIを起動できません",
            "Queria統合CLIの出力が大きすぎます",
        )
        .await?;
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        if !output.status.success() {
            let detail = if stderr.trim().is_empty() {
                stdout.trim()
            } else {
                stderr.trim()
            };
            return Err(anyhow!(
                "stagingは保存しましたがcanonical runtime/indexへ公開できませんでした: {}",
                truncate_tail(detail, 4_000)
            ));
        }
        let start = stdout
            .find('{')
            .ok_or_else(|| anyhow!("Queria統合CLIからJSONが返されませんでした"))?;
        serde_json::from_str(stdout[start..].trim()).context("Queria統合CLIのJSONを解釈できません")
    }
}

async fn bounded_output(
    mut command: Command,
    limit: Duration,
    timeout_message: &'static str,
    spawn_message: &'static str,
    overflow_message: &'static str,
) -> Result<BoundedOutput> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let operation = async move {
        let mut child = command.spawn().with_context(|| spawn_message)?;
        let stdout = child
            .stdout
            .take()
            .context("subprocess stdoutを取得できません")?;
        let stderr = child
            .stderr
            .take()
            .context("subprocess stderrを取得できません")?;
        let total = Arc::new(AtomicUsize::new(0));
        let (stdout, stderr) = tokio::try_join!(
            read_bounded(stdout, total.clone(), overflow_message),
            read_bounded(stderr, total, overflow_message),
        )?;
        let status = child
            .wait()
            .await
            .context("subprocessの終了を待機できません")?;
        Ok(BoundedOutput {
            status,
            stdout,
            stderr,
        })
    };

    timeout(limit, operation)
        .await
        .map_err(|_| anyhow!(timeout_message))?
}

async fn read_bounded<R: AsyncRead + Unpin>(
    mut reader: R,
    total: Arc<AtomicUsize>,
    overflow_message: &'static str,
) -> Result<Vec<u8>> {
    let mut output = Vec::new();
    let mut buffer = [0u8; 16 * 1024];
    loop {
        let read = reader.read(&mut buffer).await?;
        if read == 0 {
            return Ok(output);
        }
        let reserved = total.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |used| {
            used.checked_add(read)
                .filter(|next| *next <= MAX_OUTPUT_BYTES)
        });
        if reserved.is_err() {
            return Err(io::Error::new(io::ErrorKind::Other, overflow_message).into());
        }
        output.extend_from_slice(&buffer[..read]);
    }
}

fn discover_publisher(
    script_dir: &Path,
    python: Option<&PythonRuntime>,
) -> Option<PublisherRuntime> {
    if let Ok(value) = std::env::var("QUERIA_MASTER_CLI") {
        let program = PathBuf::from(value);
        if program.is_file() {
            return Some(PublisherRuntime {
                working_dir: program.parent().unwrap_or(Path::new(".")).to_path_buf(),
                program,
                prefix_args: Vec::new(),
            });
        }
    }
    if let Ok(program) = which::which(if cfg!(windows) {
        "queria-master.exe"
    } else {
        "queria-master"
    }) {
        return Some(PublisherRuntime {
            working_dir: program.parent().unwrap_or(Path::new(".")).to_path_buf(),
            program,
            prefix_args: Vec::new(),
        });
    }
    let cli_name = if cfg!(windows) {
        "queria-master.exe"
    } else {
        "queria-master"
    };
    let mut bundled_candidates = Vec::new();
    if let Ok(executable) = std::env::current_exe() {
        if let Some(parent) = executable.parent() {
            bundled_candidates.push(parent.join(cli_name));
        }
    }
    if let Some(resource_root) = script_dir.parent() {
        bundled_candidates.push(resource_root.join(cli_name));
        bundled_candidates.push(resource_root.join("bin").join(cli_name));
    }
    if let Some(program) = bundled_candidates.into_iter().find(|path| path.is_file()) {
        return Some(PublisherRuntime {
            working_dir: program.parent().unwrap_or(Path::new(".")).to_path_buf(),
            program,
            prefix_args: Vec::new(),
        });
    }
    let project_root = script_dir.parent().and_then(Path::parent)?;
    if project_root.join("queria_master").is_dir() && project_root.join("pyproject.toml").is_file()
    {
        let python = python?.clone();
        let mut prefix_args = python.prefix_args;
        prefix_args.extend(["-m".to_string(), "queria_master".to_string()]);
        return Some(PublisherRuntime {
            program: python.program,
            prefix_args,
            working_dir: project_root.to_path_buf(),
        });
    }
    None
}

fn discover_script_dir(resource_dir: &Path) -> PathBuf {
    let mut candidates = vec![resource_dir.join("public_enrichment")];
    if let Ok(current_dir) = std::env::current_dir() {
        candidates.push(current_dir.join("public_enrichment"));
        candidates.push(current_dir.join("company_scout").join("public_enrichment"));
    }
    if cfg!(debug_assertions) {
        candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../public_enrichment"));
    }
    candidates
        .into_iter()
        .find(|path| path.join(PUBLIC_SCRIPT).is_file())
        .unwrap_or_else(|| resource_dir.join("public_enrichment"))
}

fn discover_python() -> Option<PythonRuntime> {
    let mut candidates: Vec<(PathBuf, Vec<String>)> = Vec::new();
    if let Ok(value) = std::env::var("COMPANYMASTER_PYTHON") {
        let path = PathBuf::from(value);
        if path.is_file() {
            candidates.push((path, Vec::new()));
        }
    }
    if cfg!(windows) {
        if let Ok(path) = which::which("py") {
            candidates.push((path, vec!["-3".to_string()]));
        }
    }
    for name in ["python3", "python"] {
        if let Ok(path) = which::which(name) {
            if !candidates.iter().any(|(known, _)| known == &path) {
                candidates.push((path, Vec::new()));
            }
        }
    }
    candidates
        .into_iter()
        .find_map(|(program, prefix_args)| probe_python(program, prefix_args))
}

fn probe_python(program: PathBuf, prefix_args: Vec<String>) -> Option<PythonRuntime> {
    let output = std::process::Command::new(&program)
        .args(&prefix_args)
        .arg("--version")
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = format!(
        "{} {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let version = text
        .split_whitespace()
        .find(|part| part.chars().next().is_some_and(|ch| ch.is_ascii_digit()))?
        .trim()
        .to_string();
    let mut numbers = version
        .split('.')
        .filter_map(|part| part.parse::<u32>().ok());
    let major = numbers.next()?;
    let minor = numbers.next()?;
    if major < 3 || (major == 3 && minor < 11) {
        return None;
    }
    Some(PythonRuntime {
        program,
        prefix_args,
        version: format!("Python {version}"),
    })
}

fn ensure_source_file(path: &Path) -> Result<()> {
    if !path.is_file() {
        return Err(anyhow!("企業リストが見つかりません"));
    }
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(extension.as_str(), "csv" | "xlsx" | "xlsm") {
        return Err(anyhow!("企業リストはCSV、XLSX、XLSMだけを指定できます"));
    }
    Ok(())
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn truncate_tail(value: &str, max_chars: usize) -> String {
    let chars: Vec<char> = value.chars().collect();
    if chars.len() <= max_chars {
        value.to_string()
    } else {
        chars[chars.len() - max_chars..].iter().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_extensions_are_restricted() {
        let root = std::env::temp_dir().join(format!(
            "company-master-public-enrichment-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let csv = root.join("companies.csv");
        let exe = root.join("companies.exe");
        std::fs::write(&csv, b"name,address\n").unwrap();
        std::fs::write(&exe, b"not executable").unwrap();
        assert!(ensure_source_file(&csv).is_ok());
        assert!(ensure_source_file(&exe).is_err());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn output_truncation_keeps_the_tail() {
        assert_eq!(truncate_tail("abcdef", 3), "def");
        assert_eq!(truncate_tail("abc", 3), "abc");
    }

    #[tokio::test]
    async fn subprocess_streams_share_one_output_budget() {
        let total = Arc::new(AtomicUsize::new(MAX_OUTPUT_BYTES - 1));
        let error = read_bounded(&b"ab"[..], total, "too large")
            .await
            .expect_err("combined output must be rejected");
        assert!(error.to_string().contains("too large"));
    }
}
