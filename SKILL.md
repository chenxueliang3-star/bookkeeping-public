---
name: bookkeeping
description: "本 skill 管理一个本地 SQLite 个人记账数据库。用户需要记录、导入、查询、修改、删除流水，关联退款，管理活动花费，添加/修改资产账户，或打开 Dashboard 时，使用本 skill。"
---

# Bookkeeping

## 使用目标

Agent 的职责不是把账单原样交给脚本，而是把人工判断融入导入流程：

1. 把原始账单整理成标准 CSV。
2. 主动分析未分类账目，提炼稳定、可复用的关键词规则。
3. 将可靠的关键词规则写入数据库，再用于本次账单和以后的账单。
4. 判断账户别名和跨来源重复候选。
5. 导入账目，把无法可靠判断的部分留给人工确认。

## 分工边界

AI 负责判断和协作：

- 从自然语言中提取金额、账户、时间、对象、类型、分类意图。
- 对未知来源原始账单做只读检查，并临场编写一次性清洗脚本，把原始账单转换为标准 CSV。
- 主动梳理未分类账目的高频线索，判断哪些关键词规则可以长期复用。
- 把高置信、可复用的关键词规则写入数据库；不能只写入临时 JSON。
- 判断低置信账户别名、分类规则和待确认事项是否需要用户参与。

代码负责确定性工作：

- 数据库初始化、写入、余额计算、导入、去重、待确认统计、导出和 Dashboard。
- 标准 CSV 检查、读取已有关键词规则、生成关键词候选、导入前重复候选分析、正式导入后的去重与待确认生成。

不要在对话中临时复刻代码已有的去重、余额、导入或待确认统计逻辑；清洗出标准 CSV 后，直接调用 CLI。

## 初始化 SOP

首次使用本 skill 时，不要直接初始化数据库。先运行：

```bash
python3 scripts/bookkeeping.py db-path
```

告知用户当前数据库路径，并询问是否接受该路径或希望指定自定义路径。默认数据库不在 skill 目录内；未配置环境变量时通常位于：

```text
~/.local/share/bookkeeping/bookkeeping.db
```

用户确认使用当前路径后，再运行：

```bash
python3 scripts/bookkeeping.py init-db
```

如果用户希望使用自定义路径，指引用户设置数据库路径环境变量 `BOOKKEEPING_DB_PATH`，并初始化数据库：

```bash
BOOKKEEPING_DB_PATH=/path/to/bookkeeping.db python3 scripts/bookkeeping.py init-db
```

## config.yaml

项目配置文件位于：

```text
scripts/config.yaml
```

它用于维护用户的长期记账口径，不用于保存账目数据。Agent 首次使用本 skill、清洗新来源账单或判断账户关系前，应先读取该文件。

`categories` 定义可用分类树。例如：

```yaml
categories:
  支出:
    餐饮:
      - 早午晚餐
      - 饮料
```

对应合法分类为 `餐饮/早午晚餐` 和 `餐饮/饮料`。Agent 只能将账目和关键词规则映射到当前已有的合法分类；不要临场编造分类。

`accounts` 维护用户明确配置的长期自有账户清单。例如：

```yaml
accounts:
  - 招行储蓄卡
  - 支付宝
```

Agent 用它理解用户已经维护的账户，并辅助判断两个账户是否属于用户本人。只有确认转出和转入账户都是用户自有账户时，才能归类为内部转账。账户的新增、重命名和删除优先由用户通过 Dashboard 管理。

## 常用命令

```bash
python3 scripts/bookkeeping.py add --type 支出 --amount 45 --account 支付宝 --transaction-object 肯德基 --category "餐饮/早午晚餐" --note "午餐"
python3 scripts/bookkeeping.py transfer --from-account 支付宝 --to-account 微信 --amount 200 --note "账户间转账"
python3 scripts/bookkeeping.py correct-balance "招行储蓄卡" --actual 18500 --note "手动余额修正"
python3 scripts/bookkeeping.py list --limit 20
python3 scripts/bookkeeping.py accounts
python3 scripts/bookkeeping.py summary --month 2026-05
python3 scripts/bookkeeping.py unconfirmed-summary
python3 scripts/bookkeeping.py unconfirmed
python3 scripts/bookkeeping.py entry-update 12 --note "含押金"
python3 scripts/bookkeeping.py refund-link --refund-id 20 --expense-id 12
python3 scripts/bookkeeping.py activity-add --name 三亚旅行
python3 scripts/bookkeeping.py entry-activity --entry-id 12 --activity-id 3
python3 scripts/bookkeeping.py account-add --name 招行储蓄卡
python3 scripts/bookkeeping.py account-set "招行储蓄卡" --balance 1000 --asset-type bank
```

写操作 stdout 均为 JSON。改账前先 `list` 定位 id。不要调用 Dashboard 的 `/api/*`，一律走 CLI。

## 流水改删 SOP

先 `list` 拿到 `id`，再改或删。不能编造 id。

```bash
python3 scripts/bookkeeping.py entry-update 12 --category "交通/加油充电" --note "含押金"
python3 scripts/bookkeeping.py entry-delete 12
```

`entry-update` 可改：`type`、`amount`、`account`、`target-account`、`category`、`transaction-object`、`note`、`--time`、`--bill-source`、`--activity-id`。删支出会自动解开指向它的退款关联，退款本身保留。

## 退款关联 SOP

退款**不是收入**。微信/支付宝导出常把退款记成入账，原支出还在——必须把退款挂回原支出，支出只记净额。例：充电扣 30、退 28.40，关联后净花费 1.60。未关联退款不进收支。

导入末尾会自动关联（≥90% 置信）。对不上的用手动命令：

```bash
python3 scripts/bookkeeping.py refund-auto-link
python3 scripts/bookkeeping.py refund-candidates --merchant 充电桩 --amount 28.40
python3 scripts/bookkeeping.py refund-link --refund-id 20 --expense-id 12
python3 scripts/bookkeeping.py refund-unlink --refund-id 20
```

禁止把退款记成 `type=收入`。跨月退款也挂原支出，冲减归原支出的日期和分类。

## 活动花费 SOP

分类回答「餐饮花了多少」。活动回答「这件事花了多少」（旅行、球赛）。一笔账单最多属于一个活动；统计是净额（支出 − 已关联退款）；退款不单独挂活动，跟原支出走。归档后不能再往里塞账单。

```bash
python3 scripts/bookkeeping.py activity-add --name 三亚旅行 --start-date 2026-10-01 --end-date 2026-10-07
python3 scripts/bookkeeping.py activity-list
python3 scripts/bookkeeping.py entry-activity --entry-id 12 --entry-id 15 --activity-id 3
python3 scripts/bookkeeping.py activity-entries --id 3
python3 scripts/bookkeeping.py entry-activity --entry-id 12
python3 scripts/bookkeeping.py activity-update --id 3 --name 国庆出游
python3 scripts/bookkeeping.py activity-archive --id 3
python3 scripts/bookkeeping.py activity-archive --id 3 --restore
python3 scripts/bookkeeping.py activity-delete --id 3
```

`entry-activity` 不带 `--activity-id` 表示摘除。删除活动只摘归属，账单保留。不要为了活动去改分类。

## 资产账户 SOP

账户用 CLI 建和改，不要只改 `config.yaml`。`--asset-type`：`cash` / `bank` / `virtual` / `custom`。投资账户不要用这些命令建（V1 理财页关闭）。

```bash
python3 scripts/bookkeeping.py account-add --name 招行储蓄卡
python3 scripts/bookkeeping.py account-set "招行储蓄卡" --balance 18500 --asset-type bank
python3 scripts/bookkeeping.py account-set 花呗 --kind liability --balance -200 --expense-mode consume
python3 scripts/bookkeeping.py account-rename --old 招行储蓄卡 --new 招行储蓄卡(4985)
python3 scripts/bookkeeping.py accounts
python3 scripts/bookkeeping.py list --limit 50
python3 scripts/bookkeeping.py account-archive 花呗
python3 scripts/bookkeeping.py account-archive 花呗 --restore
python3 scripts/bookkeeping.py account-delete 测试账户
```

查某账户关联账单：`list` 看 `account` 列，或 Dashboard 资产页点「明细」。只有债权/负债能归档。`account-set --balance` 是设账户当前余额（建仓），账实不符用 `correct-balance`。花呗消费记支出且 `account=花呗`；还款是转账转到 `花呗`，不要再记一笔支出。

## 单笔记账 SOP

普通新增必须确认：`amount`、`type`、`account`、`category`。不能编造金额、账户或分类。

内部转账只在确认两个账户都是用户自有账户时使用 `transfer` 或 `type=转账`。给他人转账、收到他人转账、银行汇款、平台退款等外部资金流不要当作内部转账。

账实不符时使用：

```bash
python3 scripts/bookkeeping.py correct-balance "账户名" --actual 1200 --note "原因"
```

不要用普通新增模拟余额修正。

## 债权负债 SOP

债权（别人欠我）和负债（我欠别人）用 `accounts` 表里的专用账户建模：负债余额为负数表示欠款，债权余额为正数表示对方欠我。`花呗`/`抖音月付`/`京东白条` 等信用支付字样的账户在 init_db 时自动标记为负债。

关联机制是"账户即关联"，不需要额外的关联操作：

- 信用消费（如花呗买单）：记支出，`account` 填 `花呗`，余额自动更负。
- 还款：记转账，从资金账户转到 `花呗`，余额自动回升。Dashboard 负债行的「还款」按钮直接预填快速记账转账；已导入的还款流水想挂到该账户，在账单页把该笔的转入账户改成 `花呗` 即可。
- 向某人借钱：记转账，从缓冲账户 `借款-XX` 转到资金账户；还钱反向。`借入/借出/还款/收款` 旧类型由 `debt_accounts.migrate_debt_entries` 在 init_db 时自动迁移为转账。
- 剩余应收/应付金额 = 账户余额，自动计算，负债显示负数（-3866.65 = 还欠 3866.65）。
- **填入欠款/应收数额**（鲨鱼记账式）：Dashboard 债权/负债行点 ✎ 填正数，前端自动转换符号走 `correct-balance`（负债→负余额、债权→正余额），生成一笔「余额修正」流水=建仓/调整记录，明细里可见、流水加总=余额。资产账户 ✎ 仍是普通余额修正。

Dashboard 账户页「债权负债」区：负债/债权分组显示、组头小计、各组底部可自定义添加（名称+金额）；还清（余额 0）显示 ✓已还清/已收齐徽章；「归档」把结清账户移入底部「已归档债权/负债」折叠区，流水保留，支持 明细（跳账单页按账户筛选）/恢复/删除。只有债权/负债账户可归档，资产账户不行。

## 原始账单导入 SOP

支付宝和微信是已知来源，只使用已有适配脚本清洗成标准 CSV：

```bash
python3 scripts/import_alipay_bill.py "/path/to/alipay.csv" --analyze-only
python3 scripts/import_wechat_bill.py "/path/to/wechat.xlsx" --analyze-only

python3 scripts/import_alipay_bill.py "/path/to/alipay.csv" \
  --account-map /tmp/alipay_account_map.json \
  --output /tmp/alipay_standard.csv

python3 scripts/import_wechat_bill.py "/path/to/wechat.xlsx" \
  --account-map /tmp/wechat_account_map.json \
  --output /tmp/wechat_standard.csv
```

账户映射遵循以下规则：

- 能高置信确认是同一账户时，AI 直接映射，不需要询问用户。例如：账单中的 `招商银行储蓄卡(4985)` 可以直接映射为本地已有账户 `招行储蓄卡4985`。
- 存在歧义时，必须询问用户确认。例如：账单中的 `中国银行` 是否应映射为本地已有账户 `中行工资卡`，不能自行判断。
- 无法确认且不影响导入时，保留账单中的支付方式名称，不要为了减少账户数量强行合并。

支付宝/微信适配脚本不负责正式导入，只输出标准 CSV。输出后和未知账单一样进入“标准 CSV 流程”。

未知来源账单必须先只读检查文件结构、sheet、表头、样本行、类型/账户/分类/金额方向分布。然后允许在 `/tmp` 临场编写一次性清洗脚本，输出标准 CSV。不要把未知来源的一次性清洗逻辑写入主 CLI 或 Dashboard。

标准 CSV 字段固定为：

```text
transaction_time,type,category_level1,category_level2,category,amount,currency,transaction_object,account,target_account,participant,note,source
```

清洗要求：

- `transaction_time` 使用 `YYYY-MM-DD HH:MM:SS`。
- `amount` 使用正数，单位元。
- `transaction_object` 统一放外部交易对象，订单号/流水号/支付方式优先放备注。
- `source` 标记流水来源渠道：支付宝/微信适配脚本自动填（支付宝→`支付宝`、微信→`微信`）；未知来源（银行流水、券商账单等）清洗时按渠道填名称（如 `招商银行`、`XX银行App`）；确实无法判断来源时留空，导入后在回复中提醒用户可在 Dashboard 编辑面板补充。
- 普通收支能高置信映射到当前合法分类才填 `category`；不确定留空。
- 只有确认两个账户都是自有账户时，才写 `type=转账` 和 `target_account`。

清洗完成后必须进入标准 CSV 流程，不要继续手写导入、去重或统计逻辑。

## 标准 CSV 导入 SOP

支付宝、微信和未知来源账单完成清洗后，都必须走同一套流程。Agent 应主动完成能可靠完成的判断，仅在语义或账户关系存在歧义时询问用户。

1. 检查标准 CSV：

```bash
python3 scripts/bookkeeping.py import-check /tmp/cleaned_bill.csv --output /tmp/bookkeeping_import_check.json
```

重点查看：金额是否有效，时间、类型和账户是否完整，内部转账是否缺目标账户，以及仍有多少账目缺少分类。

2. 做一次关键词候选分析。命令会先读取数据库已有规则，再一次性输出仍未分类的高频线索：

```bash
python3 scripts/bookkeeping.py keyword-candidates /tmp/cleaned_bill.csv \
  --output /tmp/bookkeeping_keyword_candidates.json
```

Agent 读取输出后，先按账目 `type` 区分收入、支出等类型，再梳理 `transaction_object`、`note` 和 `raw_category` 中的候选。关键词判断必须收缩，按以下顺序处理：

- 优先检查 `transaction_object`。只有交易对象本身能稳定代表同一类消费时，才直接沉淀规则。例如：明确的餐厅、医院、交通服务或长期稳定商户。
- 如果交易对象是综合平台或含义宽泛的渠道，例如 `美团`、`京东`、`淘宝`等，禁止直接用平台名沉淀无条件分类规则。先查看该交易对象下的 `note` 明细，确认实际商户或消费场景。
- `note` 中可能包含订单号、流水号、时间戳等动态信息，导致相同商户被拆散。分析时允许临时做正则归一化以便聚合，但不得修改标准 CSV 中保留的原始 `note`。
- 从平台订单中提取稳定、具体的品牌或商户名（如“肯德基”“王记手撕大排骨”），不要使用“饭店”“订单”“消费”等过宽或临时的词。
- 只有含义明确、以后遇到同类账目仍然成立、且能够可靠映射到当前合法分类的关键词，才写入数据库。无法可靠聚合、无法判断语义或只出现一次的账目保持未分类，交给用户在 Dashboard 手工确认。

涉及到对个人的交易，不要提取关键词，但要在回复中提醒用户，与此人有多次交易，可移步 Dashboard 按照人名创建规则。不要把个人昵称、平台名称、订单号、流水号、一次性商品标题、完整备注或含义过宽的词写成长期规则。不确定时留给人工确认，不要猜。


3. 把本次分析得到的每条高置信、可复用规则写入数据库：

```bash
python3 scripts/bookkeeping.py rule-add 地铁 --category "交通/公共交通" --type 支出
```

这一步是 Agent 必须完成的长期知识沉淀。不能只在当前 CSV 中补分类，也不能只生成临时规则文件。数据库中的规则会用于后续导入，并会自动尝试处理历史待确认账目。

规则入库后用以下命令检查本轮规则是否已保存（可选）：

```bash
python3 scripts/bookkeeping.py rule-list
```

4. 同时把本轮规则整理到临时 JSON，用于补全当前标准 CSV。没有新规则时写空数组：

```json
[]
```

有高置信规则时使用这个格式：

```json
[
  {"keyword": "地铁", "category": "交通/公共交通", "type": "支出"}
]
```

应用本轮规则：

```bash
python3 scripts/bookkeeping.py apply-keyword-rules /tmp/cleaned_bill.csv \
  --keyword-rules /tmp/bookkeeping_keyword_rules.json \
  --output /tmp/cleaned_bill_with_rules.csv
```

5. 对补全后的 CSV 重新执行检查：

```bash
python3 scripts/bookkeeping.py import-check /tmp/cleaned_bill_with_rules.csv \
  --output /tmp/bookkeeping_import_check.json
```

关键词候选只分析一次。剩余空分类允许进入待确认，不要为了减少待确认数量而过度归类。

6. 生成重复候选：

```bash
python3 scripts/bookkeeping.py import-duplicates /tmp/cleaned_bill_with_rules.csv --output /tmp/bookkeeping_duplicate_candidates.json
```

7. Agent 只判断高置信账户别名，写入映射文件。没有高置信映射也写空对象：

```json
{}
```

映射方向是“新导入账户名 -> 数据库已有账户名”。

8. 正式导入：

```bash
python3 scripts/bookkeeping.py import /tmp/cleaned_bill_with_rules.csv --duplicate-account-map /tmp/bookkeeping_duplicate_account_map.json
```

导入命令末尾会自动执行退款智能关联：对未关联退款按证据打分（金额、商户、账户、时间、部分退款比例），最优与次优候选分差对应置信度 ≥90% 时自动连上，并继承原支出分类；唯一候选需绝对分达标，防止“流水里只有退款没有扣款”时误连。结果在导入输出的 `refund_auto_link` 字段：`auto`（已自动连）、`manual`（置信不足留人工）、`no_candidate`（无匹配扣款，可能是原支出在导入范围外）。

9. 导入后查看待确认摘要：

```bash
python3 scripts/bookkeeping.py unconfirmed-summary
```

也可以随时手动补跑一轮自动关联（幂等，只处理未关联退款）：

```bash
python3 scripts/bookkeeping.py refund-auto-link
```

10. 退款关联收尾（必须执行，不能只报告让用户自己处理）。导入输出中 `refund_auto_link` 字段非空、或本次导入的账单含 `type=退款` 时，Agent 逐类跟进：

    - `auto`（已自动连）：抽查核对连上的原支出是否合理（金额/商户对得上即可），无需逐条打扰用户。
    - `manual`（置信不足）：对每笔跑 `refund-candidates --merchant <交易对象> --amount <金额>`，人工核对候选。候选中存在**金额相同且商户一致**的唯一命中时，直接 `refund-link` 连上；存在多个可能候选或金额对不上时，列出候选（id、金额、商户、日期）让用户选择，不要猜。
    - `no_candidate`（无匹配扣款）：说明原支出不在本次导入范围（如更早期的账单或另一来源），在回复中告知用户，询问是否补录原支出后再关联，不要静默放过。

    处理完后跑一遍 `unconfirmed-summary` 核对无遗漏，并在最终回复中汇总退款关联结果：自动关联 N 笔、Agent 手动关联 N 笔、待用户确认 N 笔（附金额与交易对象），让用户一眼看清哪些退款已净额化、哪些还需要他决定。

11. 导入完成后启动 Dashboard，把剩余事项交给人工确认。如果本次导入的账目存在空 `source`（来源未知），在回复中提醒用户：来源信息可在 Dashboard 账单编辑面板的「来源」栏补充，账单页表头可按「(无来源)」筛出这些账目。用户也可随时在 Dashboard「待确认」页点「一键自动关联退款」重跑并查看报告。

## 待确认事项 SOP

先看摘要，再处理具体记录：

```bash
python3 scripts/bookkeeping.py unconfirmed-summary
python3 scripts/bookkeeping.py unconfirmed
```

常见处理：

```bash
python3 scripts/bookkeeping.py confirm 3 --category "餐饮/饮料"
python3 scripts/bookkeeping.py confirm 3 --category "餐饮/饮料" --learn
python3 scripts/bookkeeping.py rule-add 星巴克 --category "餐饮/饮料" --type 支出
```

只有规则稳定、可泛化时才 `--learn` 或 `rule-add`。Agent 在批量导入阶段发现可靠关键词时应主动使用 `rule-add`；人工处理单条待确认记录时，只有确认该交易对象可以代表同类账目，才使用 `--learn`。

如果用户明确需要新分类，先用 Dashboard 或分类管理命令新增分类，再确认记录。

## Dashboard

```bash
python3 scripts/bookkeeping.py dashboard --host 127.0.0.1 --port 8765
```

导入最后启动Dashboard，提醒用户打开 `http://127.0.0.1:8765`。

## 安全

- 账单文件和数据库保持本地。
- 修改或删除账目前，先用 `list` 或只读查询定位目标记录。
- 撤销导入时，不要删除或重写已学习的关键词规则。
- 退款、活动、账户的增删改走 CLI，不要手写 SQL，不要 curl `/api/*`。
