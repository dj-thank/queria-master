import { invoke } from "@tauri-apps/api/core";
import type {
  CodexStatus,
  Company,
  DataStatus,
  ResearchReport,
  SalesforceStatus,
  SalesforceFieldMapping,
  SalesforceJobStatus,
  PhoneCollectionResult,
  PublicEnrichmentOperation,
  PublicEnrichmentStatus,
  SavedSearch,
  SearchPlan,
  SearchResult,
} from "./types";

export const api = {
  bootstrap: () => invoke<DataStatus>("bootstrap"),
  dataStatus: () => invoke<DataStatus>("data_status"),
  search: (plan: SearchPlan, page = 1, pageSize = 100) =>
    invoke<SearchResult>("search_companies", { plan, page, pageSize }),
  saveSearch: (name: string, query: string, plan: SearchPlan) =>
    invoke<void>("save_search", { name, query, plan }),
  recentSearches: (limit = 12) => invoke<SavedSearch[]>("recent_searches", { limit }),
  exportCsv: (plan: SearchPlan, path: string) =>
    invoke<number>("export_search_csv", { plan, path }),
  exportXlsx: (plan: SearchPlan, path: string) =>
    invoke<number>("export_search_xlsx", { plan, path }),
  addToList: (listName: string, corporateNumbers: string[]) =>
    invoke<number>("add_to_list", { listName, corporateNumbers }),
  addSearchToList: (listName: string, plan: SearchPlan) =>
    invoke<number>("add_search_to_list", { listName, plan }),

  codexStatus: () => invoke<CodexStatus>("codex_status"),
  codexLogin: () => invoke<{ auth_url: string }>("codex_login"),
  codexLogout: () => invoke<void>("codex_logout"),
  planSearch: (query: string) => invoke<SearchPlan>("codex_plan_search", { query }),
  researchCompany: (company: Company, instruction: string) =>
    invoke<ResearchReport>("codex_research_company", { company, instruction }),
  collectCompanyPhone: (company: Company) =>
    invoke<PhoneCollectionResult>("collect_company_phone", { company }),

  duckdbStatus: () => invoke<{ available: boolean; version: string; engine: string; remote_catalog_mode: string }>("duckdb_native_status"),
  syncDuckDb: () => invoke<{ imported: number; source: string; duckdb_version: string }>("sync_duckdb_company_master"),
  importFile: (path: string) => invoke<number>("import_company_file", { path }),
  importTaxonomy: (path: string) => invoke<number>("import_industry_taxonomy", { path }),

  publicEnrichmentStatus: () => invoke<PublicEnrichmentStatus>("public_enrichment_status"),
  publicEnrichmentPrepare: (sourcePath: string, sheetName: string | null, replace: boolean) =>
    invoke<PublicEnrichmentOperation>("public_enrichment_prepare", { sourcePath, sheetName, replace }),
  publicEnrichmentMakeAssignment: (outputPath: string, chunkSize = 10000) =>
    invoke<PublicEnrichmentOperation>("public_enrichment_make_assignment", { outputPath, chunkSize }),
  publicEnrichmentRunAll: (inputDir: string) =>
    invoke<PublicEnrichmentOperation>("public_enrichment_run_all", { inputDir }),

  salesforceStatus: () => invoke<SalesforceStatus>("salesforce_status"),
  salesforceLogin: (loginUrl: string, clientId: string) =>
    invoke<{ auth_url: string }>("salesforce_login_start", { loginUrl, clientId }),
  salesforceUpsertList: (listName: string, objectName: string, externalIdField: string, mapping: SalesforceFieldMapping[]) =>
    invoke<{ accepted: number; job_id: string }>("salesforce_upsert_list", {
      listName,
      objectName,
      externalIdField,
      mapping,
    }),
  salesforceJobStatus: (jobId: string) => invoke<SalesforceJobStatus>("salesforce_job_status", { jobId }),
  salesforceRetryFailed: (jobId: string) => invoke<{ accepted: number; job_id: string }>("salesforce_retry_failed", { jobId }),
};
