# DuckDB Native data path

CompanyMaster 0.2 removes the Queria CLI/Python data subprocess. The Windows app embeds DuckDB directly through duckdb-rs and performs the public-data join inside the application process.

## Runtime

- DuckDB core: 1.5.5, pinned through `duckdb = 1.10505.0`
- Rust feature flags: `bundled`, `parquet`, `json`
- Remote data: DuckLake + HTTP(S), loaded as signed DuckDB core extensions
- Local cache: `%LOCALAPPDATA%/.../company-master.duckdb`

## Reproduce the remote join in plain DuckDB

DuckDB 1.5.2 or later:

```sql
INSTALL ducklake;
LOAD ducklake;
INSTALL httpfs;
LOAD httpfs;

ATTACH 'ducklake:https://data.queria.io/houjin_bangou/ducklake.duckdb'
  AS houjin_native (READ_ONLY);
ATTACH 'ducklake:https://data.queria.io/gbizinfo/ducklake.duckdb'
  AS gbiz_native (READ_ONLY);

SELECT
  h.corporate_number,
  h.name,
  h.prefecture_name,
  h.city_name,
  g.employee_number,
  g.capital_stock,
  g.company_url,
  g.business_summary,
  g.business_items
FROM houjin_native.main.mart_houjin_bangou h
LEFT JOIN gbiz_native.main.mart_gbizinfo_company g
  ON h.corporate_number = g.corporate_number
LIMIT 100;
```

For an authenticated Queria account/token, register it only in the current DuckDB process before `ATTACH`:

```sql
CREATE SECRET queria_auth (
  TYPE http,
  BEARER_TOKEN 'YOUR_TOKEN',
  SCOPE 'https://data.queria.io'
);
```

CompanyMaster reads `QUERIA_TOKEN` if present and creates a temporary DuckDB secret. It does not persist the token in the application database.

## Local files

The app's import button is also DuckDB-native:

- `.duckdb` / `.db`: `ATTACH ... (READ_ONLY)` and read `main.companies`
- `.parquet`: `read_parquet(...)`
- `.csv`: `read_csv_auto(...)`
- `.json` / `.jsonl`: `read_json_auto(...)`

No pandas, Python runtime, or intermediary CSV conversion is required.
