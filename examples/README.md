# 端到端示例（全部虚构）

这里的用户名、ID、引文都是编造的，只用来展示“填好了长什么样”。不要把它们导入真实客户库。

文件名带 `.example`，是为了避开 `.gitignore` 里对真实运行文件（`observed.json`、`feedback.json` 等）的忽略规则。

## 一轮流程

```bash
DB=../instagram-hk-leads-data/leads.sqlite3

python3 scripts/leads.py --db $DB init
python3 scripts/leads.py --db $DB ingest examples/observed.example.json
python3 scripts/leads.py --db $DB batch --limit 20        # 记下返回的 batch_id
python3 scripts/leads.py --db $DB batch --list            # 接手时用它找回未反馈的批次
# 把 feedback.example.json 里的 batch_REPLACE_ME 换成真实 batch_id
python3 scripts/leads.py --db $DB review feedback.json
python3 scripts/leads.py --db $DB progress examples/progress.example.json
python3 scripts/leads.py --db $DB learn rules.json
python3 scripts/leads.py --db $DB stats
python3 scripts/leads.py --db $DB next
python3 scripts/leads.py --db $DB export --format markdown --output out/accepted.md
```

## `ingest` 的返回

```json
{
  "inserted_accounts": 3,
  "updated_assessments": 0,
  "new_sources": 3,
  "new_evidence": 7,
  "new_auto_acceptances": 0,
  "mode": "learning"
}
```

## `batch --list` 的返回

```json
{
  "batches": [
    {
      "batch_id": "batch_37dcaff059ab4f1e",
      "created_at": "2026-09-05T09:05:36.203881+00:00",
      "mode": "learning",
      "item_count": 3,
      "reviewed_count": 0,
      "open": true,
      "pending_numbers": [1, 2, 3]
    }
  ],
  "open_batches": ["batch_37dcaff059ab4f1e"]
}
```

## 用户回复与 `review` 文件

用户说：“1 符合，系香港人仲住觀塘；2 唔知係唔係男仔；3 唔啱，係間甜品店。”

agent 写成：

```json
{
  "batch_id": "batch_37dcaff059ab4f1e",
  "reviews": [
    {"number": 1, "decision": "符合", "reason": "用户原话：系香港人，仲住觀塘"},
    {"number": 2, "decision": "不确定", "reason": "", "failed_criteria": ["male"]},
    {"number": 3, "decision": "不符合", "reason": "用户原话：这是间甜品店", "failed_criteria": ["personal"]}
  ]
}
```

`failed_criteria` 让 `stats.human_rejections_by_criterion` 能按 `personal` / `male` / `resident_hk` 统计人工否决原因；没有它，“学习”就只有一段自由文本可看。

## `stats` 中值得看的字段

```json
{
  "mode": "learning",
  "decisions": {"pending:human": 1, "rejected:human": 1, "accepted:human": 1},
  "open_batches": [],
  "completed_human_batches": ["batch_37dcaff059ab4f1e"],
  "model_snapshot_vs_human": {
    "eligible_accepted": 1, "eligible_rejected": 0,
    "ineligible_accepted": 0, "ineligible_rejected": 1, "human_uncertain": 1
  },
  "human_rejections_by_criterion": {"personal": 1, "male": 0, "resident_hk": 0, "unspecified": 0},
  "pending_gate_blockers": {},
  "auto_acceptances": 0,
  "auto_acceptances_needing_review": [],
  "forgotten": 0
}
```

- `pending_gate_blockers`：待审账号被固定检查挡住的原因计数（例如 `male:unknown`、`resident_hk:weak_signal`）。审过两三批后看这个字段，就能知道自动模式在实际数据上是否可能触发。
- `auto_acceptances_needing_review`：曾经自动入选、现在因为重新判断或模式降级不再入选的账号。

## 改名与删除

```bash
# Instagram 用户改名：旧记录整体并入新用户名，批次编号、人工结论、证据都保留
python3 scripts/leads.py --db $DB merge fictional_candidate_02 fictional_candidate_02_new --reason "IG 改名"

# 个人资料删除请求：抹掉该账号的全部记录，只留用户名哈希以拒绝再次导入
python3 scripts/leads.py --db $DB forget fictional_shop_03 --reason "本人要求删除"
python3 scripts/leads.py --db $DB forget fictional_shop_03 --unblock   # 确需恢复时解除拒绝
```

## 导出的 Markdown

```markdown
| 账号 | 主页 | 结果 | 判断来源 | 理由 | 独立合格种子数 | 来源路径 | 证据摘要 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fictional_seed_hk | https://www.instagram.com/fictional_seed_hk/ | accepted | human | 用户原话：系香港人，仲住觀塘 | 0 | search / source_account=(discovery) / 香港 / accounts / https://www.instagram.com/explore/search/keyword/?q=%E9%A6%99%E6%B8%AF | resident_hk / explicit_residence: 住喺觀塘 · 香港仔 (…); … |
```
