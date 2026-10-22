"""负债缓冲账户模块（净额制记账的核心）。

设计原则：
- 借款/还款/信用消费不产生收入或支出，只通过"缓冲账户"走账；
- 负债账户（liability）余额用负数表示欠款：花呗 = -100 表示欠 100；
- 应收账户（receivable）余额用正数表示别人欠我的钱；
- 统计口径：收入 = 收入；支出 = 支出 - 退款；
  转账/借入/借出/还款/收款 均为债务管道，不计收支。

本模块供 bookkeeping.py 在 init_db 时调用，所有迁移幂等可重复执行。
"""

import re

KIND_ASSET = "asset"
KIND_LIABILITY = "liability"
KIND_RECEIVABLE = "receivable"

# 信用支付方式：账单收/付款方式含这些字样时，记账到同名负债账户
CREDIT_PAYMENT_PATTERNS = ("花呗", "抖音月付", "京东白条", "美团月付", "分付", "信用卡")

# 记账口径（负债账户）：consume=花钱记支出（信用卡型/借款型）；repay=月供转账记支出（贷款型）
EXPENSE_CONSUME = "consume"
EXPENSE_REPAY = "repay"

# 还款口径计入支出的转账分类。
# ⚠️ 已核实（2026-10-21，真实 DB 只读查询）：entries.category 对转账类存的是
# 「平铺叶子名」（'转账'、'理财买入'、'信用卡还款'），不是 "转账/房贷" 路径格式；
# config.yaml 转账组下的组名即叶子名（组无叶子时 flatten_categories 直接产出组名）。
REPAY_TRANSFER_CATEGORIES = ("信用卡还款", "房贷", "车贷")

# 资产账户 asset_type 名称猜测（顺序敏感：先 bank 后 cash 后 virtual，都不中 → custom）
ASSET_TYPE_PATTERNS = [
    ("bank", ("储蓄卡", "银行", "卡")),
    ("cash", ("现金", "钱包")),
    ("virtual", ("余额宝", "零钱", "支付宝", "微信", "虚拟")),
]

# 参与收支统计的交易类型（债务管道与余额修正一律剔除）
STAT_INCOME_TYPES = ("收入",)
STAT_EXPENSE_TYPES = ("支出",)

# 需要迁移为转账的债务类型
DEBT_SOURCE_TYPES = ("借入", "借出", "还款", "收款")

_SIGNS = {
    "支出": -1,
    "收入": 1,
    "借入": 1,
    "借出": -1,
    "还款": -1,
    "收款": 1,
    "退款": 1,
    "余额修正": 1,
}


def is_credit_payment(text):
    text = str(text or "")
    return any(pattern in text for pattern in CREDIT_PAYMENT_PATTERNS)


def guess_expense_mode(name):
    """建负债账户时按名称猜默认记账口径，返回 (expense_mode, expense_mode_locked)。

    - 命中 CREDIT_PAYMENT_PATTERNS（花呗/白条/月付/分付/信用卡）→ 消费口径且写死；
    - 名字含 房贷/车贷 → 还款口径（月供转账记支出）；
    - 其余（借款/网贷等）→ 消费口径，可改。
    """
    if is_credit_payment(name):
        return EXPENSE_CONSUME, 1
    if any(key in str(name or "") for key in ("房贷", "车贷")):
        return EXPENSE_REPAY, 0
    return EXPENSE_CONSUME, 0


def guess_asset_type(name):
    """建资产账户时按名称猜 asset_type（宁缺勿错：都不中 → custom）。"""
    text = str(name or "")
    for asset_type, patterns in ASSET_TYPE_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return asset_type
    return "custom"


def derive_debt_type(expense_mode, expense_mode_locked):
    """三型呈现标签（只读派生，不落库）：信用卡型/借款型/贷款型。"""
    mode = str(expense_mode or "")
    locked = int(expense_mode_locked or 0) == 1
    if mode == EXPENSE_REPAY:
        return "贷款"
    if mode == EXPENSE_CONSUME:
        return "信用卡" if locked else "借款"
    return None


def is_credit_type_row(row):
    """信用卡型 = 消费口径且锁定（循环额度，无本金/利息概念，归档不写快照）。"""
    return (str(row["expense_mode"] or "") == EXPENSE_CONSUME
            and int(row["expense_mode_locked"] or 0) == 1)


def repay_mode_account(conn, name):
    """账户是否为还款口径（repay）负债账户。"""
    if not name:
        return False
    row = conn.execute(
        "select kind, expense_mode from accounts where name = ?", (str(name).strip(),)
    ).fetchone()
    return bool(row and (row["kind"] or "") == KIND_LIABILITY and row["expense_mode"] == EXPENSE_REPAY)


def enforce_no_expense_on_repay(conn, record):
    """防双计硬拦：还款口径负债账户上误记 type=支出 → 与月供转账确定性双计，必须拒绝。"""
    if str(record.get("type") or "") == "支出" and repay_mode_account(conn, record.get("account")):
        raise ValueError("还款口径账户不记支出，月供请用「转账」+分类「房贷/车贷」，否则会双重计支出")


def repay_transfer_warning(conn, record):
    """防漏计软提示：还款口径账户收到普通转账（分类不在还款类）→ 保存成功但提示。"""
    if str(record.get("type") or "") != "转账":
        return None
    target = str(record.get("target_account") or "").strip()
    if not repay_mode_account(conn, target):
        return None
    category = str(record.get("category") or "转账").strip()
    if category in REPAY_TRANSFER_CATEGORIES:
        return None
    return "该账户为还款口径，若此笔是月供请把分类改为「房贷/车贷」，否则不计入支出"


def _now_iso():
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _account_delta(tx_type, amount_cents, balance_delta_cents=None):
    """与 bookkeeping.account_balance_delta 同规则（本地副本，避免循环导入）。"""
    if tx_type == "转账":
        return -amount_cents
    direction = -1 if amount_cents < 0 else 1
    amount_cents = abs(amount_cents)
    if tx_type == "余额修正":
        if balance_delta_cents is not None:
            return int(balance_delta_cents) if direction > 0 else -int(balance_delta_cents)
    sign = _SIGNS.get(tx_type)
    if sign is None:
        return None
    return direction * sign * amount_cents


def _apply_balance(conn, account, tx_type, amount_cents, balance_delta_cents=None):
    if not account:
        return
    conn.execute(
        "select id from accounts where name = ?", (account,)
    ).fetchone() or _ensure_account_row(conn, account, KIND_ASSET, 0)
    delta = _account_delta(tx_type, amount_cents, balance_delta_cents)
    if delta is None:
        return
    conn.execute(
        "update accounts set current_balance_cents = current_balance_cents + ?, updated_at = ? where name = ?",
        (delta, _now_iso(), account),
    )


def _ensure_account_row(conn, name, kind, initial_cents):
    ts = _now_iso()
    conn.execute(
        """
        insert into accounts(name, initial_balance_cents, current_balance_cents, currency, kind, created_at, updated_at)
        values (?, ?, ?, 'CNY', ?, ?, ?)
        """,
        (name, initial_cents, initial_cents, kind, ts, ts),
    )


def ensure_account_kind(conn, name, kind, initial_cents=None):
    """确保账户存在并具有指定 kind；initial_cents 仅在新建时生效。"""
    row = conn.execute(
        "select kind, initial_balance_cents from accounts where name = ?", (name,)
    ).fetchone()
    if row is None:
        _ensure_account_row(conn, name, kind, initial_cents or 0)
        return
    if kind and (row["kind"] or KIND_ASSET) != kind:
        conn.execute("update accounts set kind = ?, updated_at = ? where name = ?", (kind, _now_iso(), name))


# 负债三型 + 资产管家合并迁移列（照抄 kind/archived 的幂等补列写法）
NEW_ACCOUNT_COLUMNS = [
    ("expense_mode", "text not null default ''"),
    ("expense_mode_locked", "integer not null default 0"),
    ("principal_cents", "integer"),
    ("archived_principal_cents", "integer"),
    ("archived_repay_cents", "integer"),
    ("archived_interest_cents", "integer"),
    ("asset_type", "text not null default ''"),
]


def ensure_schema(conn):
    """accounts 表补 kind/archived/负债三型/资产类型列；信用支付类账户自动标记为负债。"""
    cols = [row[1] for row in conn.execute("pragma table_info(accounts)")]
    if "kind" not in cols:
        conn.execute("alter table accounts add column kind text not null default 'asset'")
    if "archived" not in cols:
        # 债权/负债还清后的归档标记（参考 activities.archived）：0 在列，1 归档
        conn.execute("alter table accounts add column archived integer not null default 0")
    cols = [row[1] for row in conn.execute("pragma table_info(accounts)")]
    for column, decl in NEW_ACCOUNT_COLUMNS:
        if column not in cols:
            conn.execute(f"alter table accounts add column {column} {decl}")
    rows = conn.execute("select name from accounts where kind = 'asset'").fetchall()
    for row in rows:
        if is_credit_payment(row["name"]):
            conn.execute(
                "update accounts set kind = ?, updated_at = ? where name = ?",
                (KIND_LIABILITY, _now_iso(), row["name"]),
            )
    # 回填放在 kind 归位之后：保证信用支付类账户当次就拿到 expense_mode（幂等）
    backfill_new_columns(conn)


def backfill_new_columns(conn):
    """存量账户回填（幂等，只填空值）：

    - expense_mode/locked：kind=liability 且 expense_mode='' → 按 guess_expense_mode 填；
    - asset_type：kind=asset 且 asset_type='' → 按 guess_asset_type 填（顺序 bank→cash→virtual，宁缺勿错）。
    principal_cents 一律不猜（NULL 由用户补填，未填不影响统计只影响利息）。
    """
    rows = conn.execute(
        "select name from accounts where kind = ? and expense_mode = ''", (KIND_LIABILITY,)
    ).fetchall()
    for row in rows:
        mode, locked = guess_expense_mode(row["name"])
        conn.execute(
            "update accounts set expense_mode = ?, expense_mode_locked = ?, updated_at = ? where name = ? and expense_mode = ''",
            (mode, locked, _now_iso(), row["name"]),
        )
    rows = conn.execute(
        "select name from accounts where kind = ? and asset_type = ''", (KIND_ASSET,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "update accounts set asset_type = ?, updated_at = ? where name = ? and asset_type = ''",
            (guess_asset_type(row["name"]), _now_iso(), row["name"]),
        )


def _short_party(transaction_object):
    """从交易对象提取短名：'爸爸 (陈德深)' -> '爸爸'。"""
    text = str(transaction_object or "").strip()
    if not text:
        return ""
    return re.split(r"[\s（(]", text, 1)[0]


def _resolve_liability_account(conn, name):
    """候选名精确命中已有负债账户时返回 (账户名, kind)，否则 None。

    只认 liability：避免把还款/借入错挂到同名资产账户上。
    """
    if conn is None or not name:
        return None
    row = conn.execute(
        "select kind from accounts where name = ? and kind = ?",
        (name, KIND_LIABILITY),
    ).fetchone()
    if row is None:
        return None
    return name, KIND_LIABILITY


def _buffer_name_for(row, conn=None):
    """根据债务记录决定缓冲账户名与 kind。

    借入/还款优先落到真实负债账户：候选（交易对象短名、分类）依次尝试
    精确命中已有负债账户；"XX还款"先去掉"还款"后缀再匹配（花呗还款→花呗），
    避免按分类新建出"花呗还款"这类碎片缓冲账户。
    """
    tx_type = row["type"]
    category = str(row["category"] or "").strip()
    party = _short_party(row["transaction_object"])
    if tx_type in ("借入", "还款"):
        for candidate in (party, category):
            if not candidate:
                continue
            hit = _resolve_liability_account(conn, candidate)
            if hit:
                return hit
            stripped = candidate[:-2] if candidate.endswith("还款") else ""
            if stripped:
                hit = _resolve_liability_account(conn, stripped)
                if hit:
                    return hit
                if is_credit_payment(stripped):
                    return stripped, KIND_LIABILITY
            if is_credit_payment(candidate):
                return candidate, KIND_LIABILITY
        return f"借款-{party or '未命名'}", KIND_LIABILITY
    return f"借出-{party or '未命名'}", KIND_RECEIVABLE


def migrate_debt_entries(conn):
    """把 借入/借出/还款/收款 记录迁移为转账 + 缓冲账户走账（幂等）。

    - 还款：原账户付款 -> 借款-XX（负债），缓冲账户初始 = -累计还款（表示账本前已借入）
    - 收款（借款收回）：借出-XX（应收）-> 原账户，缓冲账户初始 = +累计收款（表示账本前已借出）
    - 借入：借款-XX（负债）-> 原账户，初始 0，借入后余额变负
    - 借出：原账户 -> 借出-XX（应收），初始 0，借出后余额变正
    迁移前后资产账户余额净变化为零（只改债务表达方式）。
    """
    rows = conn.execute(
        f"""
        select * from entries
        where type in ({','.join('?' for _ in DEBT_SOURCE_TYPES)})
        order by transaction_time, id
        """,
        DEBT_SOURCE_TYPES,
    ).fetchall()
    if not rows:
        return 0

    # 先按缓冲账户汇总"账本前本金"，用于初始化缓冲账户余额
    principals = {}
    buffers = {}
    kinds = {}
    for row in rows:
        buffer_name, kind = _buffer_name_for(row, conn)
        buffers[row["id"]] = buffer_name
        kinds[buffer_name] = kind
        sign = -1 if row["type"] == "还款" else (1 if row["type"] == "收款" else 0)
        if sign:
            principals[buffer_name] = principals.get(buffer_name, 0) + sign * row["amount_cents"]
    # 所有缓冲账户都要确保存在且 kind 正确（不只还款/收款）：
    # 借入/借出的本金 sign=0 不进 principals，但用户可能把借入直接记在
    # "借款-XX"账户上，ensure_account 按默认建成了 kind=asset，这里纠正回负债/债权
    for buffer_name in kinds:
        kind = kinds.get(buffer_name)
        if kind is None:
            kind = KIND_LIABILITY if buffer_name.startswith("借款-") or is_credit_payment(buffer_name) else KIND_RECEIVABLE
        ensure_account_kind(conn, buffer_name, kind, initial_cents=principals.get(buffer_name, 0))

    changed = 0
    for row in rows:
        buffer_name = buffers[row["id"]]
        old_type = row["type"]
        old_amount = row["amount_cents"]
        old_delta = row["balance_delta_cents"]
        # 回滚旧类型的余额影响
        _apply_balance(conn, row["account"], old_type, -old_amount, -(old_delta or 0))
        note_suffix = f"（原{old_type}·{row['category'] or '未分类'}）"
        new_note = f"{row['note'] or ''}{note_suffix}"
        if old_type in ("还款", "收款"):
            # 还款：原账户 -> 缓冲；收款：缓冲 -> 原账户
            if old_type == "还款":
                src, dst = row["account"], buffer_name
            else:
                src, dst = buffer_name, row["account"]
        else:
            # 借入：缓冲 -> 原账户；借出：原账户 -> 缓冲
            if old_type == "借入":
                src, dst = buffer_name, row["account"]
            else:
                src, dst = row["account"], buffer_name
        conn.execute(
            """
            update entries set type = '转账', category = '转账', account = ?, target_account = ?, note = ?
            where id = ?
            """,
            (src, dst, new_note, row["id"]),
        )
        # 应用新转账的余额影响：转出 -X，转入 +X
        _apply_balance(conn, src, "转账", old_amount)
        _apply_balance(conn, dst, "收入", old_amount)
        changed += 1

    # 债务已由缓冲账户表达，旧债权债务视图清零
    conn.execute("update account_debts set receivable_cents = 0, payable_cents = 0, updated_at = ?", (_now_iso(),))
    return changed


def run_all(conn):
    """init_db 入口：建列 + 信用账户标记 + 债务迁移 + 二次回填（覆盖迁移新建的缓冲账户）。"""
    ensure_schema(conn)
    changed = migrate_debt_entries(conn)
    # migrate_debt_entries 可能新建负债/债权缓冲账户，再跑一次回填保证口径不落空（幂等）
    backfill_new_columns(conn)
    return changed
