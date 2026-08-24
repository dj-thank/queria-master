# Luna公式サイト根拠ワークフロー

## 役割分離

この機能は、次の二つを混同しない。

1. Codexの `gpt-5.6-luna` evidence laneが、企業を非重複範囲へ分け、公式企業・公式親会社ページを読み、根拠URL付きJSON packetを返す。
2. GitHub Actions Runnerが、既知公式サイトを低速・再開可能に巡回し、語彙観測、電話候補、同一ホストフォーム、決定的スコアを全件JSONLへ生成する。

GitHub ActionsからOpenAI APIやCodexセッションを呼び出さない。Luna packetはrootがランタイムのmodel/turn/parent-spawn証跡を検証し、候補・未知・反証条件を正規化してから、ローカル成果物へ統合する。企業別packetをGitやReleaseへ公開しない。

## Luna packet契約

各企業は最低限、次を返す。

```json
{
  "entity_key": "13桁法人番号または安定キー",
  "corporate_number": "13桁法人番号",
  "company_name": "企業名",
  "official_site": {"url": "https://...", "canonicality": "official"},
  "official_evidence": [
    {"signal": "事業・親会社・連絡先", "url": "https://...", "excerpt": "最大240文字", "observed_at": "RFC3339または日付"}
  ],
  "parent_company": {"name": null, "status": "observed|inference|unknown", "evidence_url": null},
  "business_signals": [
    {"signal": "it_subsidiary|user_system_it|ses|si|contract_development|onsite_development|it_operations|recruitment|other", "fact_or_inference": "observed|inference", "strength": 0}
  ],
  "contact_evidence": [
    {"phone": null, "type": "representative|inquiry|privacy|branch|form|unknown", "evidence_url": "https://..."}
  ],
  "negative_controls": [],
  "unknowns": [],
  "promotion_authorized": false
}
```

## root受入条件

- 実行receiptが `gpt-5.6-luna`、medium、fresh context、正しい親spawnを示す。
- 対象範囲・法人番号が入力ledgerと一致する。
- observed claimは公式会社または公式グループページURLを持つ。
- 検索snippet、集約サイト、社名、JSICだけの主張はinference/unknownへ降格する。
- Lunaがconfirmedと記載しても、電話は `candidate_needs_review`、親会社関係は `candidate` へ正規化する。
- FAX、個人情報、採用、支店等の用途限定番号を代表電話へ昇格しない。
- 重複法人、無効電話、無効URL、根拠欠落が1件でもあればpacket統合を失敗させる。

## Runnerとの統合

Runnerの `business_profile.py` は全文を保持せず、最大240文字の抜粋・同一ホストURL・SHA-256だけをprogress schema v2へ残す。`ses_priority_json.py` は全行をJSON Schemaで検証し、正本JSONLからCSV・集計を一方向生成する。

Luna packetは高価値候補の意味的確認、Runnerは既知公式サイト全体の再現可能な収集を担当する。両者の事業スコアは営業優先度であり、法人関係や電話の手動確認receiptではない。

## 未充足HPの境界

このRunnerは、Release generationに束縛済みの公式サイトだけを巡回する。HP未充足企業は、Wikidata/OpenStreetMap等の出典・法人結合・利用条件を別途検証してcandidate websiteとして統合し、同じRunnerへ渡す。検索engineの無制限自動収集、有料API、利用条件で二次利用を禁じるソースは使用しない。
