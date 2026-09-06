# 本机记录与命令

`scripts/leads.py` 只使用 Python 标准库和本机 SQLite，不联网、不登录 Instagram、不打开页面、不代替 ego-browser，也不联系名单中的人。每条命令必须显式提供 `--db`；数据位置由本次任务决定，工具没有隐含的默认数据库。

## 命令

以下命令从 skill 目录执行；`data/leads.sqlite` 仅是显式路径示例。

```bash
python3 scripts/leads.py --db data/leads.sqlite init
python3 scripts/leads.py --db data/leads.sqlite migrate                 # 旧版本数据库升级结构
python3 scripts/leads.py --db data/leads.sqlite ingest observed.json
python3 scripts/leads.py --db data/leads.sqlite batch --limit 20
python3 scripts/leads.py --db data/leads.sqlite batch --list            # 全部批次及未反馈编号
python3 scripts/leads.py --db data/leads.sqlite batch --batch-id batch_ID
python3 scripts/leads.py --db data/leads.sqlite review feedback.json
python3 scripts/leads.py --db data/leads.sqlite merge old_name new_name --reason 'IG 改名'
python3 scripts/leads.py --db data/leads.sqlite forget some_name --reason '本人要求删除'
python3 scripts/leads.py --db data/leads.sqlite list
python3 scripts/leads.py --db data/leads.sqlite list --status pending
python3 scripts/leads.py --db data/leads.sqlite list --seeds
python3 scripts/leads.py --db data/leads.sqlite seed-policy
python3 scripts/leads.py --db data/leads.sqlite seed-policy --allow-female --reason '用户允许女生作为寻找男性的种子'
python3 scripts/leads.py --db data/leads.sqlite seed-policy --disallow-female --reason '用户取消女生种子用途'
python3 scripts/leads.py --db data/leads.sqlite next --limit 20
python3 scripts/leads.py --db data/leads.sqlite next --include-unavailable
python3 scripts/leads.py --db data/leads.sqlite progress progress.json
python3 scripts/leads.py --db data/leads.sqlite learn rules.json
python3 scripts/leads.py --db data/leads.sqlite mode
python3 scripts/leads.py --db data/leads.sqlite mode assisted --reason '依据真实人工反馈调整审核方式'
python3 scripts/leads.py --db data/leads.sqlite stats
python3 scripts/leads.py --db data/leads.sqlite export --format csv --output output/accepted.csv
python3 scripts/leads.py --db data/leads.sqlite export --format json --output output/accepted.json
python3 scripts/leads.py --db data/leads.sqlite export --format markdown --output output/accepted.md
```

命令输出 JSON，失败时向 stderr 输出 `{"error":"..."}` 并返回退出码 2。除 `init` 外，数据库不存在时直接报错，不创建空文件；`init` 对本工具数据库可重复执行，对已有其他数据库或非空普通文件在写入前拒绝。一次 `ingest`、`review` 或 `progress` 文件中的修改是一个事务，其中任何一项无效都会全部回退。

**结构版本**：`meta.schema_version` 当前为 3。v2 升 v3 增加女生种子政策及变更历史，默认关闭，不重写客户判断、反馈或批次快照。打开旧版本数据库时，普通命令会拒绝并提示先运行 `migrate`（或 `init`）；升级按版本逐步执行、每一步一个事务并做外键检查，返回 `migrations_applied`。升级前请备份文件。比工具更新的数据库会被拒绝而不是降级。

`export` 默认只导出入选名单；显式加 `--all` 才包括待定和人工排除项。导出不覆盖已有文件，请为新版本选新文件名。

旧版本保存过带用户名前缀的帖子证据／来源时，重新导入会按等价 URL 匹配原记录，沿用原 ID；来源仍更新首次／最近看到的时间。仅 URL 写法变化不会新增模型判断或触发一轮重复审核。旧 URL、历史判断及审核批次不回写；观察时间、原文、业务参数或判断实际变化仍正常记录。

### 改名与删除

- `merge OLD NEW --reason`：Instagram 用户改名时，把 OLD 的判断、证据、来源、进度、批次条目和人工结论整体并入 NEW。NEW 已存在时保留较新的模型判断和已有的人工结论；两者人工结论不同或 `ig_user_id` 不同则拒绝。批次编号不变，快照里的用户名一并改写。
- `forget NAME --reason`：删除该账号的全部记录，包括它作为种子出现在别人来源里的记录和其他账号快照中指向它的来源；只保留用户名的加盐哈希，之后 `ingest` 同名会被拒绝，`forget NAME --unblock` 解除。这是响应本人删除要求的入口，不是清理无用候选的手段。

## 导入账号与发现来源

`ingest` 的顶层键是 `accounts`，值为非空对象列表。下面只是字段示意，不是已发现的客户；示例为不确定状态，不能当作真实数据导入客户库。

```json
{
  "accounts": [
    {
      "username": "fictional_candidate",
      "profile_url": "https://www.instagram.com/fictional_candidate/",
      "ig_user_id": "100000000000",
      "assessment": {
        "personal": "unknown",
        "male": "unknown",
        "resident_hk": "unknown",
        "confidence": "uncertain",
        "reason": "仅说明字段；还没有核对实际账号",
        "rule_version": 0
      },
      "evidence": [
        {
          "criterion": "resident_hk",
          "signal": "flag",
          "text": "这里应保存实际看到的短原文，不补写未看到的信息",
          "url": "https://www.instagram.com/fictional_candidate/",
          "observed_at": "2026-09-05T09:00:00+08:00"
        }
      ],
      "sources": [
        {
          "kind": "following",
          "source_account": "fictional_seed",
          "source_url": "https://www.instagram.com/fictional_seed/",
          "source_key": "following",
          "observed_at": "2026-09-05T09:00:00+08:00"
        }
      ]
    }
  ]
}
```

- `username` 与 `profile_url` 至少提供一个；同时提供时必须一致。用户名去掉开头 `@` 并转成小写，主页 URL 由用户名规范化，帖子 URL 和 `explore`、`direct`、`locations` 等保留路径不接受为主页。
- `ig_user_id` 可选，是 Instagram 的数字用户 ID（页面源码或 API 响应里可见）。用户名会改，ID 不会：同一 ID 出现在不同用户名下时 `ingest` 会拒绝并提示 `merge`；已有账号记录了不同 ID 也会拒绝。能拿到就填。
- `assessment.personal/male/resident_hk` 分别取 `yes`、`no`、`unknown`；`confidence` 取 `clear` 或 `uncertain`。这三项始终是模型判断，人工最终结论另外保存；不添加年龄门槛。
- `assessment.rule_version` 可记录本次实际参考的规则版本，0 表示尚无反馈规则。工具保存该标记，不会据此假称模型已执行或学会自然语言规则。
- 新提交 `assessment` 时必须同批携带其依据 `evidence`；工具不会偷偷用旧判断的证据支持新判断。只新增来源时可以仅传 `username` 与 `sources`，原判断保持不变。
- `evidence` 记录判断标准 `criterion`、信号类别 `signal`、看到的短原文 `text`、对应 `url` 和带时区的 `observed_at`。缺失原文、URL 或观察时间的证据可保留，但不能支持自动入选；判断不了的内容直接保留 `unknown`。
- `sources.kind` 支持 `search`、`place`、`following`、`follower`、`comment`、`reply`、`mention`；后五种必须有 `source_account`。`source_url` 是实际来源页，`source_key` 是用于区分入口的查询、地点、帖子或评论标识。

### 自动入选的固定检查

只有三项均为 `yes`、`confidence=clear`、判断理由不空、每项都有可追溯证据，才可能自动入选。居港证据还必须至少含下列一个明确强信号；其他拼写或未知 `signal` 一律不能靠这个字段自动通过：

| `signal` | 可使用的实际依据 |
| --- | --- |
| `explicit_residence` | 本人明确描述当前在香港居住／生活的自述。 |
| `self_description` | 兼容写法，含义严格同 `explicit_residence`；仅写香港、旗帜、语言或籍贯不能使用这个标签。 |
| `recent_hk_life` | 不同日期的近期原创内容持续呈现在香港生活；保存对应内容依据，而非一次游客打卡。 |
| `recurring_local_life` | 兼容写法，含义同 `recent_hk_life`。 |

`flag`、`hk_flag`、`language`、单次地点、关键词、标签、转发内容或未识别信号只能帮助发现候选。脚本只校验结构和明确字段，不会读证据网页、辨别虚构文本或替 agent 核实帖子；agent 必须按实际看见的内容填写，不能用换一个 `signal` 名称来放宽规则。

### 来源方向与查看顺序

| `kind` | 该条记录表达的关系 |
| --- | --- |
| `following` | 从 `source_account` 的关注列表发现该候选：种子关注候选。 |
| `follower` | 从 `source_account` 的粉丝列表发现该候选：候选关注种子。 |
| `comment` / `reply` | 候选在来源帖子发表评论／回复；来源帖子与种子账号分别记录。 |
| `mention` | 来源账号／内容提及候选，保留实际提及页面。**只是线索，不计入种子数。** |
| `search` / `place` | 从查询或地点入口发现候选；不假设社交关系。 |

相同候选、入口种类、种子账号与入口标识的重复观察只更新首次／最近看到的时间，不增加来源条数；关注和粉丝各自采用固定入口标识。Instagram URL 会移除 `igsh`、`igshid`、`utm_*`、`fbclid`、`gclid` 等追踪参数，保留 `q`、`keywords`、`comment_id` 等业务参数。具体帖子链接同时支持根路径与用户名前缀：`/{username}/p/{shortcode}/` 归到 `/p/{shortcode}/`，`/{username}/reel/{shortcode}/` 归到 `/reel/{shortcode}/`；证据、来源和进度中的帖子地址先归一，再参与各自记录的去重；不同证据内容、观察时间或业务参数仍按相应字段处理。

优先级按**不同的当前可用种子账号数**排序；同一种子多次评论，或通过其关注、粉丝、评论三种路径重复找到，仍只算一个独立种子。人工入选、当前模式下通过固定检查的客户，以及下文已启用的女生种子，通过 `following` / `follower` / `comment` / `reply` 指向候选时才计入；`mention`、候选本人和其他未确认来源不加分。关系方向全部保留，来源数既不是居港概率，也不能把人工排除项救回客户名单。

### 女生仅作种子

客户判定仍要求男性；种子用途通过 `seed_eligible`、`seed_only`、`seed_origin` 单独表达。新库及迁移后的 `allow_female` 默认 `false`，`seed-policy` 不带修改选项时只读。

用户明确允许女生用于寻找男性后，执行 `seed-policy --allow-female --reason ...`。这项政策适用于已有及以后收到的人工反馈：只有当前客户结果为 `rejected`，且最近有效人工反馈的 `failed_criteria` 恰为 `["male"]`，才增加女生种子资格，来源标记 `human_female_feedback`。这不是模型根据名称或头像猜性别，也不代表女性已被确认满足其余客户条件；只是用户授权的寻找入口。拒绝原因为商业、假号、原因不明，或同时还有其他失败条件，不会因此成为种子。

`list --seeds` 和 `next` 包含这些女生，`next.client_status` 仍明确为 `rejected`。默认客户 `export` 只导出 `accepted`，不会混入女生；批次、模式及人工通过计数也不受政策开关影响。`stats.seed_policy` 显示政策，`seed_eligible_count` 是当前可用种子总数，`seed_only_accounts` 单列女生用户名。

政策变化必须有理由并记录旧值、新值和时间，重复提交同一值不增加事件。`--disallow-female` 关闭政策，或后续人工反馈纠正为商业号等不适用情况时，资格和关系权重随当前记录撤销，原有证据与历史不删除。改名、合并、本人删除沿用 `merge` / `forget`，不能留下失效的种子来源。

## 稳定编号与人工反馈

`batch --limit 20` 创建稳定的 `batch_id`，并为这一批的每个账号保存固定 `number`、当时的模型判断、证据、来源和排序依据。以后导入新账号或修改模型判断，都不改变已发给用户的编号或旧快照（仅 `forget` / `merge` 会改写快照中的用户名）；重新查看用 `batch --batch-id ...`。

`batch --list` 列出全部批次、每批条数、已反馈条数和 `pending_numbers`；`open_batches` 也出现在 `stats` 里。换一个 agent 接手时先看它，再把用户的“1、3 符合”对应到正确的批次。

默认批次只选择 `pending` 账号，不把自动入选账号全部重新要求审核；已经在未完成批次里的账号也不重复分配。对人工 `不确定` 的账号，只有出现新的判断／证据快照后才再次进入普通批次。需要刻意复核已审核或自动入选账号时，使用 `batch --include-reviewed --limit 20`；这不会删除旧记录。可用候选不足 20 时只返回真实数量，不补造账号。

用户可以只回编号。agent 将实际反馈对应到发送过的批次，使用以下格式；没有理由时可以省略 `reason` 或保留空字符串，不能替用户编造。

```json
{
  "batch_id": "batch_ID",
  "reviews": [
    {"number": 1, "decision": "符合"},
    {"number": 2, "decision": "不符合", "reason": "用户提供的原话或忠实简述", "failed_criteria": ["personal"]},
    {"number": 3, "decision": "不确定", "reason": "", "failed_criteria": ["male"]}
  ]
}
```

`decision` 也接受 `accepted`、`rejected`、`uncertain`。`failed_criteria` 可选，取 `personal` / `male` / `resident_hk` 的子集，表示用户否决或存疑的是哪一项；`符合` 不能带它。用户没说清楚就不填，`stats.human_rejections_by_criterion.unspecified` 会记录未拆分的否决数。这是“学习”能按条件统计错误的唯一输入。人工结论优先于后续任何模型导入：符合可入选并扩展，不符合排除，不确定仍为 `pending`；后续出现更多来源不会覆盖人工结论。用户明确纠正旧结论时，可对原批次编号再次提交新反馈；历史追加保存，完全相同的重复反馈不再添加一条记录。

`review` 专门记录真实用户反馈，不能让 agent 生成“人工审核结果”或把自动判断转录成反馈。数据库中的 `origin=human_feedback` 是操作约定和审计记录，**不是鉴别人类身份的技术保证**；调用者必须遵守真实反馈的来源边界。

## 审核方式与规则版本

- `learning`：模型即使三项明确也只形成待审候选；人工入选账号可以继续扩展。
- `assisted`：通过固定检查的账号可入选并扩展，agent 仍较频繁展示有疑问的候选和实际需要核对的规则。
- `automatic`：通过同一套固定检查的账号可入选并扩展，agent 不逐个要求审批；未知或证据不足的账号保留待定，继续处理其他可做的入口。

`assisted` 与 `automatic` 在本工具里的硬性入选检查相同，差别在 agent 与用户的审核节奏。工具不会自动升级模式；提高自动程度必须显式调用 `mode`、给出理由，并已有至少两个完整收到人工反馈的批次。**两个批次只是防止单轮就升级，不代表准确率已足够，也不是自动切换的依据**；需要结合实际错误、不同来源覆盖和用户意愿判断，前期人工轮次不限。降级同样记录理由；人工结论保持优先。

**自动入选的审计**：状态是按当前模式实时计算的，所以工具另外把每一次自动入选写进 `auto_acceptances`（用户名、判断哈希、模式、规则版本、触发点 `ingest` 或 `mode_change`）。模式降回 `learning` 时返回 `auto_accepted_now_pending`；重新判断让某个账号不再通过检查时，`stats.auto_acceptances_needing_review` 和 `list` 里的 `auto_acceptance_needs_review` 会标出它，用 `batch --include-reviewed` 复核。

**自动模式在实际数据上是否可行**，看 `stats.pending_gate_blockers`：它统计当前待审账号被固定检查挡住的原因（如 `male:unknown`、`resident_hk:weak_signal`、`personal:no_evidence`）。如果绝大多数都是 `male:unknown`，说明目标定义本身限制了自动化，而不是证据收集不够。

规则总结文件：

```json
{
  "based_on_batches": ["batch_ID_1", "batch_ID_2"],
  "rules": [
    "此处填写从这些批次真实人工反馈归纳出的规则",
    "同时记录不适用情况与仍不能判断的情形"
  ],
  "reason": "说明哪些反馈支持这次规则变化"
}
```

`learn` 引用的每个批次只需至少收到一条真实人工反馈，不要求整批 20 个审核完成，允许边审核边总结。规则只能依据该版本创建前已经收到的反馈，未审条目和模型自己的判断不能充当依据；空反馈批次会被拒绝。它与 `mode` 提高自动程度所需的多个完整人工批次是两种不同检查。

每次 `learn` 生成新版本并保留旧版本，不训练模型、不执行自然语言规则、不触发模式升级。agent 通过 `stats.latest_rule` 阅读最新规则；`stats.model_snapshot_vs_human` 对比当时模型判断与人工结论，自动判断不计作人工真值，人工不确定单列。该计数是审核记录，不是抽样方法得到的总体准确率。

## 入口进度与恢复

```json
{
  "entries": [
    {
      "kind": "search",
      "source_key": "香港 / accounts / 本轮具体查询",
      "status": "in_progress",
      "cursor": {"last_visible_result": "界面中最后实际核对的位置"}
    },
    {
      "kind": "place",
      "source_key": "实际地点页 URL 或稳定地点标识",
      "status": "unavailable",
      "reason": "本次界面没有可用地点入口"
    },
    {
      "username": "fictional_seed",
      "kind": "following",
      "status": "in_progress",
      "cursor": "实际看到并完成核对的位置"
    },
    {
      "username": "fictional_seed",
      "kind": "posts",
      "status": "in_progress",
      "cursor": "已发现哪些可见原创帖子，尚有哪些未核对"
    },
    {
      "username": "fictional_seed",
      "kind": "comment",
      "source_key": "https://www.instagram.com/p/FICTIONAL/",
      "status": "pending",
      "cursor": ""
    }
  ]
}
```

`search`、`place` 不需要账号，以 `kind + source_key` 单独记录；其余入口要求账号已经导入。关注、粉丝和帖子发现分别用 `following`、`follower`、`posts`；每个评论入口的 `source_key` 必须是实际看到的具体 Instagram 帖子／Reel URL；支持 `/{username}/p/{shortcode}/`、`/{username}/reel/{shortcode}/` 等带用户名前缀的形式，由工具归一，不要求 agent 猜造另一条链接。`cursor` 可以是字符串或 JSON 对象，只保存界面实际可解释的位置，不伪造分页游标。

这项用户名前缀支持来自 2026-09-05 的实际页面读取：原工具保存该类评论入口时报错，修复后已把 3 条真实评论进度写入本地数据库并读回。根路径和带用户名前缀的重复记录归一、输入校验等由本地测试覆盖；本次改动不增加数据库结构版本。真实账号与帖子地址只留在本机。

状态为 `pending`、`in_progress`、`done`、`unavailable`；`unavailable` 必须说明原因。`done` 仅表示本轮约定的可见范围已经处理，不保证已经枚举整个平台上的全部数据。发现需要查看的帖子时先为其建立 `comment` 待办，再结束本轮 `posts` 发现；否则尚未登记的帖子无法凭空出现在数据库里。

`next` 只返回可继续扩展的种子，以及尚未完成的搜索／地点入口，不领取、不消耗、不完成任何任务。反复调用结果不变，直到显式写入进度或其他输入改变；未建立进度的种子默认有关注、粉丝、帖子发现三个待办。女生入口带 `seed_only=true`，其 `client_status` 不会改成客户通过。

不可用入口默认不安排自动重试；`next --include-unavailable` 可以只读查看这些入口，即使账号其他入口已经全部完成。搜索／地点的全部当前状态也在 `stats.discovery_progress` 中。原因解决后，通过 `progress` 将相应入口改回 `pending`；工具保留每次进度变化历史。

## 导出与数据边界

CSV 的 `status` 和 `decision_origin` 是最终状态及其来源，`ig_user_id` 有则输出，`model_personal/model_male/model_resident_hk/model_confidence/model_reason` 明确是模型原始预测，不能解释成人工结论。`reason` 使用实际人工理由或当前模型理由，人工没给理由就留空；证据和来源保留为 JSON 文本。

CSV 对以公式触发字符开头的文本加单引号，包括 `=`、`+`、`-`、`@` 及前置空白／制表符的情况。JSON 保留完整当前记录；Markdown 包含判断来源、理由、来源路径和证据摘要，便于人工复核。

SQLite 保存当前账号判断、历史模型判断、全部去重证据和来源、批次快照、人工反馈历史、规则版本、模式变更、自动入选记录、改名记录、删除哈希和每个入口的进度历史。工具没有任何联系、关注、发帖、评论、登录或后台任务功能；测试只用临时数据库和合成记录。

这些都是关于真实个人的资料（含性别、居住地推断和原话引用）。只保留任务需要的最短原文，运行数据不进公开仓库，收到本人删除要求时用 `forget`。

```bash
python3 -B -m unittest discover -s tests -v
```
