#!/usr/bin/env python3
from __future__ import annotations
import json
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
required = [
    'package.json', 'src/App.tsx', 'src/api.ts',
    'src-tauri/Cargo.toml', 'src-tauri/tauri.conf.json',
    'src-tauri/src/lib.rs', 'src-tauri/src/db.rs',
    'src-tauri/src/codex.rs', 'src-tauri/src/duckdb_native.rs', 'src-tauri/src/salesforce.rs',
    'scripts/fetch-codex.ps1', 'scripts/build-windows.ps1',
    '.github/workflows/windows-build.yml',
]
missing=[f for f in required if not (ROOT/f).exists()]
if missing:
    raise SystemExit('Missing required files: '+', '.join(missing))
for p in ROOT.rglob('*.json'):
    if 'node_modules' in p.parts or 'target' in p.parts:
        continue
    with p.open(encoding='utf-8') as f:
        json.load(f)
py_compile.compile(str(ROOT/'scripts/normalize-jsic.py'), doraise=True)
conf=json.load(open(ROOT/'src-tauri/tauri.conf.json',encoding='utf-8'))
assert conf['bundle']['targets']==['msi','nsis']
assert conf['bundle']['resources']['resources/bin/codex.exe']=='bin/codex.exe'
assert 'gpt-5.6-luna' in (ROOT/'src-tauri/src/codex.rs').read_text(encoding='utf-8')
native=(ROOT/'src-tauri/src/duckdb_native.rs').read_text(encoding='utf-8')
assert 'https://data.queria.io/houjin_bangou/ducklake.duckdb' in native
assert 'https://data.queria.io/gbizinfo/ducklake.duckdb' in native
assert "ATTACH 'ducklake:{HOUJIN_CATALOG}'" in native
assert '(READ_ONLY)' in native
cargo=(ROOT/'src-tauri/Cargo.toml').read_text(encoding='utf-8')
assert '1.10505.0' in cargo and 'bundled' in cargo and 'parquet' in cargo
print('CompanyScout source checks: OK')
