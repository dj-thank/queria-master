import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Bot,
  Building2,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  ExternalLink,
  FileSearch,
  Filter,
  ListPlus,
  Loader2,
  LogIn,
  LogOut,
  RefreshCw,
  Search,
  Send,
  Settings2,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { open, save } from "@tauri-apps/plugin-dialog";
import { api } from "./api";
import type {
  CodexStatus,
  Company,
  DataStatus,
  ResearchReport,
  SalesforceStatus,
  SalesforceFieldMapping,
  SalesforceJobStatus,
  SavedSearch,
  SearchPlan,
  SearchResult,
} from "./types";

const blankPlan: SearchPlan = {
  prefectures: [],
  cities: [],
  industry_codes: [],
  industry_terms: [],
  company_kinds: [],
  keyword_any: [],
  keyword_all: [],
  limit: 30000,
};

type View = "search" | "ai" | "research" | "connections";

const fmt = new Intl.NumberFormat("ja-JP");
const splitValues = (value: string) => value.split(/[、,\n]/).map((v) => v.trim()).filter(Boolean);
const optionalNumber = (value: string) => value.trim() === "" ? undefined : Number(value);
const currentYear = new Date().getFullYear();
const prefectures = ["北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"];
const industryPresets: Array<{ label: string; codes: string[]; terms: string[] }> = [
  { label: "情報通信業", codes: ["37", "38", "39", "40", "41"], terms: [] },
  { label: "製造業", codes: [], terms: ["製造"] },
  { label: "建設・土木", codes: [], terms: ["建設"] },
  { label: "卸売・小売", codes: [], terms: ["販売"] },
  { label: "金融・保険", codes: [], terms: ["金融"] },
  { label: "医療・福祉", codes: [], terms: ["医療"] },
  { label: "教育", codes: [], terms: ["教育"] },
  { label: "運輸・物流", codes: [], terms: ["運輸"] },
];
const industryMiddle = [
  { code: "37", label: "通信業" },
  { code: "38", label: "放送業" },
  { code: "39", label: "情報サービス業" },
  { code: "40", label: "インターネット附随サービス業" },
  { code: "41", label: "映像・音声・文字情報制作業" },
];
const companyKinds = ["株式会社", "有限会社", "合同会社", "合資会社", "合名会社", "医療法人", "学校法人", "社会福祉法人", "NPO法人"];
const employeeRanges = [
  { label: "指定なし", min: undefined, max: undefined },
  { label: "1〜9名", min: 1, max: 9 },
  { label: "10〜49名", min: 10, max: 49 },
  { label: "50〜299名", min: 50, max: 299 },
  { label: "300〜999名", min: 300, max: 999 },
  { label: "1,000名以上", min: 1000, max: undefined },
];
const capitalRanges = [
  { label: "指定なし", min: undefined, max: undefined },
  { label: "〜1,000万円", min: undefined, max: 10_000_000 },
  { label: "1,000万〜1億円", min: 10_000_000, max: 100_000_000 },
  { label: "1億〜10億円", min: 100_000_000, max: 1_000_000_000 },
  { label: "10億円以上", min: 1_000_000_000, max: undefined },
];
const establishedRanges = [
  { label: "指定なし", from: undefined, to: undefined },
  { label: "5年以内", from: currentYear - 5, to: undefined },
  { label: "10年以内", from: currentYear - 10, to: undefined },
  { label: "20年以内", from: currentYear - 20, to: undefined },
  { label: "2000年以前", from: undefined, to: 2000 },
];
const toggleValue = (values: string[], value: string) => values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
const displayPageSize = (targetPlan: SearchPlan) => Math.max(1, Math.min(targetPlan.limit, 30000));
const virtualRowHeight = 52;

function compactNumber(value?: number | null) {
  if (value == null) return "-";
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(1)}億`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(0)}万`;
  return fmt.format(value);
}

function App() {
  const [view, setView] = useState<View>("search");
  const [prompt, setPrompt] = useState("東京都のSaaS・受託開発会社で、従業員30〜300名、Webサイトあり");
  const [plan, setPlan] = useState<SearchPlan>(blankPlan);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Company | null>(null);
  const [research, setResearch] = useState<ResearchReport | null>(null);
  const [codex, setCodex] = useState<CodexStatus | null>(null);
  const [data, setData] = useState<DataStatus | null>(null);
  const [salesforce, setSalesforce] = useState<SalesforceStatus | null>(null);
  const [salesforceJob, setSalesforceJob] = useState<SalesforceJobStatus | null>(null);
  const [sfObjectName, setSfObjectName] = useState("Account");
  const [sfExternalId, setSfExternalId] = useState("CorporateNumber__c");
  const [sfMappingText, setSfMappingText] = useState("name=Name\ncorporate_number=CorporateNumber__c\nwebsite=Website\nphone=Phone\nprefecture=BillingState\ncity=BillingCity\naddress=BillingStreet\nindustry=Industry\nemployees=NumberOfEmployees\nbusiness_summary=Description");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string>("");
  const [listName, setListName] = useState("営業候補");
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [fieldCategory, setFieldCategory] = useState<"region" | "industry" | "organization" | "scale" | "keywords">("region");
  const [listScrollTop, setListScrollTop] = useState(0);
  const listScrollRef = useRef<HTMLDivElement>(null);

  const displayRows = result?.rows ?? [];
  const virtualStart = Math.max(0, Math.floor(listScrollTop / virtualRowHeight) - 8);
  const virtualEnd = Math.min(displayRows.length, virtualStart + 60);
  const virtualRows = displayRows.slice(virtualStart, virtualEnd);

  const filters = useMemo(() => {
    const out: string[] = [];
    plan.prefectures.forEach((v) => out.push(v));
    plan.cities.forEach((v) => out.push(v));
    plan.industry_terms.forEach((v) => out.push(v));
    plan.industry_codes.forEach((v) => out.push(`業種 ${v}`));
    if (plan.min_employees != null || plan.max_employees != null)
      out.push(`従業員 ${plan.min_employees ?? 0}〜${plan.max_employees ?? "∞"}`);
    if (plan.min_capital != null || plan.max_capital != null)
      out.push(`資本金 ${compactNumber(plan.min_capital)}〜${compactNumber(plan.max_capital)}`);
    if (plan.established_from != null || plan.established_to != null)
      out.push(`設立年 ${plan.established_from ?? "?"}〜${plan.established_to ?? "?"}`);
    if (plan.website_required) out.push("Webあり");
    return out;
  }, [plan]);

  useEffect(() => {
    void refreshStatus();
  }, []);

  async function refreshStatus() {
    try {
      const [d, c, sf, memories] = await Promise.allSettled([
        api.bootstrap(),
        api.codexStatus(),
        api.salesforceStatus(),
        api.recentSearches(12),
      ]);
      if (d.status === "fulfilled") setData(d.value);
      if (c.status === "fulfilled") setCodex(c.value);
      if (sf.status === "fulfilled") setSalesforce(sf.value);
      if (memories.status === "fulfilled") setSavedSearches(memories.value);
    } catch (error) {
      setMessage(String(error));
    }
  }

  async function consultAndSearch() {
    if (!prompt.trim()) return;
    setBusy("plan");
    setMessage("");
    try {
      const query = prompt.trim();
      const next = await api.planSearch(query);
      setPlan(next);
      setPage(1);
      await api.saveSearch(query.slice(0, 48), query, next);
      api.recentSearches(12).then(setSavedSearches).catch(() => undefined);
      const found = await api.search(next, 1, displayPageSize(next));
      setResult(found);
      setSelected(found.rows[0] ?? null);
      setView("search");
      setListScrollTop(0);
      listScrollRef.current?.scrollTo({ top: 0 });
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function runSearch(targetPage = 1, targetPlan = plan) {
    setBusy("search");
    setMessage("");
    try {
      const found = await api.search(targetPlan, targetPage, displayPageSize(targetPlan));
      setResult(found);
      setPage(targetPage);
      setSelected(found.rows[0] ?? null);
      setListScrollTop(0);
      listScrollRef.current?.scrollTo({ top: 0 });
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function extractWithLimit(limit: number) {
    const targetPlan = { ...plan, limit };
    setPlan(targetPlan);
    await runSearch(1, targetPlan);
  }

  async function loginCodex() {
    setBusy("codex-login");
    try {
      const { auth_url } = await api.codexLogin();
      await openUrl(auth_url);
      setMessage("ブラウザでChatGPTログインを完了してください。完了後、この画面の状態を更新します。");
      setTimeout(() => void refreshStatus(), 3500);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function logoutCodex() {
    setBusy("codex-logout");
    try {
      await api.codexLogout();
      await refreshStatus();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function deepResearch() {
    if (!selected) return;
    setBusy("research");
    setResearch(null);
    setView("research");
    try {
      const report = await api.researchCompany(
        selected,
        "公式サイトと公開Web情報を優先し、事業内容・顧客・提供サービス・強み・想定される日本標準産業分類を調べる。根拠URLを残す。",
      );
      setResearch(report);
      await refreshStatus();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function collectPhone() {
    if (!selected) return;
    setBusy("phone");
    try {
      const value = await api.collectCompanyPhone(selected);
      if (value.phone) {
        setSelected({ ...selected, phone: value.phone });
        setResult((current) => current ? { ...current, rows: current.rows.map((row) => row.corporate_number === selected.corporate_number ? { ...row, phone: value.phone } : row) } : current);
        setMessage(`公式サイトから電話番号 ${value.phone} を取得しました。`);
      } else {
        setMessage("公式サイトから電話番号を確認できませんでした。");
      }
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function exportCsv() {
    const path = await save({ defaultPath: "company-list.csv", filters: [{ name: "CSV", extensions: ["csv"] }] });
    if (!path) return;
    setBusy("export");
    try {
      const count = await api.exportCsv(plan, path);
      setMessage(`${fmt.format(count)}件をCSVに出力しました。`);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function exportXlsx() {
    const path = await save({ defaultPath: "company-list.xlsx", filters: [{ name: "Excel", extensions: ["xlsx"] }] });
    if (!path) return;
    setBusy("export");
    try {
      const count = await api.exportXlsx(plan, path);
      setMessage(`${fmt.format(count)}件をExcel（XLSX）に出力しました。`);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function importCompanies() {
    const file = await open({ multiple: false, filters: [{ name: "DuckDB / Company data", extensions: ["duckdb", "db", "parquet", "csv", "json", "jsonl"] }] });
    if (!file || Array.isArray(file)) return;
    setBusy("import");
    try {
      const count = await api.importFile(file);
      setMessage(`${fmt.format(count)}件を取り込みました。`);
      await refreshStatus();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function syncDuckDb() {
    setBusy("duckdb");
    try {
      const value = await api.syncDuckDb();
      setMessage(`${fmt.format(value.imported)}件をDuckDBネイティブ同期しました（${value.duckdb_version}）。`);
      await refreshStatus();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function addSearchResultsToList() {
    if (!result?.rows.length) return;
    setBusy("list");
    try {
      const count = await api.addSearchToList(listName, plan);
      setMessage(`${listName} に検索結果から ${fmt.format(count)}件追加しました。`);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function sendListToSalesforce() {
    if (!salesforce?.connected) return;
    setBusy("salesforce-upsert");
    try {
      const mapping: SalesforceFieldMapping[] = sfMappingText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
        const [source, ...targetParts] = line.split("=");
        return { source: source.trim(), target: targetParts.join("=").trim() };
      }).filter((field) => field.source && field.target);
      await api.addSearchToList(listName, plan);
      const value = await api.salesforceUpsertList(listName, sfObjectName, sfExternalId, mapping);
      setSalesforceJob({ job_id: value.job_id, state: "UploadComplete", number_records_processed: 0, number_records_failed: 0, number_records_total: value.accepted, error_message: null });
      setMessage(`${fmt.format(value.accepted)}件をSalesforceへ送信しました。Bulk Job: ${value.job_id}`);
      void pollSalesforceJob(value.job_id);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function pollSalesforceJob(jobId: string) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      try {
        const status = await api.salesforceJobStatus(jobId);
        setSalesforceJob(status);
        if (["JobComplete", "Failed", "Aborted"].includes(status.state)) return;
      } catch {
        return;
      }
    }
  }

  async function refreshSalesforceJob() {
    if (!salesforceJob) return;
    try {
      const status = await api.salesforceJobStatus(salesforceJob.job_id);
      setSalesforceJob(status);
      setMessage(`Salesforceジョブ ${status.state}: 成功処理 ${fmt.format(status.number_records_processed)}件、失敗 ${fmt.format(status.number_records_failed)}件`);
    } catch (error) {
      setMessage(String(error));
    }
  }

  async function retrySalesforceFailed() {
    if (!salesforceJob) return;
    setBusy("salesforce-retry");
    try {
      const value = await api.salesforceRetryFailed(salesforceJob.job_id);
      setSalesforceJob({ job_id: value.job_id, state: "UploadComplete", number_records_processed: 0, number_records_failed: 0, number_records_total: value.accepted, error_message: null });
      setMessage(`失敗行を${fmt.format(value.accepted)}件再送しました。Bulk Job: ${value.job_id}`);
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function importTaxonomy() {
    const file = await open({ multiple: false, filters: [{ name: "Normalized JSIC", extensions: ["csv", "parquet"] }] });
    if (!file || Array.isArray(file)) return;
    setBusy("taxonomy");
    try {
      const count = await api.importTaxonomy(file);
      setMessage(`産業分類 ${fmt.format(count)}件を取り込みました。`);
      await refreshStatus();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Building2 size={19} /></div>
          <div><strong>CompanyMaster</strong><span>企業探索ワークベンチ</span></div>
        </div>
        <nav>
          <button className={view === "search" ? "active" : ""} onClick={() => setView("search")}><Search size={17} />検索</button>
          <button className={view === "ai" ? "active" : ""} onClick={() => setView("ai")}><Bot size={17} />AI検索</button>
          <button className={view === "research" ? "active" : ""} onClick={() => setView("research")}><FileSearch size={17} />企業調査</button>
          <button className={view === "connections" ? "active" : ""} onClick={() => setView("connections")}><Settings2 size={17} />接続</button>
        </nav>
        <div className="sidebar-stats">
          <div><span>企業</span><strong>{data ? fmt.format(data.company_count) : "-"}</strong></div>
          <div><span>業種分類</span><strong>{data ? fmt.format(data.taxonomy_count) : "-"}</strong></div>
          <div><span>調査メモ</span><strong>{data ? fmt.format(data.research_count) : "-"}</strong></div>
        </div>
        <div className="model-lock"><Sparkles size={14} /><div><span>LLM固定</span><strong>GPT-5.6 Luna</strong></div></div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="top-status">
            <span className={codex?.authenticated ? "dot ok" : "dot"} />
            {codex?.authenticated ? codex.email ?? "ChatGPTログイン済み" : "ChatGPT未ログイン"}
            {codex?.authenticated && !codex.luna_available && <span className="warning-pill">Luna利用不可</span>}
          </div>
          {codex?.authenticated ? (
            <button className="ghost small" onClick={logoutCodex} disabled={busy === "codex-logout"}><LogOut size={15} />ログアウト</button>
          ) : (
            <button className="primary small" onClick={loginCodex} disabled={busy === "codex-login"}><LogIn size={15} />ChatGPTでログイン</button>
          )}
        </header>

        {message && <div className="notice"><span>{message}</span><button onClick={() => setMessage("")}><X size={15} /></button></div>}

        {view === "search" && (
          <section className="workspace">
            <div className="search-mode-header">
              <div className="search-heading"><span className="eyebrow"><Settings2 size={14} /> 項目検索</span><h1>企業検索</h1><p>条件を選択して、必要な企業だけを正確に絞り込みます。</p></div>
              <span className="field-mode-badge"><Settings2 size={13} /> 選択式フィルター</span>
            </div>

            <div className="hero-search field-search-card">
              <div className="field-search-intro"><div><strong>項目を選んで検索</strong><span>カテゴリを切り替えて選択できます。画面全体を縦にスクロールする必要はありません。</span></div></div>
              <div className="chips field-summary"><span className="filter-label"><Filter size={14} />選択中</span>{filters.length ? filters.map((f) => <span className="chip" key={f}>{f}</span>) : <span className="muted">条件なし（全国・全業種）</span>}<button className="ghost tiny" onClick={() => runSearch(1)} disabled={!!busy}><RefreshCw size={13} />再検索</button></div>

              <div className="condition-panel">
                <div className="condition-panel-head">
                  <div><span className="condition-title"><Settings2 size={14}/> 条件を選んで絞り込み</span><small>クリックで複数選択。条件は検索ボタンを押すまで適用されません。</small></div>
                  <button className="ghost tiny" onClick={() => setPlan({ ...blankPlan })} disabled={!!busy}>条件をクリア</button>
                </div>
                <div className="field-category-tabs" role="tablist" aria-label="項目検索カテゴリ">
                  {([['region', '地域'], ['industry', '業種'], ['organization', '法人種別'], ['scale', '規模・設立'], ['keywords', 'キーワード']] as const).map(([id, label]) => <button key={id} className={fieldCategory === id ? "active" : ""} onClick={() => setFieldCategory(id)} role="tab" aria-selected={fieldCategory === id}>{label}{id === "region" && plan.prefectures.length > 0 && <b>{plan.prefectures.length}</b>}{id === "industry" && (plan.industry_codes.length + plan.industry_terms.length) > 0 && <b>{plan.industry_codes.length + plan.industry_terms.length}</b>}{id === "organization" && plan.company_kinds.length > 0 && <b>{plan.company_kinds.length}</b>}{id === "keywords" && (plan.keyword_any.length + plan.keyword_all.length) > 0 && <b>{plan.keyword_any.length + plan.keyword_all.length}</b>}</button>)}
                </div>

                {fieldCategory === "region" && <div className="condition-section">
                  <div className="condition-section-head"><strong>地域</strong><span>{plan.prefectures.length ? `${plan.prefectures.length}都道府県` : "全国"}</span></div>
                  <div className="option-grid prefecture-options">
                    {prefectures.map((value) => <button key={value} className={`option-pill ${plan.prefectures.includes(value) ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, prefectures: toggleValue(p.prefectures, value) }))}>{value}</button>)}
                  </div>
                  <div className="condition-input-row"><label><span>市区町村（任意）</span><input value={plan.cities.join(", ")} onChange={(e) => setPlan((p) => ({ ...p, cities: splitValues(e.target.value) }))} placeholder="千代田区、横浜市" /></label></div>
                </div>}

                {fieldCategory === "industry" && <div className="condition-section">
                  <div className="condition-section-head"><strong>業種</strong><span>大きな分類から選択できます</span></div>
                  <div className="option-grid industry-options">
                    {industryPresets.map((preset) => {
                      const active = preset.codes.every((code) => plan.industry_codes.includes(code)) && preset.terms.every((term) => plan.industry_terms.includes(term));
                      return <button key={preset.label} className={`option-pill ${active ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, industry_codes: active ? p.industry_codes.filter((code) => !preset.codes.includes(code)) : Array.from(new Set([...p.industry_codes, ...preset.codes])), industry_terms: active ? p.industry_terms.filter((term) => !preset.terms.includes(term)) : Array.from(new Set([...p.industry_terms, ...preset.terms])) }))}>{preset.label}</button>;
                    })}
                  </div>
                  <div className="subsection-label">情報通信業の中分類（JSICコード）</div>
                  <div className="option-grid middle-options">
                    {industryMiddle.map((item) => <button key={item.code} className={`option-pill ${plan.industry_codes.includes(item.code) ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, industry_codes: toggleValue(p.industry_codes, item.code) }))}><b>{item.code}</b>{item.label}</button>)}
                  </div>
                  <div className="condition-input-row two"><label><span>業種コード（小分類・細分類も可）</span><input value={plan.industry_codes.filter((code) => !industryMiddle.some((item) => item.code === code)).join(", ")} onChange={(e) => setPlan((p) => ({ ...p, industry_codes: [...industryMiddle.map((item) => item.code).filter((code) => p.industry_codes.includes(code)), ...splitValues(e.target.value)] }))} placeholder="例: 391 / 3911" /></label><label><span>業種キーワード</span><input value={plan.industry_terms.join(", ")} onChange={(e) => setPlan((p) => ({ ...p, industry_terms: splitValues(e.target.value) }))} placeholder="SaaS、食品製造" /></label></div>
                </div>}

                {fieldCategory === "organization" && <div className="condition-section">
                  <div className="condition-section-head"><strong>法人種別</strong><span>{plan.company_kinds.length ? `${plan.company_kinds.length}種類` : "指定なし"}</span></div>
                  <div className="option-grid kind-options">{companyKinds.map((kind) => <button key={kind} className={`option-pill ${plan.company_kinds.includes(kind) ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, company_kinds: toggleValue(p.company_kinds, kind) }))}>{kind}</button>)}</div>
                </div>}

                {fieldCategory === "scale" && <div className="condition-section">
                  <div className="condition-section-head"><strong>規模・設立</strong><span>よく使う範囲をワンクリック</span></div>
                  <div className="range-group"><span className="range-label">従業員数</span><div className="range-options">{employeeRanges.map((range) => { const active = plan.min_employees === range.min && plan.max_employees === range.max; return <button key={range.label} className={`range-choice ${active ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, min_employees: range.min, max_employees: range.max }))}>{range.label}</button>; })}</div></div>
                  <div className="range-group"><span className="range-label">資本金</span><div className="range-options">{capitalRanges.map((range) => { const active = plan.min_capital === range.min && plan.max_capital === range.max; return <button key={range.label} className={`range-choice ${active ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, min_capital: range.min, max_capital: range.max }))}>{range.label}</button>; })}</div></div>
                  <div className="range-group"><span className="range-label">設立年</span><div className="range-options">{establishedRanges.map((range) => { const active = plan.established_from === range.from && plan.established_to === range.to; return <button key={range.label} className={`range-choice ${active ? "active" : ""}`} onClick={() => setPlan((p) => ({ ...p, established_from: range.from, established_to: range.to }))}>{range.label}</button>; })}</div></div>
                  <div className="condition-input-row three"><label><span>従業員 最小</span><input type="number" min={0} value={plan.min_employees ?? ""} onChange={(e) => setPlan((p) => ({ ...p, min_employees: optionalNumber(e.target.value) }))} /></label><label><span>従業員 最大</span><input type="number" min={0} value={plan.max_employees ?? ""} onChange={(e) => setPlan((p) => ({ ...p, max_employees: optionalNumber(e.target.value) }))} /></label><label><span>Webサイト</span><select value={plan.website_required == null ? "any" : plan.website_required ? "yes" : "no"} onChange={(e) => setPlan((p) => ({ ...p, website_required: e.target.value === "any" ? undefined : e.target.value === "yes" }))}><option value="any">指定なし</option><option value="yes">あり</option><option value="no">なし</option></select></label></div>
                </div>}

                {fieldCategory === "keywords" && <div className="condition-section compact-section">
                  <div className="condition-input-row two"><label><span>キーワード（いずれかを含む）</span><input value={plan.keyword_any.join(", ")} onChange={(e) => setPlan((p) => ({ ...p, keyword_any: splitValues(e.target.value) }))} placeholder="SaaS、クラウド、食品" /></label><label><span>キーワード（すべてを含む）</span><input value={plan.keyword_all.join(", ")} onChange={(e) => setPlan((p) => ({ ...p, keyword_all: splitValues(e.target.value) }))} placeholder="自社開発、法人向け" /></label></div>
                  <div className="condition-input-row limit-row"><label><span>最大抽出件数</span><input type="number" min={1} max={2000000} value={plan.limit} onChange={(e) => setPlan((p) => ({ ...p, limit: Math.max(1, Math.min(2000000, Number(e.target.value) || 1)) }))} /></label><span className="condition-help">業種コードは前方一致。公式業種とAI推定は結果画面で区別して表示します。</span></div>
                </div>}
                <div className="condition-actions"><span>{filters.length ? `${filters.length}個の条件を選択中` : "条件なし（全国・全業種）"}</span><button className="primary" onClick={() => runSearch(1)} disabled={!!busy}><Search size={15} />この条件で検索</button></div>
              </div>
            </div>

            <div className="toolbar">
              <div className="result-title">
                <strong>{result ? `${fmt.format(result.total)}社` : "企業を検索"}</strong>
                {result && <span>{result.elapsed_ms} ms</span>}
              </div>
              <div className="toolbar-actions">
                <input value={listName} onChange={(e) => setListName(e.target.value)} className="list-name" aria-label="リスト名" />
                <button className="ghost" onClick={() => void extractWithLimit(30000)} disabled={!!busy}><Search size={15} />30,000件抽出</button>
                <button className="ghost" onClick={() => void extractWithLimit(2000000)} disabled={!!busy}><Search size={15} />全件抽出</button>
                <button className="ghost" onClick={addSearchResultsToList} disabled={!result?.rows.length || !!busy}><ListPlus size={15} />検索結果をリストへ</button>
                <button className="ghost" onClick={exportCsv} disabled={!result || !!busy}><Download size={15} />CSV</button>
                <button className="ghost" onClick={exportXlsx} disabled={!result || !!busy}><Download size={15} />Excel</button>
                <button className="primary" onClick={sendListToSalesforce} disabled={!result?.rows.length || !salesforce?.connected || !!busy}><Send size={15} />Salesforce</button>
              </div>
            </div>

            <div className="content-grid">
              <div className="table-card">
                <div className="result-table" role="table">
                  <div className="result-grid result-header" role="row"><span>会社</span><span>所在地</span><span>業種</span><span>従業員</span><span>資本金</span><span>電話</span><span>Web</span></div>
                  {displayRows.length ? <div className="result-scroll" ref={listScrollRef} onScroll={(e) => setListScrollTop(e.currentTarget.scrollTop)}>
                    <div className="virtual-canvas" style={{ height: `${displayRows.length * virtualRowHeight}px` }}>
                      <div className="virtual-rows" style={{ transform: `translateY(${virtualStart * virtualRowHeight}px)` }}>
                        {virtualRows.map((company) => <div key={company.corporate_number} className={`result-grid result-row ${selected?.corporate_number === company.corporate_number ? "selected" : ""}`} role="row" onClick={() => setSelected(company)}>
                          <span><strong>{company.name}</strong><small>{company.corporate_number}</small></span>
                          <span>{[company.prefecture, company.city].filter(Boolean).join(" ") || "-"}</span>
                          <span><span>{company.industry_name || company.inferred_industry_name || "-"}</span>{company.inferred_industry_name && !company.industry_name && <small className="inferred">AI推定</small>}</span>
                          <span>{company.employees != null ? fmt.format(company.employees) : "-"}</span>
                          <span>{compactNumber(company.capital)}</span>
                          <span>{company.phone || "-"}</span>
                          <span>{company.website ? <a href={company.website} onClick={(e) => { e.stopPropagation(); void openUrl(company.website!); }}><ExternalLink size={15} /></a> : "-"}</span>
                        </div>)}
                      </div>
                    </div>
                  </div> : <div className="empty">自然文で条件を書き、Lunaに相談すると検索条件を作って抽出します。</div>}
                  {displayRows.length > 0 && <div className="result-footnote">{fmt.format(displayRows.length)}件を読み込み済み・スクロールは仮想描画で高速表示</div>}
                </div>
                {result && result.total > result.page_size && (
                  <div className="pager">
                    <button onClick={() => runSearch(Math.max(1, page - 1))} disabled={page <= 1 || !!busy}><ChevronLeft size={16} /></button>
                    <span>{page} / {Math.ceil(result.total / result.page_size)}</span>
                    <button onClick={() => runSearch(page + 1)} disabled={page >= Math.ceil(result.total / result.page_size) || !!busy}><ChevronRight size={16} /></button>
                  </div>
                )}
              </div>

              <aside className="inspector">
                {selected ? <>
                  <div className="inspector-head"><div><span className="kicker">企業詳細</span><h2>{selected.name}</h2></div>{selected.website && <button className="icon-btn" onClick={() => void openUrl(selected.website!)}><ExternalLink size={17} /></button>}</div>
                  <dl>
                    <div><dt>法人番号</dt><dd>{selected.corporate_number}</dd></div>
                    <div><dt>所在地</dt><dd>{selected.address || [selected.prefecture, selected.city].filter(Boolean).join(" ") || "-"}</dd></div>
                    <div><dt>業種</dt><dd>{selected.industry_name || "-"}{selected.industry_code ? ` (${selected.industry_code})` : ""}</dd></div>
                    <div><dt>従業員</dt><dd>{selected.employees != null ? `${fmt.format(selected.employees)}名` : "-"}</dd></div>
                    <div><dt>資本金</dt><dd>{selected.capital != null ? `${fmt.format(selected.capital)}円` : "-"}</dd></div>
                    <div><dt>設立年</dt><dd>{selected.established_year ?? "-"}</dd></div>
                    <div><dt>電話</dt><dd>{selected.phone || "-"}</dd></div>
                  </dl>
                  {selected.business_summary && <p className="summary">{selected.business_summary}</p>}
                  <button className="primary full" onClick={deepResearch} disabled={busy === "research" || !codex?.authenticated || !codex?.luna_available}>
                    {busy === "research" ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />} Webまで深掘り
                  </button>
                  <button className="ghost full" onClick={collectPhone} disabled={busy === "phone" || !selected.website}>
                    {busy === "phone" ? <Loader2 className="spin" size={16} /> : <Search size={16} />} 公式サイトから電話番号を取得
                  </button>
                </> : <div className="empty-inspector"><Building2 size={30} /><span>会社を選択</span></div>}
              </aside>
            </div>
          </section>
        )}

        {view === "ai" && (
          <section className="workspace ai-view">
            <div className="section-title"><div><span className="eyebrow"><Bot size={15} /> Luna AI Search</span><h1>AI検索</h1><p className="view-subtitle">自然文で条件を相談し、構造化した条件で企業を探します。</p></div><span className="luna-badge"><Sparkles size={12} />GPT-5.6 Luna固定</span></div>
            <div className="research-ai-card ai-page-card">
              <div className="research-ai-head"><div><strong>探したい企業を自然文で入力</strong><small>例：「東京都の情報通信業で、従業員50名以上、Webサイトあり」</small></div></div>
              <div className="prompt-row">
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="例：東京都の情報通信業で、従業員50名以上、Webサイトあり" />
                <button className="send" onClick={consultAndSearch} disabled={!!busy || !codex?.authenticated || !codex?.luna_available} title="Lunaで検索">
                  {busy === "plan" ? <Loader2 className="spin" size={20} /> : <Send size={20} />}
                </button>
              </div>
              <div className="ai-examples"><span>例から始める</span><button onClick={() => setPrompt("東京都の情報通信業で、従業員50名以上、Webサイトあり")}>東京のIT企業</button><button onClick={() => setPrompt("製造業で、従業員100〜500名、Webサイトあり")}>中堅メーカー</button><button onClick={() => setPrompt("法人向けSaaSを提供する会社")}>法人向けSaaS</button></div>
              <div className="memory-row"><span className="memory-label">検索メモリー</span><select value="" onChange={(e) => { const item = savedSearches.find((v) => v.id === e.target.value); if (!item) return; setPrompt(item.query); setPlan(item.plan); setView("search"); void runSearch(1, item.plan); }}><option value="">過去の検索を呼び出す</option>{savedSearches.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><span className="muted">{savedSearches.length ? `${savedSearches.length}件` : "未保存"}</span></div>
              <div className="ai-search-note"><Sparkles size={14} /><span>Lunaが条件をJSON化してから項目検索を実行します。検索結果は検索タブで30,000件まで高速表示できます。</span></div>
            </div>
            {!codex?.authenticated && <div className="ai-login-hint"><Bot size={20} /><div><strong>AI検索にはChatGPTログインが必要です</strong><span>右上のログインボタンから、このユーザー自身のアカウントで接続してください。</span></div></div>}
          </section>
        )}

        {view === "research" && (
          <section className="workspace research-view">
            <div className="section-title"><div><span className="eyebrow">企業調査</span><h1>{selected?.name ?? "会社を選択してください"}</h1></div>{selected && <button className="primary" onClick={deepResearch} disabled={busy === "research"}><Sparkles size={16} />再調査</button>}</div>
            {busy === "research" && <div className="research-loading"><Loader2 className="spin" size={24} /><div><strong>公開Web情報を調査中</strong><span>公式サイトを優先して、根拠URL付きのメモを生成します。</span></div></div>}
            {research ? <div className="research-grid">
              <article className="report-card"><h3>調査サマリー</h3><p>{research.summary}</p><h3>主な発見</h3><ul>{research.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>{research.industry_guess && <div className="industry-guess"><span>推定業種</span><strong>{research.industry_guess.name || research.industry_guess.code || "-"}</strong><small>信頼度 {Math.round((research.industry_guess.confidence ?? 0) * 100)}%</small></div>}</article>
              <article className="report-card"><h3>根拠</h3><div className="sources">{research.sources.map((s, i) => <button key={i} onClick={() => void openUrl(s.url)}><ExternalLink size={14} /><span><strong>{s.title || s.url}</strong><small>{s.note}</small></span></button>)}</div><h3>トランスクリプト</h3><pre>{research.transcript}</pre></article>
            </div> : !busy && <div className="blank-state"><FileSearch size={35} /><strong>調査結果はここに蓄積されます</strong><span>検索画面で会社を選び「Webまで深掘り」を実行してください。</span></div>}
          </section>
        )}

        {view === "connections" && (
          <section className="workspace connections">
            <div className="section-title"><div><span className="eyebrow">Connections</span><h1>データと外部サービス</h1></div><button className="ghost" onClick={refreshStatus}><RefreshCw size={15} />状態更新</button></div>
            <div className="connection-grid">
              <ConnectionCard icon={<Bot size={20} />} title="Codex App Server" status={codex?.authenticated ? "接続済み" : "未接続"} ok={!!codex?.authenticated} description="各ユーザーが自分のChatGPTアカウントでログイン。モデルはGPT-5.6 Lunaに固定。">
                {codex?.authenticated ? <button className="ghost" onClick={logoutCodex}>ログアウト</button> : <button className="primary" onClick={loginCodex}>ChatGPTでログイン</button>}
              </ConnectionCard>
              <ConnectionCard icon={<Database size={20} />} title="DuckDB Native / 公開法人データ" status={data?.duckdb_native ? (data.runtime_attached ? "Queria全量を接続中" : (data.duckdb_version || "組み込み済み")) : "初期化中"} ok={!!data?.duckdb_native} description={data?.runtime_attached ? "Queria runtime DB（約582万法人）をDuckDB純正のREAD_ONLY接続で参照中。検索メモリー・リスト・調査メモだけをCompanyMaster側へ保存します。" : "Queria CLIを使わず、組み込みDuckDBがDuckLakeをREAD_ONLYで直接ATTACH。国税庁法人番号＋gBizINFOを法人番号でJOINし、ローカルDuckDBへ高速キャッシュ。"}>
                <div className="button-row wrap"><button className="primary" onClick={syncDuckDb} disabled={busy === "duckdb"}>{busy === "duckdb" ? <Loader2 className="spin" size={15}/> : <RefreshCw size={15}/>}DuckDB同期</button><button className="ghost" onClick={importCompanies}><Upload size={15}/>DuckDB/Parquet読込</button><button className="ghost" onClick={importTaxonomy}><Upload size={15}/>産業分類</button></div>
              </ConnectionCard>
              <ConnectionCard icon={<CloudSalesforce />} title="Salesforce" status={salesforce?.connected ? "接続済み" : "未接続"} ok={!!salesforce?.connected} description="External Client App + Authorization Code/PKCE。法人番号を外部IDにしてBulk API 2.0でUpsert。">
                <SalesforceConnect connected={!!salesforce?.connected} onMessage={setMessage} onRefresh={refreshStatus} />
                {salesforce?.connected && <div className="sf-form sf-mapping">
                  <input value={sfObjectName} onChange={(e) => setSfObjectName(e.target.value)} placeholder="Object API名: Account" />
                  <input value={sfExternalId} onChange={(e) => setSfExternalId(e.target.value)} placeholder="外部ID項目: CorporateNumber__c" />
                  <textarea value={sfMappingText} onChange={(e) => setSfMappingText(e.target.value)} rows={5} aria-label="Salesforce項目マッピング" />
                  <small>CompanyMaster項目=Salesforce API項目（例: phone=Phone）</small>
                  {salesforceJob && <div className="sf-job"><span>{salesforceJob.job_id} / {salesforceJob.state}</span><span>処理 {fmt.format(salesforceJob.number_records_processed)}・失敗 {fmt.format(salesforceJob.number_records_failed)}</span><div className="button-row"><button className="ghost tiny" onClick={refreshSalesforceJob}>状態更新</button><button className="ghost tiny" onClick={retrySalesforceFailed} disabled={!!busy || salesforceJob.number_records_failed === 0}>失敗行を再送</button></div></div>}
                </div>}
              </ConnectionCard>
            </div>
            <div className="local-db"><Database size={16}/><div><strong>ローカルDB</strong><span>{data?.db_path ?? "初期化中"}</span></div></div>
          </section>
        )}
      </main>
    </div>
  );
}

function ConnectionCard({ icon, title, status, ok, description, children }: { icon: ReactNode; title: string; status: string; ok: boolean; description: string; children: ReactNode }) {
  return <article className="connection-card"><div className="connection-top"><div className="connection-icon">{icon}</div><span className={ok ? "status ok" : "status"}>{ok && <CheckCircle2 size={13}/>} {status}</span></div><h3>{title}</h3><p>{description}</p><div className="connection-actions">{children}</div></article>;
}

function CloudSalesforce() {
  return <span style={{ fontWeight: 800, fontSize: 13 }}>SF</span>;
}

function SalesforceConnect({ connected, onMessage, onRefresh }: { connected: boolean; onMessage: (s: string) => void; onRefresh: () => Promise<void> }) {
  const [loginUrl, setLoginUrl] = useState("https://login.salesforce.com");
  const [clientId, setClientId] = useState("");
  const [busy, setBusy] = useState(false);
  if (connected) return <span className="muted">接続情報はWindows資格情報ストアに保存されます。</span>;
  return <div className="sf-form"><input value={loginUrl} onChange={(e) => setLoginUrl(e.target.value)} placeholder="Salesforce Login URL"/><input value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="External Client App Client ID"/><button className="primary" disabled={!clientId || busy} onClick={async () => {setBusy(true); try { const {auth_url}=await api.salesforceLogin(loginUrl, clientId); await openUrl(auth_url); onMessage("Salesforceの認証をブラウザで完了してください。"); setTimeout(() => void onRefresh(), 3500); } catch(e){ onMessage(String(e)); } finally {setBusy(false);} }}>{busy ? <Loader2 className="spin" size={15}/> : <LogIn size={15}/>}接続</button></div>;
}

export default App;
