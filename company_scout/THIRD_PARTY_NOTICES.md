# Third-party notices / data sources

CompanyScout itself is an internal MVP source package. Before redistributing a compiled installer outside your organization, review the current licenses and terms of every bundled dependency and data source.

## OpenAI Codex

- Source: https://github.com/openai/codex
- The build script fetches the pinned runtime; the binary is not committed in this source package.
- Review the repository's current license/notice files at release time.

## DuckDB

- Project: https://duckdb.org/
- Rust client: https://github.com/duckdb/duckdb-rs
- Embedded engine target: DuckDB 1.5.5 (`duckdb` crate `1.10505.0`)
- DuckLake and httpfs are DuckDB core extensions loaded by the embedded engine.

## Queria datasets / original sources

CompanyScout does not bundle the Queria CLI. It reads public DuckLake catalogs directly with DuckDB in READ_ONLY mode.

Dataset terms can differ from software licenses. Check dataset metadata and original-source terms before redistribution.

- National Tax Agency corporate number data via Queria
- gBizINFO (Ministry of Economy, Trade and Industry) via Queria
- e-Stat Japan Standard Industrial Classification
- Optional EDINET data if added later

Sources:

- https://docs.queria.io/connection/duckdb-cli/
- https://www.houjin-bangou.nta.go.jp/
- https://info.gbiz.go.jp/
- https://www.e-stat.go.jp/classifications/terms/10
- https://disclosure2.edinet-fsa.go.jp/

## Salesforce

Salesforce is an external destination. CompanyScout does not redistribute Salesforce data; users authenticate to their own Salesforce org. Review the Salesforce API terms applicable to the target organization.
