export type SearchPlan = {
  text?: string | null;
  prefectures: string[];
  cities: string[];
  industry_codes: string[];
  industry_terms: string[];
  company_kinds: string[];
  min_employees?: number | null;
  max_employees?: number | null;
  min_capital?: number | null;
  max_capital?: number | null;
  established_from?: number | null;
  established_to?: number | null;
  website_required?: boolean | null;
  keyword_any: string[];
  keyword_all: string[];
  limit: number;
};

export type SavedSearch = {
  id: string;
  name: string;
  query: string;
  plan: SearchPlan;
  created_at: string;
};

export type Company = {
  corporate_number: string;
  name: string;
  prefecture?: string | null;
  city?: string | null;
  address?: string | null;
  kind?: string | null;
  industry_code?: string | null;
  industry_name?: string | null;
  industry_source?: string | null;
  inferred_industry_code?: string | null;
  inferred_industry_name?: string | null;
  inferred_industry_confidence?: number | null;
  employees?: number | null;
  capital?: number | null;
  established_year?: number | null;
  website?: string | null;
  phone?: string | null;
  representative?: string | null;
  business_summary?: string | null;
  source_updated_at?: string | null;
};

export type SearchResult = {
  rows: Company[];
  total: number;
  page: number;
  page_size: number;
  elapsed_ms: number;
};

export type CodexStatus = {
  running: boolean;
  authenticated: boolean;
  email?: string | null;
  plan_type?: string | null;
  luna_available: boolean;
  model: string;
  version?: string | null;
};

export type ResearchReport = {
  corporate_number: string;
  company_name: string;
  thread_id: string;
  summary: string;
  transcript: string;
  industry_guess?: {
    code?: string | null;
    name?: string | null;
    confidence?: number | null;
    rationale?: string | null;
  } | null;
  findings: string[];
  sources: Array<{ url: string; title?: string | null; note?: string | null }>;
  created_at: string;
};

export type DataStatus = {
  company_count: number;
  taxonomy_count: number;
  research_count: number;
  db_path: string;
  duckdb_native: boolean;
  runtime_attached: boolean;
  duckdb_version?: string | null;
};

export type SalesforceStatus = {
  connected: boolean;
  username?: string | null;
  instance_url?: string | null;
  api_version: string;
};

export type SalesforceFieldMapping = {
  source: string;
  target: string;
};

export type SalesforceJobStatus = {
  job_id: string;
  state: string;
  number_records_processed: number;
  number_records_failed: number;
  number_records_total: number;
  error_message?: string | null;
};

export type PhoneCollectionResult = {
  phone?: string | null;
  source_url: string;
};
