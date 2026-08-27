import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Building2,
  Check,
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
  Menu,
  PanelLeftClose,
  Phone,
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
  PublicEnrichmentStatus,
  ResearchReport,
  SalesforceFieldMapping,
  SalesforceJobStatus,
  SalesforceStatus,
  SavedSearch,
  SearchPlan,
  SearchResult,
} from "./types";

type View = "search" | "research" | "connections";
type SortField = NonNullable<SearchPlan["sort_by"]>;

const PAGE_SIZE = 100;
// Match the Rust-side safety ceiling so a 5.8M-row national snapshot can be
// listed or exported in full. XLSX keeps its stricter worksheet limit server-side.
const MAX_RESULTS = 10_000_000;
const fmt = new Intl.NumberFormat("ja-JP");

const emptyPlan = (): SearchPlan => ({
  text: null,
  prefectures: [],
  cities: [],
  industry_codes: [],
  industry_terms: [],
  company_kinds: [],
  keyword_any: [],
  keyword_all: [],
  website_required: null,
  phone_required: null,
  sort_by: "relevance",
  sort_direction: "asc",
  limit: 100_000,
});

const prefectures = [
  "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
  "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
  "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
  "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
  "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
];

const companyKinds = [
  { value: "301", label: "株式会社" },
  { value: "302", label: "有限会社" },
  { value: "303", label: "合名会社" },
  { value: "304", label: "合資会社" },
  { value: "305", label: "合同会社" },
];

const splitValues = (value: string) =>
  value.split(/[、,\n]/).map((item) => item.trim()).filter(Boolean);

const toOptionalNumber = (value: string) => {
  if (!value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

function compactNumber(value?: number | null) {
  if (value == null) return "—";
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(1)}億円`;
  if (value >= 10_000) return `${fmt.format(Math.round(value / 10_000))}万円`;
  return `${fmt.format(value)}円`;
}

function formatKind(value?: string | null) {
  return companyKinds.find((kind) => kind.value === value)?.label ?? value ?? "—";
}

function toggle(values: string[], value: string) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function safeHttpUrl(value: string) {
  const parsed = new URL(value);
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("HTTP(S)以外のURLは開けません");
  }
  if (parsed.username || parsed.password) {
    throw new Error("認証情報を含むURLは開けません");
  }
  return parsed.toString();
}

function App() {
  const [view, setView] = useState<View>("search");
  const [plan, setPlan] = useState<SearchPlan>(emptyPlan);
  const [appliedPlan, setAppliedPlan] = useState<SearchPlan | null>(null);
  const [query, setQuery] = useState("");
  const [aiPrompt, setAiPrompt] = useState("");
  const [aiOpen, setAiOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(() => window.matchMedia("(min-width: 901px)").matches);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [detail, setDetail] = useState<Company | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [research, setResearch] = useState<ResearchReport | null>(null);
  const [data, setData] = useState<DataStatus | null>(null);
  const [publicEnrichment, setPublicEnrichment] = useState<PublicEnrichmentStatus | null>(null);
  const [codex, setCodex] = useState<CodexStatus | null>(null);
  const [salesforce, setSalesforce] = useState<SalesforceStatus | null>(null);
  const [salesforceJob, setSalesforceJob] = useState<SalesforceJobStatus | null>(null);
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [listName, setListName] = useState("営業候補");
  const [enrichmentSheet, setEnrichmentSheet] = useState("");
  const [enrichmentReplace, setEnrichmentReplace] = useState(false);
  const [sfObjectName, setSfObjectName] = useState("Account");
  const [sfExternalId, setSfExternalId] = useState("CorporateNumber__c");
  const [sfMappingText, setSfMappingText] = useState(
    "name=Name\ncorporate_number=CorporateNumber__c\nwebsite=Website\nphone=Phone\nprefecture=BillingState\ncity=BillingCity\naddress=BillingStreet\nindustry=Industry\nemployees=NumberOfEmployees\nbusiness_summary=Description",
  );
  const searchGeneration = useRef(0);
  const pageCheckboxRef = useRef<HTMLInputElement>(null);

  const currentPageSize = result?.page_size || PAGE_SIZE;
  const totalPages = result ? Math.max(1, Math.ceil(result.total / currentPageSize)) : 1;
  const currentPage = result?.page ?? 1;
  const visibleRows = result?.rows ?? [];
  const allPageChecked = visibleRows.length > 0 && visibleRows.every((row) => checked.has(row.corporate_number));
  const somePageChecked = visibleRows.some((row) => checked.has(row.corporate_number));
  const operationPlan = appliedPlan ?? plan;
  const draftPlan = useMemo<SearchPlan>(
    () => ({ ...plan, text: query.trim() || null }),
    [plan, query],
  );
  const hasUnappliedChanges = result != null && appliedPlan != null
    && JSON.stringify(draftPlan) !== JSON.stringify(appliedPlan);

  const activeFilters = useMemo(() => {
    const labels: Array<{ key: string; label: string }> = [];
    plan.prefectures.forEach((value) => labels.push({ key: `prefecture:${value}`, label: value }));
    plan.cities.forEach((value) => labels.push({ key: `city:${value}`, label: `市区町村: ${value}` }));
    plan.industry_codes.forEach((value) => labels.push({ key: `industry-code:${value}`, label: `業種 ${value}` }));
    plan.industry_terms.forEach((value) => labels.push({ key: `industry-term:${value}`, label: value }));
    plan.company_kinds.forEach((value) => labels.push({ key: `kind:${value}`, label: formatKind(value) }));
    if (plan.min_employees != null || plan.max_employees != null) labels.push({ key: "employees", label: `従業員 ${plan.min_employees ?? 0}–${plan.max_employees ?? "上限なし"}` });
    if (plan.min_capital != null || plan.max_capital != null) labels.push({ key: "capital", label: `資本金 ${compactNumber(plan.min_capital)}–${compactNumber(plan.max_capital)}` });
    if (plan.established_from != null || plan.established_to != null) labels.push({ key: "established", label: `設立 ${plan.established_from ?? "以前"}–${plan.established_to ?? "以後"}` });
    if (plan.website_required != null) labels.push({ key: "website", label: plan.website_required ? "Webあり" : "Webなし" });
    if (plan.phone_required != null) labels.push({ key: "phone", label: plan.phone_required ? "電話あり" : "電話なし" });
    plan.keyword_any.forEach((value) => labels.push({ key: `keyword-any:${value}`, label: `いずれか: ${value}` }));
    plan.keyword_all.forEach((value) => labels.push({ key: `keyword-all:${value}`, label: `すべて: ${value}` }));
    return labels;
  }, [plan]);

  useEffect(() => {
    if (pageCheckboxRef.current) {
      pageCheckboxRef.current.indeterminate = somePageChecked && !allPageChecked;
    }
  }, [allPageChecked, somePageChecked]);

  useEffect(() => {
    void refreshStatus();
  }, []);

  async function refreshStatus() {
    const [dataResult, codexResult, salesforceResult, searchesResult, enrichmentResult] = await Promise.allSettled([
      api.bootstrap(),
      api.codexStatus(),
      api.salesforceStatus(),
      api.recentSearches(20),
      api.publicEnrichmentStatus(),
    ]);
    if (dataResult.status === "fulfilled") setData(dataResult.value);
    else setNotice(`データ状態を取得できませんでした: ${String(dataResult.reason)}`);
    if (codexResult.status === "fulfilled") setCodex(codexResult.value);
    if (salesforceResult.status === "fulfilled") setSalesforce(salesforceResult.value);
    if (searchesResult.status === "fulfilled") setSavedSearches(searchesResult.value);
    if (enrichmentResult.status === "fulfilled") setPublicEnrichment(enrichmentResult.value);
  }

  async function runSearch(targetPlan: SearchPlan, page = 1) {
    const generation = ++searchGeneration.current;
    setBusy("search");
    setNotice("");
    try {
      const found = await api.search(targetPlan, page, PAGE_SIZE);
      if (generation !== searchGeneration.current) return;
      setPlan(targetPlan);
      setQuery(targetPlan.text?.trim() ?? "");
      setAppliedPlan(targetPlan);
      setResult(found);
      setDetail((current) => found.rows.find((row) => row.corporate_number === current?.corporate_number)
        ?? (window.matchMedia("(min-width: 1201px)").matches ? found.rows[0] ?? null : null));
      setChecked(new Set());
      if (found.warnings?.length) setNotice(found.warnings.join(" "));
    } catch (error) {
      if (generation === searchGeneration.current) setNotice(`検索できませんでした: ${String(error)}`);
    } finally {
      if (generation === searchGeneration.current) setBusy(null);
    }
  }

  async function directSearch() {
    const next = { ...draftPlan, sort_by: query.trim() ? "relevance" as const : plan.sort_by };
    await runSearch(next, 1);
  }

  async function aiSearch() {
    if (!aiPrompt.trim()) return;
    const generation = ++searchGeneration.current;
    setBusy("plan");
    setNotice("");
    try {
      const next = await api.planSearch(aiPrompt.trim());
      if (generation !== searchGeneration.current) return;
      const normalized: SearchPlan = { ...emptyPlan(), ...next, limit: Math.min(next.limit || 100_000, MAX_RESULTS) };
      setAiOpen(false);
      await api.saveSearch(aiPrompt.trim().slice(0, 48), aiPrompt.trim(), normalized);
      if (generation !== searchGeneration.current) return;
      api.recentSearches(20).then(setSavedSearches).catch(() => undefined);
      await runSearch(normalized, 1);
    } catch (error) {
      if (generation === searchGeneration.current) {
        setNotice(`AI条件を作成できませんでした: ${String(error)}`);
      }
    } finally {
      if (generation === searchGeneration.current) setBusy(null);
    }
  }

  async function saveCurrentSearch() {
    const name = query.trim() || activeFilters.map((filter) => filter.label).slice(0, 3).join("・") || "条件なしの検索";
    try {
      await api.saveSearch(name.slice(0, 48), query.trim(), draftPlan);
      setSavedSearches(await api.recentSearches(20));
      setNotice(`「${name.slice(0, 48)}」を保存しました。`);
    } catch (error) {
      setNotice(`検索条件を保存できませんでした: ${String(error)}`);
    }
  }

  async function loadSavedSearch(id: string) {
    const savedSearch = savedSearches.find((item) => item.id === id);
    if (!savedSearch) return;
    await runSearch({ ...emptyPlan(), ...savedSearch.plan }, 1);
  }

  function removeFilter(key: string) {
    const [type, ...rest] = key.split(":");
    const value = rest.join(":");
    setPlan((current) => {
      if (type === "prefecture") return { ...current, prefectures: current.prefectures.filter((item) => item !== value) };
      if (type === "city") return { ...current, cities: current.cities.filter((item) => item !== value) };
      if (type === "industry-code") return { ...current, industry_codes: current.industry_codes.filter((item) => item !== value) };
      if (type === "industry-term") return { ...current, industry_terms: current.industry_terms.filter((item) => item !== value) };
      if (type === "kind") return { ...current, company_kinds: current.company_kinds.filter((item) => item !== value) };
      if (type === "keyword-any") return { ...current, keyword_any: current.keyword_any.filter((item) => item !== value) };
      if (type === "keyword-all") return { ...current, keyword_all: current.keyword_all.filter((item) => item !== value) };
      if (type === "employees") return { ...current, min_employees: null, max_employees: null };
      if (type === "capital") return { ...current, min_capital: null, max_capital: null };
      if (type === "established") return { ...current, established_from: null, established_to: null };
      if (type === "website") return { ...current, website_required: null };
      if (type === "phone") return { ...current, phone_required: null };
      return current;
    });
  }

  async function changeSort(field: SortField) {
    const same = plan.sort_by === field;
    const direction = same && plan.sort_direction === "asc" ? "desc" : "asc";
    await runSearch({ ...draftPlan, sort_by: field, sort_direction: direction }, 1);
  }

  function togglePageSelection() {
    setChecked((current) => {
      const next = new Set(current);
      if (allPageChecked) visibleRows.forEach((row) => next.delete(row.corporate_number));
      else visibleRows.forEach((row) => next.add(row.corporate_number));
      return next;
    });
  }

  function toggleCompanySelection(corporateNumber: string) {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(corporateNumber)) next.delete(corporateNumber);
      else next.add(corporateNumber);
      return next;
    });
  }

  async function addToList(scope: "selected" | "all") {
    if (!result) return;
    if (scope === "selected" && checked.size === 0) return;
    if (scope === "all" && hasUnappliedChanges) {
      setNotice("変更した条件を検索へ適用してから、一致企業をリストへ追加してください。");
      return;
    }
    setBusy("list");
    try {
      const count = scope === "selected"
        ? await api.addToList(listName, Array.from(checked))
        : await api.addSearchToList(listName, operationPlan);
      setNotice(`「${listName}」に${fmt.format(count)}件を追加しました。`);
    } catch (error) {
      setNotice(`リストへ追加できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function exportMatches(format: "csv" | "xlsx") {
    if (hasUnappliedChanges) {
      setNotice("変更した条件を検索へ適用してから、結果を出力してください。");
      return;
    }
    const path = await save({
      defaultPath: `queria-companies.${format}`,
      filters: [{ name: format === "csv" ? "CSV" : "Excel", extensions: [format] }],
    });
    if (!path) return;
    setBusy("export");
    try {
      const count = format === "csv" ? await api.exportCsv(operationPlan, path) : await api.exportXlsx(operationPlan, path);
      setNotice(`検索条件に一致する${fmt.format(count)}件を${format.toUpperCase()}へ出力しました。`);
    } catch (error) {
      setNotice(`出力できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function openExternal(url?: string | null) {
    if (!url) return;
    try {
      await openUrl(safeHttpUrl(url));
    } catch (error) {
      setNotice(`URLを開けませんでした: ${String(error)}`);
    }
  }

  async function loginCodex() {
    setBusy("codex-login");
    try {
      const { auth_url } = await api.codexLogin();
      await openUrl(safeHttpUrl(auth_url));
      setNotice("ブラウザでログインを完了してから、接続状態を更新してください。");
      window.setTimeout(() => void refreshStatus(), 3500);
    } catch (error) {
      setNotice(String(error));
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
      setNotice(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function deepResearch() {
    if (!detail) return;
    setBusy("research");
    setResearch(null);
    setView("research");
    try {
      const report = await api.researchCompany(
        detail,
        "公式サイトと公的な公開情報を優先し、事業内容・提供サービス・顧客・強み・日本標準産業分類を根拠URL付きで調べる。",
      );
      setResearch(report);
      await refreshStatus();
    } catch (error) {
      setNotice(`企業調査を完了できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function collectPhone() {
    if (!detail?.website) return;
    setBusy("phone");
    try {
      const value = await api.collectCompanyPhone(detail);
      if (!value.phone) {
        setNotice("公式サイトで公開電話番号を確認できませんでした。");
        return;
      }
      const updated = { ...detail, phone: value.phone };
      setDetail(updated);
      setResult((current) => current ? {
        ...current,
        rows: current.rows.map((row) => row.corporate_number === detail.corporate_number ? updated : row),
      } : current);
      setNotice(`公式サイトから電話番号 ${value.phone} を取得しました。`);
    } catch (error) {
      setNotice(`電話番号を取得できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function preparePublicEnrichment() {
    const file = await open({ multiple: false, filters: [{ name: "企業リスト", extensions: ["csv", "xlsx", "xlsm"] }] });
    if (!file || Array.isArray(file)) return;
    setBusy("public-prepare");
    setNotice("");
    try {
      const value = await api.publicEnrichmentPrepare(file, enrichmentSheet.trim() || null, enrichmentReplace);
      setPublicEnrichment(value.status);
      setNotice(`企業リスト ${fmt.format(value.status.companies)}件を検証用ステージへ準備しました。`);
    } catch (error) {
      setNotice(`企業リストを準備できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function makePublicAssignment() {
    const path = await save({ defaultPath: "corporate-number-assignment.csv", filters: [{ name: "CSV", extensions: ["csv"] }] });
    if (!path) return;
    setBusy("public-assignment");
    setNotice("");
    try {
      const value = await api.publicEnrichmentMakeAssignment(path, 10_000);
      setPublicEnrichment(value.status);
      setNotice(`法人番号付与用CSVを出力しました: ${path}`);
    } catch (error) {
      setNotice(`法人番号付与用CSVを出力できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function runPublicEnrichment() {
    const directory = await open({ directory: true, multiple: false });
    if (!directory || Array.isArray(directory)) return;
    setBusy("public-run");
    setNotice("");
    try {
      const value = await api.publicEnrichmentRunAll(directory);
      setPublicEnrichment(value.status);
      setNotice(`公開情報を証拠DBへ統合し、runtime/indexを同一世代で公開しました。法人番号確定 ${fmt.format(value.status.accepted_matches)}件、要確認 ${fmt.format(value.status.review_matches)}件。`);
      await refreshStatus();
    } catch (error) {
      setNotice(`公開情報を統合できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function importCompanies() {
    const file = await open({ multiple: false, filters: [{ name: "Company data", extensions: ["duckdb", "db", "parquet", "csv", "json", "jsonl"] }] });
    if (!file || Array.isArray(file)) return;
    setBusy("import");
    try {
      const count = await api.importFile(file);
      setNotice(`${fmt.format(count)}件を取り込みました。`);
      await refreshStatus();
    } catch (error) {
      setNotice(String(error));
    } finally {
      setBusy(null);
    }
  }

  async function syncDuckDb() {
    setBusy("sync");
    try {
      const value = await api.syncDuckDb();
      setNotice(`${fmt.format(value.imported)}件を同期しました（DuckDB ${value.duckdb_version}）。`);
      await refreshStatus();
    } catch (error) {
      setNotice(String(error));
    } finally {
      setBusy(null);
    }
  }

  function salesforceMapping(): SalesforceFieldMapping[] {
    return sfMappingText.split(/\r?\n/).map((line) => {
      const [source, ...target] = line.split("=");
      return { source: source?.trim(), target: target.join("=").trim() };
    }).filter((field) => field.source && field.target);
  }

  async function sendToSalesforce() {
    if (!salesforce?.connected || !result) return;
    if (checked.size === 0 && hasUnappliedChanges) {
      setNotice("変更した条件を検索へ適用してから、一致企業をSalesforceへ送信してください。");
      return;
    }
    const snapshotName = `${listName}-${new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14)}`;
    setBusy("salesforce");
    try {
      if (checked.size) await api.addToList(snapshotName, Array.from(checked));
      else await api.addSearchToList(snapshotName, operationPlan);
      const response = await api.salesforceUpsertList(snapshotName, sfObjectName, sfExternalId, salesforceMapping());
      setSalesforceJob({
        job_id: response.job_id,
        state: "UploadComplete",
        number_records_processed: 0,
        number_records_failed: 0,
        number_records_total: response.accepted,
        error_message: null,
      });
      setNotice(`${checked.size ? "選択した" : "検索条件に一致する"}${fmt.format(response.accepted)}件をSalesforceへ送信しました。`);
    } catch (error) {
      setNotice(`Salesforceへ送信できませんでした: ${String(error)}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className={`app-shell ${mobileNavOpen ? "nav-open" : ""}`}>
      <aside className="app-sidebar" aria-label="メインナビゲーション">
        <div className="brand">
          <span className="brand-mark"><Building2 size={20} /></span>
          <span><strong>CompanyMaster G</strong><small>情報通信業 37〜41</small></span>
        </div>
        <nav>
          <button className={view === "search" ? "active" : ""} onClick={() => { setView("search"); setMobileNavOpen(false); }}><Search size={18} />企業検索</button>
          <button className={view === "research" ? "active" : ""} onClick={() => { setView("research"); setMobileNavOpen(false); }}><FileSearch size={18} />企業調査</button>
          <button className={view === "connections" ? "active" : ""} onClick={() => { setView("connections"); setMobileNavOpen(false); }}><Settings2 size={18} />データと接続</button>
        </nav>
        <div className="sidebar-summary">
          <span>収録企業</span>
          <strong>{data ? fmt.format(data.company_count) : "—"}</strong>
          <small>{data?.runtime_attached ? "統合ランタイム接続中" : "ローカルデータ"}</small>
        </div>
        <div className="sidebar-health">
          <span className={`health-dot ${data?.search_index_available ? "ok" : ""}`} />
          <span><strong>{data?.search_index_available ? "高速索引 利用可能" : "DuckDB検索"}</strong><small>{data?.search_index_status ?? "状態を確認中"}</small></span>
        </div>
      </aside>

      <main className="app-main">
        <header className="app-topbar">
          <button className="icon-button mobile-menu" aria-label="メニューを開く" onClick={() => setMobileNavOpen((openState) => !openState)}><Menu size={20} /></button>
          <div className="topbar-title">
            <strong>{view === "search" ? "企業検索" : view === "research" ? "企業調査" : "データと接続"}</strong>
            {view === "search" && result && <span>{fmt.format(result.total)}件・{result.elapsed_ms}ms・{result.engine || "DuckDB"}</span>}
          </div>
          <div className="topbar-actions">
            <span className={`connection-pill ${codex?.authenticated ? "connected" : ""}`}><span />{codex?.authenticated ? "ChatGPT 接続済み" : "ChatGPT 未接続"}</span>
            <button className="button quiet" onClick={() => void refreshStatus()}><RefreshCw size={16} />状態を更新</button>
          </div>
        </header>

        {notice && <div className="notice" role="status" aria-live="polite"><span>{notice}</span><button aria-label="通知を閉じる" onClick={() => setNotice("")}><X size={16} /></button></div>}

        {view === "search" && (
          <section className="search-workbench">
            <div className="search-command">
              <form onSubmit={(event) => { event.preventDefault(); void directSearch(); }} className="global-search" role="search">
                <Search size={20} aria-hidden="true" />
                <label className="sr-only" htmlFor="global-query">企業名、法人番号、住所、電話番号、URLを検索</label>
                <input id="global-query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="企業名・法人番号・住所・電話番号・URLを横断検索" autoComplete="off" />
                {query && <button type="button" className="clear-query" aria-label="検索語を消去" onClick={() => setQuery("")}><X size={16} /></button>}
                <button type="submit" className="button primary" disabled={busy === "search"}>{busy === "search" ? <Loader2 className="spin" size={17} /> : <Search size={17} />}検索</button>
              </form>
              <div className="command-actions">
                <button className={`button ${aiOpen ? "selected" : "quiet"}`} onClick={() => setAiOpen((openState) => !openState)}><Sparkles size={17} />文章で条件指定</button>
                <select aria-label="保存した検索" defaultValue="" onChange={(event) => { void loadSavedSearch(event.target.value); event.currentTarget.value = ""; }}>
                  <option value="">保存した検索</option>
                  {savedSearches.map((savedSearch) => <option key={savedSearch.id} value={savedSearch.id}>{savedSearch.name}</option>)}
                </select>
                <button className="button quiet" onClick={() => void saveCurrentSearch()} disabled={!result}><Check size={17} />条件を保存</button>
              </div>
            </div>

            {aiOpen && (
              <div className="ai-condition" role="region" aria-label="文章による検索条件の作成">
                <span className="ai-icon"><Bot size={20} /></span>
                <div>
                  <strong>探したい企業をそのまま書く</strong>
                  <small>AIが文章を検索条件へ変換します。実行前後に左の条件欄で確認・修正できます。</small>
                </div>
                <textarea value={aiPrompt} onChange={(event) => setAiPrompt(event.target.value)} placeholder="例：神奈川県の情報サービス企業。従業員50名以上で、公式サイトと電話番号がある会社" rows={2} />
                <button className="button primary" disabled={!aiPrompt.trim() || busy === "plan"} onClick={() => void aiSearch()}>{busy === "plan" ? <Loader2 className="spin" size={17} /> : <Sparkles size={17} />}条件を作成して検索</button>
              </div>
            )}

            <div className="active-filter-bar">
              <button className="button filter-toggle" onClick={() => setFilterOpen((openState) => !openState)} aria-expanded={filterOpen}><Filter size={17} />絞り込み{activeFilters.length > 0 && <b>{activeFilters.length}</b>}</button>
              <div className="filter-chips">
                {activeFilters.length === 0 ? <span>全国・全業種</span> : activeFilters.map((filter) => (
                  <button key={filter.key} className="filter-chip" onClick={() => removeFilter(filter.key)}>{filter.label}<X size={14} /></button>
                ))}
              </div>
              {hasUnappliedChanges && <span className="unapplied-pill">未適用の変更</span>}
              {activeFilters.length > 0 && <button className="text-button" onClick={() => setPlan((current) => ({ ...emptyPlan(), text: current.text }))}>絞り込みをクリア</button>}
            </div>

            <div className={`workbench-grid ${filterOpen ? "with-filters" : ""}`}>
              {filterOpen && (
                <>
                  <button className="filter-scrim" aria-label="絞り込みを閉じる" onClick={() => setFilterOpen(false)} />
                  <FilterPanel plan={plan} setPlan={setPlan} onApply={(nextPlan) => void runSearch({ ...nextPlan, text: query.trim() || null }, 1)} busy={busy === "search"} onClose={() => setFilterOpen(false)} />
                </>
              )}

              <div className="results-panel">
                <div className="results-toolbar">
                  <div>
                    <strong>{result ? `${fmt.format(result.total)}件` : "検索結果"}</strong>
                    <span>{result ? `1ページ${currentPageSize}件・${result.elapsed_ms}ms` : "条件を指定して企業を検索します"}</span>
                  </div>
                  <div className="selection-actions">
                    <input value={listName} onChange={(event) => setListName(event.target.value)} aria-label="リスト名" />
                    {checked.size > 0 ? (
                      <button className="button secondary" onClick={() => void addToList("selected")} disabled={busy === "list"}><ListPlus size={16} />選択した{checked.size}社を追加</button>
                    ) : (
                      <button className="button secondary" onClick={() => void addToList("all")} disabled={!result || busy === "list" || hasUnappliedChanges} title={hasUnappliedChanges ? "変更した条件を先に検索してください" : "画面に適用済みの条件を使用します"}><ListPlus size={16} />一致企業を追加（上限{fmt.format(operationPlan.limit)}件）</button>
                    )}
                    <div className="export-group" aria-label="検索結果を出力">
                      <button className="button quiet" onClick={() => void exportMatches("csv")} disabled={!result || busy === "export" || hasUnappliedChanges} title={hasUnappliedChanges ? "変更した条件を先に検索してください" : undefined}><Download size={16} />CSV</button>
                      <button className="button quiet" onClick={() => void exportMatches("xlsx")} disabled={!result || busy === "export" || hasUnappliedChanges} title={hasUnappliedChanges ? "変更した条件を先に検索してください" : undefined}>Excel</button>
                    </div>
                  </div>
                </div>

                <div className="table-wrap" aria-busy={busy === "search"}>
                  <table>
                    <caption className="sr-only">企業検索結果。各行を選ぶと詳細が開きます。</caption>
                    <thead>
                      <tr>
                        <th className="check-column"><input ref={pageCheckboxRef} type="checkbox" aria-label="このページをすべて選択" checked={allPageChecked} onChange={togglePageSelection} /></th>
                        <SortableHeader label="企業名" field="name" plan={plan} onSort={changeSort} />
                        <th>所在地</th>
                        <th>業種</th>
                        <SortableHeader label="従業員" field="employees" plan={plan} onSort={changeSort} />
                        <SortableHeader label="資本金" field="capital" plan={plan} onSort={changeSort} />
                        <th>連絡先</th>
                      </tr>
                    </thead>
                    <tbody>
                      {busy === "search" && !result ? <LoadingRows /> : visibleRows.map((company) => (
                        <tr key={company.corporate_number} className={detail?.corporate_number === company.corporate_number ? "active" : ""} onClick={() => setDetail(company)}>
                          <td className="check-column" onClick={(event) => event.stopPropagation()}><input type="checkbox" aria-label={`${company.name}を選択`} checked={checked.has(company.corporate_number)} onChange={() => toggleCompanySelection(company.corporate_number)} /></td>
                          <td><button className="company-button" onClick={() => setDetail(company)}><strong>{company.name}</strong><small>{company.corporate_number}・{formatKind(company.kind)}</small></button></td>
                          <td><span>{company.prefecture || "—"} {company.city || ""}</span><small title={company.address ?? undefined}>{company.address || "住所情報なし"}</small></td>
                          <td><span>{company.industry_name || company.inferred_industry_name || "—"}</span>{company.inferred_industry_name && !company.industry_name && <small className="ai-label">AI推定 {Math.round((company.inferred_industry_confidence ?? 0) * 100)}%</small>}</td>
                          <td className="number-cell">{company.employees == null ? "—" : `${fmt.format(company.employees)}名`}</td>
                          <td className="number-cell">{compactNumber(company.capital)}</td>
                          <td><div className="contact-status"><span className={company.website ? "available" : ""}>Web</span><span className={company.phone ? "available" : ""}>電話</span></div></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!busy && !result && <EmptyState icon={<Search size={30} />} title="企業を検索してみましょう" text="企業名・法人番号なら上の検索欄へ、営業リストなら左の絞り込みへ入力します。" />}
                  {!busy && result && visibleRows.length === 0 && <EmptyState icon={<Filter size={30} />} title="一致する企業がありません" text="条件を1つずつ外すか、表記を短くして再検索してください。" />}
                  {busy === "search" && result && <div className="table-loading"><Loader2 className="spin" size={20} />検索中…</div>}
                </div>

                <div className="pagination" aria-label="検索結果のページ移動">
                  <span>{result ? `${fmt.format(Math.min((currentPage - 1) * currentPageSize + 1, result.total))}–${fmt.format(Math.min(currentPage * currentPageSize, result.total))} / ${fmt.format(result.total)}` : "0件"}</span>
                  <div>
                    <button className="icon-button" aria-label="前のページ" title={hasUnappliedChanges ? "変更した条件を先に検索してください" : undefined} disabled={!result || currentPage <= 1 || busy === "search" || hasUnappliedChanges} onClick={() => void runSearch(operationPlan, currentPage - 1)}><ChevronLeft size={18} /></button>
                    <span>{result ? `${currentPage} / ${totalPages}` : "—"}</span>
                    <button className="icon-button" aria-label="次のページ" title={hasUnappliedChanges ? "変更した条件を先に検索してください" : undefined} disabled={!result || currentPage >= totalPages || busy === "search" || hasUnappliedChanges} onClick={() => void runSearch(operationPlan, currentPage + 1)}><ChevronRight size={18} /></button>
                  </div>
                </div>
              </div>

              <DetailPanel company={detail} busy={busy} engine={result?.engine} onClose={() => setDetail(null)} onOpenUrl={openExternal} onResearch={deepResearch} onCollectPhone={collectPhone} />
            </div>
          </section>
        )}

        {view === "research" && (
          <section className="page-section">
            <PageHeading eyebrow="AIによる公開情報調査" title="企業調査" description="検索結果から企業を選び、公式サイトと公的情報を優先して調査します。" />
            {busy === "research" && <EmptyState icon={<Loader2 className="spin" size={30} />} title="公開情報を調査中" text="根拠URLを確認しながらレポートを作成しています。" />}
            {!busy && !research && <EmptyState icon={<FileSearch size={30} />} title="調査対象を選んでください" text="企業検索の右側にある「この企業を調査」から開始できます。" />}
            {research && (
              <div className="research-layout">
                <article className="report-card">
                  <div className="report-title"><span><small>調査対象</small><h2>{research.company_name}</h2></span><span className="status-badge ok"><CheckCircle2 size={14} />完了</span></div>
                  <h3>要約</h3><p>{research.summary}</p>
                  {research.industry_guess && <div className="industry-guess"><small>推定業種</small><strong>{research.industry_guess.code} {research.industry_guess.name}</strong><span>確信度 {Math.round((research.industry_guess.confidence ?? 0) * 100)}%</span><p>{research.industry_guess.rationale}</p></div>}
                  <h3>確認できたこと</h3><ul>{research.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul>
                </article>
                <aside className="report-card sources-card">
                  <h3>根拠となる公開情報</h3>
                  {research.sources.map((source) => <button key={source.url} onClick={() => void openExternal(source.url)}><ExternalLink size={16} /><span><strong>{source.title || source.url}</strong><small>{source.note || source.url}</small></span></button>)}
                  <details><summary>調査ログ</summary><pre>{research.transcript}</pre></details>
                </aside>
              </div>
            )}
          </section>
        )}

        {view === "connections" && (
          <section className="page-section">
            <PageHeading eyebrow="データの鮮度と外部連携" title="データと接続" description="現在使われているデータ、検索索引、ChatGPT、Salesforceの状態を確認します。" />
            <div className="connection-grid">
              <ConnectionCard icon={<Database size={21} />} title="企業マスター" status={data?.runtime_attached ? "統合ランタイム" : "ローカル"} ok={Boolean(data?.company_count)}>
                <dl className="metric-list">
                  <div><dt>企業</dt><dd>{data ? fmt.format(data.company_count) : "—"}</dd></div>
                  <div><dt>住所あり</dt><dd>{data ? fmt.format(data.address_count) : "—"}</dd></div>
                  <div><dt>Webあり</dt><dd>{data ? fmt.format(data.website_count) : "—"}</dd></div>
                  <div><dt>電話あり</dt><dd>{data ? fmt.format(data.phone_count) : "—"}</dd></div>
                </dl>
                {data?.runtime_attached ? <p className="card-note">統合ランタイムは外部の生成処理で更新されます。この画面からの同期・上書きは行いません。</p> : <div className="card-actions"><button className="button secondary" disabled={busy === "sync"} onClick={() => void syncDuckDb()}><RefreshCw size={16} />DuckDBを同期</button><button className="button quiet" disabled={busy === "import"} onClick={() => void importCompanies()}><Upload size={16} />ファイル取込</button></div>}
              </ConnectionCard>

              <ConnectionCard icon={<Search size={21} />} title="全文検索索引" status={data?.search_index_available ? "利用可能" : "フォールバック"} ok={Boolean(data?.search_index_available)}>
                <dl className="metric-list"><div><dt>状態</dt><dd>{data?.search_index_status ?? "未確認"}</dd></div><div><dt>収録行</dt><dd>{data?.search_index_row_count != null ? fmt.format(data.search_index_row_count) : "—"}</dd></div><div><dt>検索経路</dt><dd>{data?.search_index_available ? "SQLite FTS5" : "DuckDB"}</dd></div></dl>
                <p className="card-note">索引が互換・最新の場合だけ高速検索を使用し、使えない条件は警告付きでDuckDBへ切り替えます。</p>
              </ConnectionCard>

              <ConnectionCard icon={<FileSearch size={21} />} title="公開情報の照合・統合" status={publicEnrichment?.available ? (publicEnrichment.publish_available ? (publicEnrichment.companies > 0 ? `${fmt.format(publicEnrichment.companies)}社準備済み` : "公開可能") : "統合CLI要確認") : "Python 3.11+が必要"} ok={Boolean(publicEnrichment?.available && publicEnrichment.publish_available)}>
                <p className="card-note">企業リストを法人番号で照合し、検証用SQLiteをstagingとして保存します。確定結果だけを証拠DBへ取り込み、runtimeと検索索引を同一generationで再公開します。欠損値は推測しません。</p>
                <dl className="metric-list enrichment-metrics">
                  <div><dt>企業</dt><dd>{fmt.format(publicEnrichment?.companies ?? 0)}</dd></div>
                  <div><dt>法人番号確定</dt><dd>{fmt.format(publicEnrichment?.accepted_matches ?? 0)}</dd></div>
                  <div><dt>要確認</dt><dd>{fmt.format(publicEnrichment?.review_matches ?? 0)}</dd></div>
                  <div><dt>公開マスタ</dt><dd>{fmt.format(publicEnrichment?.public_master ?? 0)}</dd></div>
                </dl>
                <div className="enrichment-controls">
                  <label>Excelシート名（空欄なら先頭）<input value={enrichmentSheet} onChange={(event) => setEnrichmentSheet(event.target.value)} placeholder="任意" /></label>
                  <label className="check-row"><input type="checkbox" checked={enrichmentReplace} onChange={(event) => setEnrichmentReplace(event.target.checked)} /><span>検証用ステージを作り直す</span></label>
                </div>
                <div className="card-actions enrichment-actions">
                  <button className="button secondary" onClick={() => void preparePublicEnrichment()} disabled={Boolean(busy) || !publicEnrichment?.available}>{busy === "public-prepare" ? <Loader2 className="spin" size={16} /> : <Upload size={16} />}企業リスト</button>
                  <button className="button quiet" onClick={() => void makePublicAssignment()} disabled={Boolean(busy) || !publicEnrichment?.available || !publicEnrichment.companies}><Download size={16} />法人番号CSV</button>
                  <button className="button primary" onClick={() => void runPublicEnrichment()} disabled={Boolean(busy) || !publicEnrichment?.available || !publicEnrichment.publish_available || !publicEnrichment.companies}>{busy === "public-run" ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}検証して公開</button>
                </div>
                {publicEnrichment?.error && <small className="enrichment-error">{publicEnrichment.error}</small>}
              </ConnectionCard>

              <ConnectionCard icon={<Sparkles size={21} />} title="ChatGPT" status={codex?.authenticated ? "接続済み" : "未接続"} ok={Boolean(codex?.authenticated)}>
                <p>{codex?.authenticated ? `${codex.email ?? "ChatGPT"} / ${codex.model}` : "文章から検索条件を作り、企業の公開情報を調査するために使用します。"}</p>
                <div className="card-actions">{codex?.authenticated ? <button className="button quiet" onClick={() => void logoutCodex()} disabled={busy === "codex-logout"}><LogOut size={16} />ログアウト</button> : <button className="button primary" onClick={() => void loginCodex()} disabled={busy === "codex-login"}><LogIn size={16} />ChatGPTでログイン</button>}</div>
              </ConnectionCard>

              <ConnectionCard icon={<Send size={21} />} title="Salesforce" status={salesforce?.connected ? "接続済み" : "未接続"} ok={Boolean(salesforce?.connected)}>
                <p>{salesforce?.connected ? `${salesforce.username ?? "ユーザー"} / ${salesforce.instance_url ?? ""}` : "Salesforce CLIで接続すると、選択企業または現在の検索結果を一意なスナップショットとして送信できます。"}</p>
                {salesforce?.connected && <div className="sf-form"><label>オブジェクト<input value={sfObjectName} onChange={(event) => setSfObjectName(event.target.value)} /></label><label>外部ID<input value={sfExternalId} onChange={(event) => setSfExternalId(event.target.value)} /></label><label>項目マッピング<textarea rows={7} value={sfMappingText} onChange={(event) => setSfMappingText(event.target.value)} /></label><button className="button primary" onClick={() => void sendToSalesforce()} disabled={!result || busy === "salesforce" || (checked.size === 0 && hasUnappliedChanges)} title={checked.size === 0 && hasUnappliedChanges ? "変更した条件を先に検索してください" : undefined}><Send size={16} />{checked.size ? `選択した${checked.size}社` : "現在の検索結果"}を送信</button></div>}
                {salesforceJob && <div className="job-status"><strong>{salesforceJob.state}</strong><span>{fmt.format(salesforceJob.number_records_processed)}件処理 / {fmt.format(salesforceJob.number_records_failed)}件失敗</span><small>{salesforceJob.job_id}</small></div>}
              </ConnectionCard>
            </div>
            {data?.db_path && <div className="path-card"><Database size={17} /><span><strong>現在のデータベース</strong><code>{data.db_path}</code></span></div>}
          </section>
        )}
      </main>
      {mobileNavOpen && <button className="nav-scrim" aria-label="メニューを閉じる" onClick={() => setMobileNavOpen(false)} />}
    </div>
  );
}

function FilterPanel({ plan, setPlan, onApply, busy, onClose }: {
  plan: SearchPlan;
  setPlan: React.Dispatch<React.SetStateAction<SearchPlan>>;
  onApply: (plan: SearchPlan) => void;
  busy: boolean;
  onClose: () => void;
}) {
  return (
    <aside className="filter-panel" aria-label="絞り込み条件">
      <div className="filter-panel-head"><span><Filter size={18} /><strong>絞り込み</strong></span><button className="icon-button" aria-label="絞り込みを閉じる" onClick={onClose}><PanelLeftClose size={18} /></button></div>
      <div className="filter-scroll">
        <fieldset>
          <legend>地域</legend>
          <label>都道府県
            <select value="" onChange={(event) => { if (event.target.value) setPlan((current) => ({ ...current, prefectures: Array.from(new Set([...current.prefectures, event.target.value])) })); }}>
              <option value="">追加する都道府県</option>{prefectures.filter((value) => !plan.prefectures.includes(value)).map((value) => <option value={value} key={value}>{value}</option>)}
            </select>
          </label>
          {plan.prefectures.length > 0 && <div className="mini-chips">{plan.prefectures.map((value) => <button key={value} onClick={() => setPlan((current) => ({ ...current, prefectures: current.prefectures.filter((item) => item !== value) }))}>{value}<X size={13} /></button>)}</div>}
          <label>市区町村（部分一致）<input value={plan.cities.join("、")} onChange={(event) => setPlan((current) => ({ ...current, cities: splitValues(event.target.value) }))} placeholder="横浜市、千代田区" /></label>
        </fieldset>

        <fieldset>
          <legend>業種</legend>
          <label>JSICコード<input value={plan.industry_codes.join("、")} onChange={(event) => setPlan((current) => ({ ...current, industry_codes: splitValues(event.target.value) }))} placeholder="G、39、3911" /></label>
          <label>業種キーワード<input value={plan.industry_terms.join("、")} onChange={(event) => setPlan((current) => ({ ...current, industry_terms: splitValues(event.target.value) }))} placeholder="情報サービス、食品製造" /></label>
          <small className="field-help">大分類・中分類・詳細コードをコード境界で検索します。</small>
        </fieldset>

        <fieldset>
          <legend>法人種別</legend>
          <div className="checkbox-list">{companyKinds.map((kind) => <label key={kind.value}><input type="checkbox" checked={plan.company_kinds.includes(kind.value)} onChange={() => setPlan((current) => ({ ...current, company_kinds: toggle(current.company_kinds, kind.value) }))} />{kind.label}</label>)}</div>
        </fieldset>

        <fieldset>
          <legend>規模</legend>
          <div className="two-fields"><label>従業員 最小<input type="number" min={0} value={plan.min_employees ?? ""} onChange={(event) => setPlan((current) => ({ ...current, min_employees: toOptionalNumber(event.target.value) }))} /></label><label>最大<input type="number" min={0} value={plan.max_employees ?? ""} onChange={(event) => setPlan((current) => ({ ...current, max_employees: toOptionalNumber(event.target.value) }))} /></label></div>
          <div className="two-fields"><label>資本金 最小<input type="number" min={0} value={plan.min_capital ?? ""} onChange={(event) => setPlan((current) => ({ ...current, min_capital: toOptionalNumber(event.target.value) }))} /></label><label>最大<input type="number" min={0} value={plan.max_capital ?? ""} onChange={(event) => setPlan((current) => ({ ...current, max_capital: toOptionalNumber(event.target.value) }))} /></label></div>
          <div className="two-fields"><label>設立年 From<input type="number" min={1800} max={2100} value={plan.established_from ?? ""} onChange={(event) => setPlan((current) => ({ ...current, established_from: toOptionalNumber(event.target.value) }))} /></label><label>To<input type="number" min={1800} max={2100} value={plan.established_to ?? ""} onChange={(event) => setPlan((current) => ({ ...current, established_to: toOptionalNumber(event.target.value) }))} /></label></div>
        </fieldset>

        <fieldset>
          <legend>公開情報</legend>
          <label>Webサイト<TriState value={plan.website_required} onChange={(value) => setPlan((current) => ({ ...current, website_required: value }))} /></label>
          <label>電話番号<TriState value={plan.phone_required} onChange={(value) => setPlan((current) => ({ ...current, phone_required: value }))} /></label>
        </fieldset>

        <fieldset>
          <legend>追加キーワード</legend>
          <label>いずれかを含む<input value={plan.keyword_any.join("、")} onChange={(event) => setPlan((current) => ({ ...current, keyword_any: splitValues(event.target.value) }))} placeholder="SaaS、クラウド" /></label>
          <label>すべてを含む<input value={plan.keyword_all.join("、")} onChange={(event) => setPlan((current) => ({ ...current, keyword_all: splitValues(event.target.value) }))} placeholder="自社開発、法人向け" /></label>
          <label>全件操作の上限<input type="number" min={1} max={MAX_RESULTS} value={plan.limit} onChange={(event) => setPlan((current) => ({ ...current, limit: Math.max(1, Math.min(MAX_RESULTS, Number(event.target.value) || 1)) }))} /></label>
        </fieldset>
      </div>
      <div className="filter-panel-actions"><button className="button quiet" onClick={() => setPlan(emptyPlan())}>クリア</button><button className="button primary" disabled={busy} onClick={() => onApply(plan)}>{busy ? <Loader2 className="spin" size={16} /> : <Search size={16} />}この条件で検索</button></div>
    </aside>
  );
}

function TriState({ value, onChange }: { value?: boolean | null; onChange: (value: boolean | null) => void }) {
  return <select value={value == null ? "any" : value ? "yes" : "no"} onChange={(event) => onChange(event.target.value === "any" ? null : event.target.value === "yes")}><option value="any">指定なし</option><option value="yes">あり</option><option value="no">なし</option></select>;
}

function SortableHeader({ label, field, plan, onSort }: { label: string; field: SortField; plan: SearchPlan; onSort: (field: SortField) => Promise<void> }) {
  const active = plan.sort_by === field;
  const ariaSort = active ? (plan.sort_direction === "desc" ? "descending" : "ascending") : "none";
  return <th aria-sort={ariaSort}><button className="sort-button" onClick={() => void onSort(field)}>{label}<span aria-hidden="true">{active ? plan.sort_direction === "desc" ? "↓" : "↑" : "↕"}</span></button></th>;
}

function DetailPanel({ company, busy, engine, onClose, onOpenUrl, onResearch, onCollectPhone }: {
  company: Company | null;
  busy: string | null;
  engine?: string;
  onClose: () => void;
  onOpenUrl: (url?: string | null) => Promise<void>;
  onResearch: () => Promise<void>;
  onCollectPhone: () => Promise<void>;
}) {
  if (!company) return <aside className="detail-panel empty-detail"><Building2 size={27} /><strong>企業を選択</strong><span>行を選ぶと、出典を含む詳細をここで確認できます。</span></aside>;
  return (
    <aside className="detail-panel" aria-label={`${company.name}の詳細`}>
      <div className="detail-head"><div><small>法人番号 {company.corporate_number || "未確認"}</small><h2>{company.name}</h2></div><button className="icon-button" aria-label="詳細を閉じる" onClick={onClose}><X size={18} /></button></div>
      <div className="source-line"><span className="source-badge"><Database size={13} />企業マスター</span>{engine && <span className="source-badge muted">{engine}</span>}</div>
      <dl className="detail-list">
        <div><dt>FUMA ID</dt><dd>{company.fuma_id || "—"}</dd></div>
        <div><dt>データ区分</dt><dd>{company.source_kind || "—"}</dd></div>
        <div><dt>法人種別</dt><dd>{formatKind(company.kind)}</dd></div>
        <div><dt>所在地</dt><dd>{[company.prefecture, company.city, company.address].filter(Boolean).join(" ") || "—"}</dd></div>
        <div><dt>業種</dt><dd>{company.industry_name || company.inferred_industry_name || "—"}{company.industry_code && <small>{company.industry_code}</small>}</dd></div>
        <div><dt>業種階層</dt><dd>{[company.industry_middle_name, company.industry_small_name, company.industry_detail_name].filter(Boolean).join(" / ") || "—"}</dd></div>
        <div><dt>従業員</dt><dd>{company.employees == null ? "—" : `${fmt.format(company.employees)}名`}</dd></div>
        <div><dt>資本金</dt><dd>{compactNumber(company.capital)}</dd></div>
        <div><dt>設立年</dt><dd>{company.established_year ?? "—"}</dd></div>
        <div><dt>代表者</dt><dd>{company.representative || "—"}</dd></div>
        <div><dt>電話</dt><dd>{company.phone || "—"}{company.phone_type && <small>{company.phone_type}</small>}</dd></div>
        <div><dt>電話出典</dt><dd>{company.phone_source_url ? <button className="inline-link" onClick={() => void onOpenUrl(company.phone_source_url)}>{company.phone_source_url}<ExternalLink size={14} /></button> : "—"}</dd></div>
        <div><dt>Web</dt><dd>{company.website ? <button className="inline-link" onClick={() => void onOpenUrl(company.website)}>{company.website}<ExternalLink size={14} /></button> : "—"}</dd></div>
      </dl>
      {company.business_summary && <div className="business-summary"><strong>事業概要</strong><p>{company.business_summary}</p></div>}
      <div className="detail-actions"><button className="button primary" onClick={() => void onResearch()} disabled={busy === "research"}><FileSearch size={16} />この企業を調査</button><button className="button quiet" onClick={() => void onCollectPhone()} disabled={!company.website || busy === "phone"}>{busy === "phone" ? <Loader2 className="spin" size={16} /> : <Phone size={16} />}公式サイトから電話確認</button></div>
      {company.source_updated_at && <small className="updated-at">データ更新: {company.source_updated_at}</small>}
    </aside>
  );
}

function LoadingRows() {
  return <>{Array.from({ length: 7 }, (_, index) => <tr className="skeleton-row" key={index}><td colSpan={7}><span /></td></tr>)}</>;
}

function EmptyState({ icon, title, text }: { icon: React.ReactNode; title: string; text: string }) {
  return <div className="empty-state">{icon}<strong>{title}</strong><span>{text}</span></div>;
}

function PageHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <div className="page-heading"><span>{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>;
}

function ConnectionCard({ icon, title, status, ok, children }: { icon: React.ReactNode; title: string; status: string; ok: boolean; children: React.ReactNode }) {
  return <article className="connection-card"><div className="connection-card-head"><span className="connection-icon">{icon}</span><span className={`status-badge ${ok ? "ok" : ""}`}>{ok && <CheckCircle2 size={14} />}{status}</span></div><h2>{title}</h2>{children}</article>;
}

export default App;
