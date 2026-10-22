#!/usr/bin/env python3
import argparse
import collections
import contextlib
import csv
import datetime as dt
import io
import json
import math
import mimetypes
import os
import re
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import debt_accounts
import investments
import quotes as invest_quotes

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "scripts" / "bookkeeping_app"
DEFAULT_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "bookkeeping"
DB_PATH = Path(os.environ.get("BOOKKEEPING_DB_PATH", DEFAULT_DATA_DIR / "bookkeeping.db")).expanduser()
CONFIG_PATH = ROOT / "scripts" / "config.yaml"
STATIC_PATH = APP_PATH / "static"
INDEX_PATH = STATIC_PATH / "index.html"

ENTRY_TYPES = {"支出", "收入", "转账", "借入", "借出", "还款", "收款", "退款", "余额修正"}
BALANCE_SIGNS = {
    "支出": -1,
    "收入": 1,
    "借入": 1,
    "借出": -1,
    "还款": -1,
    "收款": 1,
    "退款": 1,
    "余额修正": 1,
}
IMPORT_FIELD_ALIASES = {
    "transaction_time": {
        "transaction_time",
        "time",
        "date",
        "datetime",
        "交易时间",
        "交易日期",
        "记账时间",
        "记账日期",
        "账单时间",
        "发生时间",
        "发生日期",
        "入账时间",
        "入账日期",
        "创建时间",
        "支付时间",
        "付款时间",
        "收款时间",
    },
    "target_account": {
        "target_account",
        "to_account",
        "transfer_account",
        "target",
        "转入账户",
        "收款账户",
        "对方账户",
        "目标账户",
    },
    "transaction_object": {
        "transaction_object",
        "交易对象",
        "交易对方",
        "counterparty",
        "merchant",
        "商家",
        "商户",
        "收付款人",
        "交易方",
        "对方",
    },
    "bill_source": {
        "bill_source",
        "source",
        "来源",
        "流水来源",
        "账单来源",
    },
}


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def cents(value):
    if value is None or str(value).strip() == "":
        raise ValueError("amount is required")
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    return int(round(float(text) * 100))


def money(cents_value):
    return f"{(cents_value or 0) / 100:.2f}"


def parse_attributes(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def account_balance_delta(tx_type, amount_cents, balance_delta_cents=None):
    if tx_type == "转账":
        return -amount_cents
    direction = -1 if amount_cents < 0 else 1
    amount_cents = abs(amount_cents)
    if tx_type == "余额修正":
        if balance_delta_cents is not None:
            return int(balance_delta_cents) if direction > 0 else -int(balance_delta_cents)
    sign = BALANCE_SIGNS.get(tx_type)
    if sign is None:
        return None
    return direction * sign * amount_cents


ENTRY_COLUMNS = [
    "id",
    "transaction_time",
    "type",
    "category",
    "status",
    "amount_cents",
    "currency",
    "transaction_object",
    "account",
    "target_account",
    "participant",
    "balance_delta_cents",
    "actual_balance_cents",
    "note",
    "refund_pair_id",
    "activity_id",
    "bill_source",
]

UNCONFIRMED_COLUMNS = ENTRY_COLUMNS + ["raw_payload"]


def set_database_path(value):
    global DB_PATH
    if value:
        DB_PATH = Path(value).expanduser()


def migrate_table(conn, table, columns):
    existing = conn.execute(f"pragma table_info({table})").fetchall()
    existing_names = [row["name"] for row in existing]
    if not existing_names or existing_names == columns:
        return
    stale_table = f"{table}_old"
    stale_exists = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (stale_table,),
    ).fetchone()
    if stale_exists:
        current_count = conn.execute(f"select count(*) from {table}").fetchone()[0]
        stale_columns = [row["name"] for row in conn.execute(f"pragma table_info({stale_table})").fetchall()]
        stale_count = conn.execute(f"select count(*) from {stale_table}").fetchone()[0]
        if current_count == 0 and stale_count > 0 and stale_columns == columns:
            conn.execute(f"drop table {table}")
            conn.execute(f"alter table {stale_table} rename to {table}")
            return
    rows = conn.execute(f"select * from {table}").fetchall()
    temp_table = stale_table
    suffix = 1
    while conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (temp_table,)).fetchone():
        temp_table = f"{table}_old_{suffix}"
        suffix += 1
    conn.execute(f"alter table {table} rename to {temp_table}")
    create_entries_table(conn, table)
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f"insert into {table}({', '.join(columns)}) values ({placeholders})"
    for row in rows:
        old = dict(row)
        attrs = parse_attributes(old.get("attributes"))
        values = []
        for column in columns:
            if column == "target_account":
                values.append(old.get("target_account") or old.get("to_account"))
            elif column == "transaction_object":
                values.append(old.get("transaction_object") or old.get("merchant") or old.get("counterparty"))
            elif column == "balance_delta_cents":
                values.append(old.get("balance_delta_cents") if "balance_delta_cents" in old else attrs.get("delta_cents"))
            elif column == "actual_balance_cents":
                values.append(old.get("actual_balance_cents") if "actual_balance_cents" in old else attrs.get("actual_balance_cents"))
            elif column == "status":
                values.append(old.get("status") if "status" in old else ("pending" if table == "unconfirmed_entries" else "confirmed"))
            elif column in old:
                values.append(old.get(column))
            elif column == "currency":
                values.append("CNY")
            elif column == "participant":
                values.append("自己")
            else:
                values.append(None)
        conn.execute(insert_sql, values)
    conn.execute(f"drop table {temp_table}")


def create_entries_table(conn, table):
    raw_payload = ", raw_payload text" if table == "unconfirmed_entries" else ""
    status_default = "pending" if table == "unconfirmed_entries" else "confirmed"
    conn.execute(
        f"""
        create table if not exists {table} (
            id integer primary key autoincrement,
            transaction_time text not null,
            type text,
            category text,
            status text not null default '{status_default}',
            amount_cents integer not null,
            currency text not null default 'CNY',
            transaction_object text,
            account text not null default '',
            target_account text,
            participant text not null default '自己',
            balance_delta_cents integer,
            actual_balance_cents integer,
            note text,
            refund_pair_id integer,
            activity_id integer,
            bill_source text{raw_payload}
        )
        """
    )


_KEEPALIVE_CONN = None


def connect():
    global _KEEPALIVE_CONN
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 哨兵连接：WAL 模式下"最后一个连接关闭"会触发 checkpoint（fsync ~100ms+）。
    # 服务端每个请求都新开/关闭连接，若无常驻连接，每次 close 都要付一次 checkpoint。
    # 进程内保留一个空闲连接，使请求级连接关闭时永远不是"最后一个"。
    if _KEEPALIVE_CONN is None:
        _KEEPALIVE_CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _KEEPALIVE_CONN.execute("pragma journal_mode = wal")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    # WAL + synchronous=NORMAL：写操作不再每次 commit 都全量 fsync，
    # 单次写入从 ~70ms 降到 <1ms，且掉电只可能丢最后一个事务不损坏库
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    return conn


def migrate_category_only_unconfirmed(conn):
    rows = conn.execute(
        """
        select *
        from unconfirmed_entries
        where coalesce(account, '') != ''
            and coalesce(type, '') != ''
            and amount_cents != 0
            and type not in ('转账', '余额修正')
            and coalesce(category, '') = ''
        order by transaction_time, id
        """
    ).fetchall()
    for row in rows:
        ensure_account(conn, row["account"], row["currency"])
        conn.execute(
            """
            insert into entries(transaction_time, type, category, status, amount_cents, currency, transaction_object,
                account, target_account, participant, balance_delta_cents, actual_balance_cents, note, refund_pair_id)
            values (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["transaction_time"],
                row["type"],
                row["category"],
                row["amount_cents"],
                row["currency"],
                row["transaction_object"],
                row["account"],
                row["target_account"],
                row["participant"],
                row["balance_delta_cents"],
                row["actual_balance_cents"],
                row["note"],
                row["refund_pair_id"] if "refund_pair_id" in row.keys() else None,
            ),
        )
        update_account_balance(conn, row["account"], row["type"], row["amount_cents"], row["balance_delta_cents"])
        conn.execute("delete from unconfirmed_entries where id = ?", (row["id"],))


def paired_refund_category(conn, obj, ttime):
    """Find the category of the most recent expense at/before the refund from the same merchant."""
    obj = str(obj or "").strip()
    if not obj:
        return "退款"
    row = conn.execute(
        """
        select category from entries
        where type = '支出' and transaction_object = ? and transaction_time <= ?
            and coalesce(category, '') != ''
        order by transaction_time desc, id desc limit 1
        """,
        (obj, ttime),
    ).fetchone()
    return row["category"] if row else "退款"


def migrate_refund_type(conn):
    """退款不是收入：把存量 收入/退款 记录迁移为独立 退款 类型，并按商户配对继承原支出分类。"""
    changed = False
    for table in ("entries", "unconfirmed_entries"):
        rows = conn.execute(
            f"select id, transaction_time, transaction_object from {table} where type = '收入' and coalesce(category, '') = '退款'"
        ).fetchall()
        for row in rows:
            category = paired_refund_category(conn, row["transaction_object"], row["transaction_time"])
            conn.execute(f"update {table} set type = '退款', category = ? where id = ?", (category, row["id"]))
            changed = True
    return changed


def init_db():
    conn = connect()
    conn.executescript(
        """
        create table if not exists accounts (
            id integer primary key autoincrement,
            name text not null unique,
            initial_balance_cents integer not null default 0,
            current_balance_cents integer not null default 0,
            currency text not null default 'CNY',
            note text,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists merchant_category_rules (
            id integer primary key autoincrement,
            merchant text not null unique,
            category text not null,
            type text,
            created_at text not null
        );

        create table if not exists activities (
            id integer primary key autoincrement,
            name text not null unique,
            start_date text,
            end_date text,
            note text,
            archived integer not null default 0,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists account_debts (
            id integer primary key autoincrement,
            name text not null unique,
            receivable_cents integer not null default 0,
            payable_cents integer not null default 0,
            currency text not null default 'CNY',
            visible integer not null default 1,
            created_at text not null,
            updated_at text not null,
            hidden_at text
        );

        """
    )
    create_entries_table(conn, "entries")
    create_entries_table(conn, "unconfirmed_entries")
    migrate_table(conn, "entries", ENTRY_COLUMNS)
    migrate_table(conn, "unconfirmed_entries", UNCONFIRMED_COLUMNS)
    conn.execute("drop table if exists import_batches")
    conn.execute("create index if not exists idx_entries_time on entries(transaction_time)")
    conn.execute("create index if not exists idx_entries_category on entries(category)")
    conn.execute("create index if not exists idx_entries_account on entries(account)")
    conn.execute("create index if not exists idx_entries_target_account on entries(target_account)")
    conn.execute("create index if not exists idx_entries_transaction_object on entries(transaction_object)")
    conn.execute("create index if not exists idx_unconfirmed_transaction_object on unconfirmed_entries(transaction_object)")
    conn.execute("create index if not exists idx_account_debts_visible on account_debts(visible)")
    conn.execute("create index if not exists idx_entries_activity on entries(activity_id)")
    # activities 归档列：老库幂等补列
    activity_cols = [row["name"] for row in conn.execute("pragma table_info(activities)").fetchall()]
    if activity_cols and "archived" not in activity_cols:
        conn.execute("alter table activities add column archived integer not null default 0")
    # 流水来源列：老库幂等补列（bill_source 记录账单来自哪个渠道：微信/支付宝/银行App 等）
    for table in ("entries", "unconfirmed_entries"):
        cols = [row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()]
        if cols and "bill_source" not in cols:
            conn.execute(f"alter table {table} add column bill_source text")
    # 退款不参与分类：清理历史上误建的退款类型关键词规则（幂等）
    conn.execute("delete from merchant_category_rules where type = '退款'")
    migrate_category_only_unconfirmed(conn)
    migrate_refund_type(conn)
    debt_accounts.run_all(conn)
    investments.run_all(conn)
    sync_account_debts_from_entries(conn)
    # 还款口径月供分类基建（add_category 自带存在性检查，天然幂等）：
    # 转账组无叶子 → entries.category 存平铺叶子名（已核实：'转账'/'理财买入'/'信用卡还款'）
    add_category("转账", "房贷")
    add_category("转账", "车贷")
    conn.commit()
    conn.close()


def ensure_account(conn, name, currency="CNY"):
    if not name:
        raise ValueError("account is required")
    row = conn.execute("select id from accounts where name = ?", (name,)).fetchone()
    if row:
        return
    ts = now_iso()
    conn.execute(
        """
        insert into accounts(name, initial_balance_cents, current_balance_cents, currency, created_at, updated_at)
        values (?, 0, 0, ?, ?, ?)
        """,
        (name, currency, ts, ts),
    )


def known_own_account_names(conn):
    names = set(config_account_names())
    names.update(row["name"] for row in conn.execute("select name from accounts").fetchall())
    return names


def set_account(name, balance=None, currency="CNY", note=None, kind=None,
                expense_mode=None, principal=None, asset_type=None):
    """建户/改户（幂等 upsert）。

    负债三型 / 资产管家扩展参数：
    - expense_mode: consume/repay（仅负债账户有意义）；锁定账户传不同值 → 400「该账户类型已锁定」
    - principal: 本金（元，借款型/贷款型适用）；信用卡型（锁定）→ 400「没有本金概念」
    - asset_type: cash/bank/virtual/custom（仅资产账户）；invest → 400「投资账户请在理财页添加」
    balance=None 时既有账户余额/备注不动（仅更新显式传入的扩展字段），新账户按 0 建户。
    """
    init_db()
    name = str(name or "").strip()
    if not name:
        raise ValueError("account name is required")
    ts = now_iso()
    conn = connect()
    try:
        row = conn.execute("select * from accounts where name = ?", (name,)).fetchone()
        existing = dict(row) if row else None
        effective_kind = kind or (existing["kind"] if existing else None) or "asset"

        # ---- expense_mode（仅负债）----
        mode_value = None
        locked_value = None
        if expense_mode is not None and effective_kind != debt_accounts.KIND_LIABILITY:
            raise ValueError("expense_mode 仅适用于负债账户")
        if effective_kind == debt_accounts.KIND_LIABILITY:
            if expense_mode is not None and expense_mode not in (debt_accounts.EXPENSE_CONSUME, debt_accounts.EXPENSE_REPAY):
                raise ValueError("expense_mode must be consume or repay")
            if existing is None:
                if debt_accounts.is_credit_payment(name):
                    # 信用卡类命名：写死消费口径（前端「信用卡型」，锁定不可改）
                    if expense_mode not in (None, debt_accounts.EXPENSE_CONSUME):
                        raise ValueError("该账户类型已锁定")
                    mode_value, locked_value = debt_accounts.EXPENSE_CONSUME, 1
                else:
                    g_mode, g_locked = debt_accounts.guess_expense_mode(name)
                    mode_value = expense_mode or g_mode
                    locked_value = g_locked if expense_mode is None else 0
            else:
                cur_mode = existing["expense_mode"] or ""
                cur_locked = int(existing["expense_mode_locked"] or 0)
                if cur_locked and expense_mode is not None and expense_mode != cur_mode:
                    raise ValueError("该账户类型已锁定")
                if expense_mode is not None:
                    mode_value = expense_mode
                    locked_value = cur_locked if cur_locked else 0

        # ---- principal（仅借款型/贷款型负债）----
        principal_cents = None
        if principal not in (None, ""):
            if effective_kind != debt_accounts.KIND_LIABILITY:
                raise ValueError("principal 仅适用于负债账户")
            locked_now = locked_value if locked_value is not None else int((existing or {}).get("expense_mode_locked") or 0)
            if locked_now or debt_accounts.is_credit_payment(name):
                # 信用卡型（循环额度）没有本金概念——四层兜底第一层
                raise ValueError("信用卡型账户没有本金概念")
            principal_cents = cents(principal)
            # 设计（负债文档 §2）：本金为正数，NULL=未填；0/负数一律拒绝（填 0 多半是误操作）
            if principal_cents <= 0:
                raise ValueError("本金必须为正数")

        # ---- asset_type（仅资产）----
        asset_type_value = None
        if asset_type not in (None, ""):
            asset_type_value = str(asset_type).strip()
            if asset_type_value == "invest":
                raise ValueError("投资账户请在理财页添加")
            if asset_type_value not in ("cash", "bank", "virtual", "custom"):
                raise ValueError("asset_type must be one of cash/bank/virtual/custom")
            if effective_kind != debt_accounts.KIND_ASSET:
                raise ValueError("asset_type 仅适用于资产账户")

        # ---- 余额：None → 既有保留 / 新户 0 ----
        if balance in (None, ""):
            amount = existing["initial_balance_cents"] if existing else 0
        else:
            amount = cents(balance)

        if existing is None:
            if effective_kind == debt_accounts.KIND_ASSET and asset_type_value is None:
                asset_type_value = debt_accounts.guess_asset_type(name)
            conn.execute(
                """
                insert into accounts(name, initial_balance_cents, current_balance_cents, currency, note, kind,
                    expense_mode, expense_mode_locked, principal_cents, asset_type, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, amount, amount, currency, note, effective_kind,
                 mode_value or "", locked_value or 0, principal_cents, asset_type_value or "", ts, ts),
            )
        elif balance in (None, ""):
            # 只更新显式传入的扩展字段（口径/本金/类型纠正），余额与备注不动
            conn.execute(
                """
                update accounts set kind = ?,
                    expense_mode = coalesce(?, expense_mode),
                    expense_mode_locked = coalesce(?, expense_mode_locked),
                    principal_cents = coalesce(?, principal_cents),
                    asset_type = coalesce(?, asset_type),
                    updated_at = ?
                where name = ?
                """,
                (effective_kind, mode_value, locked_value, principal_cents, asset_type_value, ts, name),
            )
        else:
            conn.execute(
                """
                update accounts set initial_balance_cents = ?, current_balance_cents = ?, currency = ?, note = ?, kind = ?,
                    expense_mode = coalesce(?, expense_mode),
                    expense_mode_locked = coalesce(?, expense_mode_locked),
                    principal_cents = coalesce(?, principal_cents),
                    asset_type = coalesce(?, asset_type),
                    updated_at = ?
                where name = ?
                """,
                (amount, amount, currency, note, effective_kind, mode_value, locked_value,
                 principal_cents, asset_type_value, ts, name),
            )
        conn.commit()
    finally:
        conn.close()
    accounts = config_account_names()
    if name not in accounts:
        accounts.append(name)
        write_config_accounts(accounts)


def flatten_categories():
    if not CONFIG_PATH.exists():
        return []
    categories = []
    stack = []
    in_categories = False
    for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or raw.lstrip().startswith("#"):
            continue
        if len(raw) == len(raw.lstrip(" ")) and stripped.endswith(":"):
            in_categories = stripped == "categories:"
            stack = []
            continue
        if not in_categories:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        text = stripped
        level = indent // 2
        if text.startswith("- "):
            item = text[2:].strip()
            prefix = stack[-1][1] if stack else ""
            categories.append(f"{prefix}/{item}" if prefix else item)
            continue
        key = text.split(":", 1)[0].strip().strip('"').strip("'")
        stack = [s for s in stack if s[0] < level]
        if level >= 1:
            if stack and stack[-1][0] == level - 1 and stack[-1][1] in {"支出", "收入", "转账", "借入", "借出", "还款", "收款", "退款"}:
                value = key
            elif stack and stack[-1][1] not in {"支出", "收入", "转账", "借入", "借出", "还款", "收款", "退款"}:
                value = f"{stack[-1][1]}/{key}"
            else:
                value = key
            stack.append((level, value))
            if text.endswith("[]"):
                categories.append(value)
    return sorted(set(categories))


def category_tree():
    if not CONFIG_PATH.exists():
        return {}
    tree = {}
    current_type = None
    current_group = None
    in_categories = False
    for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or raw.lstrip().startswith("#"):
            continue
        if len(raw) == len(raw.lstrip(" ")) and stripped.endswith(":"):
            in_categories = stripped == "categories:"
            current_type = None
            current_group = None
            continue
        if not in_categories:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        text = stripped
        if indent == 2 and text.endswith(":"):
            current_type = text[:-1].strip().strip('"').strip("'")
            current_group = None
            tree.setdefault(current_type, {})
        elif indent == 4 and current_type:
            key = text.split(":", 1)[0].strip().strip('"').strip("'")
            current_group = key
            tree[current_type].setdefault(current_group, [])
        elif indent == 6 and current_type and current_group and text.startswith("- "):
            tree[current_type][current_group].append(text[2:].strip())
    # 退款的分类继承支出的分类树（关联原支出时分类本就继承自支出），
    # 否则编辑已确认退款时前端"分类必须属于该类型"校验必然失败
    if tree.get("支出"):
        tree["退款"] = dict(tree["支出"])
    return tree


def config_account_names():
    if not CONFIG_PATH.exists():
        return []
    names = []
    in_accounts = False
    for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or raw.lstrip().startswith("#"):
            continue
        if len(raw) == len(raw.lstrip(" ")) and stripped.endswith(":"):
            in_accounts = stripped == "accounts:"
            continue
        if in_accounts and stripped.startswith("- "):
            names.append(stripped[2:].strip())
    return names


def account_names():
    init_db()
    conn = connect()
    names = {row["name"] for row in conn.execute("select name from accounts").fetchall()}
    conn.close()
    return sorted(names)


def historical_account_names():
    init_db()
    conn = connect()
    names = set(account_names())
    for sql in [
        "select distinct account name from entries where account != ''",
        "select distinct target_account name from entries where target_account is not null and target_account != ''",
        "select distinct account name from unconfirmed_entries where account != ''",
        "select distinct target_account name from unconfirmed_entries where target_account is not null and target_account != ''",
    ]:
        names.update(row["name"] for row in conn.execute(sql).fetchall())
    conn.close()
    return sorted(names)


def add_account_name(name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("account name is required")
    init_db()
    conn = connect()
    try:
        ensure_account(conn, name)
        # 新建资产账户立即回填 asset_type（下次 GET summary 分组即正确，幂等）
        debt_accounts.backfill_new_columns(conn)
        conn.commit()
    finally:
        conn.close()
    accounts = config_account_names()
    if name not in accounts:
        accounts.append(name)
        write_config_accounts(accounts)


def write_config_accounts(accounts):
    seen = set()
    clean_accounts = []
    for account in accounts:
        account = str(account or "").strip()
        if account and account not in seen:
            seen.add(account)
            clean_accounts.append(account)
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines() if CONFIG_PATH.exists() else []
    account_header = next((i for i, line in enumerate(lines) if line.strip() == "accounts:" and not line.startswith(" ")), None)
    if account_header is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("accounts:")
        lines.extend(f"  - {name}" for name in clean_accounts)
    else:
        end = account_header + 1
        while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
            end += 1
        lines = lines[:account_header + 1] + [f"  - {name}" for name in clean_accounts] + lines[end:]
    CONFIG_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def rename_account_name(old_name, new_name):
    old_name = str(old_name or "").strip()
    new_name = str(new_name or "").strip()
    if not old_name or not new_name:
        raise ValueError("account name is required")
    if old_name == new_name:
        return {"ok": True}
    if new_name in set(account_names()):
        raise ValueError("account already exists")
    init_db()
    conn = connect()
    try:
        row = conn.execute("select id from accounts where name = ?", (old_name,)).fetchone()
        if row:
            conn.execute("update accounts set name = ?, updated_at = ? where name = ?", (new_name, now_iso(), old_name))
        else:
            ensure_account(conn, new_name)
        conn.execute("update entries set account = ? where account = ?", (new_name, old_name))
        conn.execute("update entries set target_account = ? where target_account = ?", (new_name, old_name))
        conn.execute("update unconfirmed_entries set account = ? where account = ?", (new_name, old_name))
        conn.execute("update unconfirmed_entries set target_account = ? where target_account = ?", (new_name, old_name))
        conn.commit()
    finally:
        conn.close()

    accounts = [new_name if account == old_name else account for account in config_account_names()]
    if new_name not in accounts:
        accounts.append(new_name)
    write_config_accounts(accounts)
    return {"ok": True}


def delete_account_name(name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("account name is required")
    init_db()
    conn = connect()
    try:
        conn.execute("delete from accounts where name = ?", (name,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def set_account_archived(name, archived=True):
    """债权/负债账户归档/恢复（参考活动归档）：归档后不在债权负债主列表显示，流水保留可查。

    归档快照（防双计/利息口径，仅借款型+贷款型）：
    - 归档时对借款型/贷款型负债算一次快照三列（本金/Σ还款/总利息=Σ还款−本金），
      此后展示不再受后续流水影响；principal 未填时 repay 照算、interest 留 NULL；
    - 信用卡型（循环额度，无本金概念）归档不写快照（三列 NULL）；
    - 恢复归档清空三列，继续还款后重新归档即重算。
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("account name is required")
    init_db()
    conn = connect()
    snapshot = {"principal_cents": None, "repay_cents": None, "interest_cents": None}
    try:
        row = conn.execute("select * from accounts where name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError("账户不存在")
        if (row["kind"] or "asset") not in ("liability", "receivable"):
            raise ValueError("只有债权/负债账户可以归档")
        if archived:
            if row["kind"] == "liability" and not debt_accounts.is_credit_type_row(row):
                principal = row["principal_cents"]
                repay = conn.execute(
                    "select coalesce(sum(amount_cents), 0) v from entries where type = '转账' and target_account = ?",
                    (name,),
                ).fetchone()["v"]
                snapshot = {
                    "principal_cents": principal,
                    "repay_cents": int(repay),
                    "interest_cents": (int(repay) - principal) if principal is not None else None,
                }
                conn.execute(
                    """
                    update accounts set archived = 1, archived_principal_cents = ?, archived_repay_cents = ?,
                        archived_interest_cents = ?, updated_at = ? where name = ?
                    """,
                    (snapshot["principal_cents"], snapshot["repay_cents"], snapshot["interest_cents"], now_iso(), name),
                )
            else:
                # 信用卡型/债权：只置归档标记，快照三列保持 NULL
                conn.execute(
                    "update accounts set archived = 1, updated_at = ? where name = ?",
                    (now_iso(), name),
                )
        else:
            conn.execute(
                """
                update accounts set archived = 0, archived_principal_cents = null, archived_repay_cents = null,
                    archived_interest_cents = null, updated_at = ? where name = ?
                """,
                (now_iso(), name),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "name": name, "archived": bool(archived), **snapshot}


def add_category(tx_type, category):
    tx_type = str(tx_type or "").strip()
    category = str(category or "").strip().strip("/")
    if tx_type not in ENTRY_TYPES:
        raise ValueError("invalid type")
    if not category:
        raise ValueError("category is required")
    tree = category_tree()
    existing = set(flatten_categories())
    if category in existing:
        return
    parts = [p.strip() for p in category.split("/", 1)]
    group = parts[0]
    leaf = parts[1] if len(parts) > 1 and parts[1] else None
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines() if CONFIG_PATH.exists() else ["categories:"]
    type_header = next((i for i, line in enumerate(lines) if line == f"  {tx_type}:"), None)
    if type_header is None:
        lines.append("")
        lines.append(f"  {tx_type}:")
        type_header = len(lines) - 1
    type_end = type_header + 1
    while type_end < len(lines) and not (lines[type_end].startswith("  ") and not lines[type_end].startswith("    ") and lines[type_end].strip().endswith(":")):
        if lines[type_end] and not lines[type_end].startswith(" "):
            break
        type_end += 1
    if leaf is None:
        lines.insert(type_end, f"    {group}: []")
    else:
        group_line = next((i for i in range(type_header + 1, type_end) if lines[i].startswith("    ") and lines[i].strip().split(":", 1)[0] == group), None)
        if group_line is None:
            lines.insert(type_end, f"    {group}:")
            lines.insert(type_end + 1, f"      - {leaf}")
        elif lines[group_line].strip().endswith("[]"):
            lines[group_line] = f"    {group}:"
            lines.insert(group_line + 1, f"      - {leaf}")
        else:
            insert_at = group_line + 1
            while insert_at < type_end and lines[insert_at].startswith("      "):
                insert_at += 1
            lines.insert(insert_at, f"      - {leaf}")
    CONFIG_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _set_category_path(tree, tx_type, category):
    parts = [p.strip() for p in str(category or "").strip().strip("/").split("/", 1)]
    group = parts[0] if parts else ""
    leaf = parts[1] if len(parts) > 1 and parts[1] else None
    if not group:
        raise ValueError("category is required")
    type_tree = tree.setdefault(tx_type, {})
    leaves = type_tree.setdefault(group, [])
    if leaf and leaf not in leaves:
        leaves.append(leaf)


def _remove_category_path(tree, tx_type, category):
    parts = [p.strip() for p in str(category or "").strip().strip("/").split("/", 1)]
    group = parts[0] if parts else ""
    leaf = parts[1] if len(parts) > 1 and parts[1] else None
    type_tree = tree.get(tx_type) or {}
    if group not in type_tree:
        raise ValueError("category not found")
    if leaf:
        leaves = type_tree[group]
        if leaf not in leaves:
            raise ValueError("category not found")
        leaves.remove(leaf)
        if not leaves:
            del type_tree[group]
    else:
        if type_tree[group]:
            raise ValueError("cannot delete category group with subcategories")
        del type_tree[group]


def _write_category_tree(tree):
    accounts = config_account_names()
    lines = ["categories:"]
    for tx_type, groups in tree.items():
        lines.append(f"  {tx_type}:")
        for group, leaves in groups.items():
            if leaves:
                lines.append(f"    {group}:")
                lines.extend(f"      - {leaf}" for leaf in leaves)
            else:
                lines.append(f"    {group}: []")
        lines.append("")
    lines.append("accounts:")
    lines.extend(f"  - {name}" for name in accounts)
    CONFIG_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def delete_category(tx_type, category):
    tx_type = str(tx_type or "").strip()
    category = str(category or "").strip().strip("/")
    if tx_type not in ENTRY_TYPES:
        raise ValueError("invalid type")
    if not category:
        raise ValueError("category is required")
    tree = category_tree()
    if category not in set(categories_for_type(tree, tx_type)):
        raise ValueError("category not found")
    _remove_category_path(tree, tx_type, category)
    _write_category_tree(tree)
    init_db()
    conn = connect()
    try:
        conn.execute("delete from merchant_category_rules where category = ? and coalesce(type, ?) = ?", (category, tx_type, tx_type))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def rename_category(tx_type, old_category, new_category):
    tx_type = str(tx_type or "").strip()
    old_category = str(old_category or "").strip().strip("/")
    new_category = str(new_category or "").strip().strip("/")
    if tx_type not in ENTRY_TYPES:
        raise ValueError("invalid type")
    if not old_category or not new_category:
        raise ValueError("category is required")
    tree = category_tree()
    current = set(categories_for_type(tree, tx_type))
    if old_category not in current:
        raise ValueError("category not found")
    if new_category != old_category and new_category in current:
        raise ValueError("category already exists")
    _remove_category_path(tree, tx_type, old_category)
    _set_category_path(tree, tx_type, new_category)
    _write_category_tree(tree)
    init_db()
    conn = connect()
    try:
        conn.execute("update entries set category = ? where category = ? and type = ?", (new_category, old_category, tx_type))
        conn.execute("update unconfirmed_entries set category = ? where category = ? and type = ?", (new_category, old_category, tx_type))
        conn.execute("update merchant_category_rules set category = ? where category = ? and coalesce(type, ?) = ?", (new_category, old_category, tx_type, tx_type))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def categories_for_type(tree, tx_type):
    groups = tree.get(tx_type) or {}
    categories = []
    for group, leaves in groups.items():
        if leaves:
            categories.extend(f"{group}/{leaf}" for leaf in leaves)
        else:
            categories.append(group)
    return categories


def valid_categories():
    return set(flatten_categories()) | {"转账", "余额修正", "退款"}


def standardize_category(category, tx_type=None):
    _, _, path = parse_category_path(category)
    if not path:
        return None
    valid = valid_categories()
    if path in valid:
        return path
    if tx_type == "转账" and path == "转账":
        return "转账"
    return None


def apply_learned_rule(conn, record):
    if record.get("category"):
        return record
    candidates = conn.execute(
        "select merchant, category, type from merchant_category_rules order by length(merchant) desc"
    ).fetchall()
    rule = match_rule_in_text(candidates, record.get("transaction_object"))
    if not rule:
        rule = match_rule_in_text(candidates, record.get("note"))
    if rule:
        record["category"] = rule["category"]
        if rule["type"]:
            record["type"] = rule["type"]
    return record


def match_rule_in_text(candidates, value):
    text = str(value or "").strip()
    if not text:
        return None
    return next((candidate for candidate in candidates if candidate["merchant"] and candidate["merchant"] in text), None)


def normalize_record(conn, record, apply_rules=True):
    record = dict(record)
    record["transaction_object"] = record.get("transaction_object") or record.get("merchant") or record.get("counterparty")
    record["transaction_time"] = record.get("transaction_time") or now_iso()
    record["currency"] = record.get("currency") or "CNY"
    record["participant"] = record.get("participant") or "自己"
    record["target_account"] = record.get("target_account") or record.get("to_account") or record.get("transfer_account")
    record["type"] = record.get("type")
    if record["type"] and record["type"] not in ENTRY_TYPES:
        record["type"] = None
    if record["type"] == "转账":
        # 保留合法的转账类自定义分类（如 理财买入/理财卖出），其余归为通用"转账"
        record["category"] = standardize_category(record.get("category"), "转账") or "转账"
    if not record.get("category") and record.get("category_level1"):
        level1 = str(record.get("category_level1") or "").strip()
        level2 = str(record.get("category_level2") or "").strip()
        record["category"] = f"{level1}/{level2}" if level2 else level1
    if apply_rules:
        record = apply_learned_rule(conn, record)
    record["category"] = standardize_category(record.get("category"), record.get("type"))
    return record


def update_account_balance(conn, account, tx_type, amount_cents, balance_delta_cents=None):
    delta = account_balance_delta(tx_type, amount_cents, balance_delta_cents)
    if delta is None:
        return
    ensure_account(conn, account)
    conn.execute(
        "update accounts set current_balance_cents = current_balance_cents + ?, updated_at = ? where name = ?",
        (delta, now_iso(), account),
    )


def debt_party_name(row):
    name = str(row["transaction_object"] or row["participant"] or "").strip()
    return name or "未指定对象"


def sync_account_debts_from_entries(conn):
    rows = conn.execute(
        """
        select type, amount_cents, transaction_object, participant
        from entries
        where type in ('借出', '收款', '借入', '还款')
        """
    ).fetchall()
    totals = {}
    for row in rows:
        name = debt_party_name(row)
        current = totals.setdefault(name, {"receivable_cents": 0, "payable_cents": 0})
        amount = int(row["amount_cents"] or 0)
        if row["type"] == "借出":
            current["receivable_cents"] += amount
        elif row["type"] == "收款":
            current["receivable_cents"] -= amount
        elif row["type"] == "借入":
            current["payable_cents"] += amount
        elif row["type"] == "还款":
            current["payable_cents"] -= amount

    ts = now_iso()
    for name, total in totals.items():
        receivable = max(0, total["receivable_cents"])
        payable = max(0, total["payable_cents"])
        conn.execute(
            """
            insert into account_debts(name, receivable_cents, payable_cents, currency, visible, created_at, updated_at)
            values (?, ?, ?, 'CNY', 1, ?, ?)
            on conflict(name) do update set
                receivable_cents = excluded.receivable_cents,
                payable_cents = excluded.payable_cents,
                updated_at = excluded.updated_at
            """,
            (name, receivable, payable, ts, ts),
        )

    existing_names = set(totals)
    if existing_names:
        placeholders = ",".join("?" for _ in existing_names)
        conn.execute(
            f"""
            update account_debts
            set receivable_cents = 0, payable_cents = 0, updated_at = ?
            where name not in ({placeholders})
            """,
            (ts, *existing_names),
        )
    else:
        conn.execute("update account_debts set receivable_cents = 0, payable_cents = 0, updated_at = ?", (ts,))


def list_account_debts(conn):
    return rows_to_dicts(
        conn.execute(
            """
            select id, name, receivable_cents, payable_cents, currency, updated_at
            from account_debts
            where visible = 1 and (receivable_cents != 0 or payable_cents != 0)
            order by (receivable_cents + payable_cents) desc, name
            """
        ).fetchall()
    )


def hide_account_debt(debt_id):
    init_db()
    conn = connect()
    try:
        row = conn.execute("select id from account_debts where id = ?", (debt_id,)).fetchone()
        if not row:
            raise ValueError(f"debt row not found: {debt_id}")
        ts = now_iso()
        conn.execute(
            "update account_debts set visible = 0, hidden_at = ?, updated_at = ? where id = ?",
            (ts, ts, debt_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def is_internal_transfer_record(conn, record):
    if record.get("type") != "转账":
        return False
    account = str(record.get("account") or "").strip()
    target_account = str(record.get("target_account") or "").strip()
    if not account or not target_account or account == target_account:
        return False
    own_accounts = known_own_account_names(conn)
    return account in own_accounts and target_account in own_accounts


def insert_entry(record, auto_learn=False, raw_payload=None):
    init_db()
    conn = connect()
    try:
        result = insert_entry_with_conn(conn, record, auto_learn=auto_learn, raw_payload=raw_payload)
        sync_account_debts_from_entries(conn)
        sync_current_balances_from_entries(conn)
        conn.commit()
        return result
    finally:
        conn.close()


def normalize_activity_id(conn, value):
    """activity_id 归一：空/0 → None；必须指向已存在的活动。"""
    value = str(value or "").strip()
    if not value or value == "0":
        return None
    try:
        aid = int(value)
    except ValueError:
        return None
    if not conn.execute("select 1 from activities where id = ?", (aid,)).fetchone():
        raise ValueError(f"activity not found: {aid}")
    return aid


def insert_entry_with_conn(conn, record, auto_learn=False, raw_payload=None):
    record = normalize_record(conn, record)
    # 防双计硬拦：还款口径负债账户不记支出（与月供转账会确定性双重计支出）
    debt_accounts.enforce_no_expense_on_repay(conn, record)
    # 退款关联：仅退款类型且原支出存在时保留，否则忽略
    pair_id = record.get("refund_pair_id")
    pair_id = int(pair_id) if str(pair_id or "").strip().isdigit() else None
    if pair_id is not None:
        if record.get("type") != "退款":
            pair_id = None
        else:
            src = conn.execute(
                "select id from entries where id = ? and type = '支出'", (pair_id,)
            ).fetchone()
            if src is None:
                pair_id = None
    record["refund_pair_id"] = pair_id
    record["activity_id"] = normalize_activity_id(conn, record.get("activity_id"))
    bill_source = str(record.get("bill_source") or "").strip() or None
    raw_amount_cents = cents(record.get("amount"))
    amount_cents = abs(raw_amount_cents)
    if record.get("type") == "余额修正" and record.get("balance_delta_cents") in (None, ""):
        record["balance_delta_cents"] = raw_amount_cents
    required_missing = not record.get("account") or not record.get("type") or not amount_cents
    if record.get("type") == "转账" and not record.get("target_account"):
        required_missing = True
    if record.get("type") == "转账" and record.get("target_account") and not is_internal_transfer_record(conn, record):
        required_missing = True
    if record.get("type") == "转账" and record.get("account") == record.get("target_account"):
        raise ValueError("transfer target_account must differ from account")
    needs_category = record.get("type") not in {"转账", "余额修正"} and not record.get("category")
    if required_missing:
        conn.execute(
            """
            insert into unconfirmed_entries(transaction_time, type, category, amount_cents, currency, transaction_object,
                status, account, target_account, participant, balance_delta_cents, actual_balance_cents,
                note, refund_pair_id, activity_id, bill_source, raw_payload)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["transaction_time"],
                record.get("type"),
                record.get("category"),
                amount_cents,
                record["currency"],
                record.get("transaction_object"),
                "pending",
                record.get("account") or "",
                record.get("target_account"),
                record["participant"],
                record.get("balance_delta_cents"),
                record.get("actual_balance_cents"),
                record.get("note"),
                record.get("refund_pair_id"),
                record.get("activity_id"),
                bill_source,
                json.dumps(raw_payload or record, ensure_ascii=False),
            ),
        )
        result = {"status": "unconfirmed", "id": conn.execute("select last_insert_rowid()").fetchone()[0]}
        # 防漏计软提示：还款口径账户收到普通转账（可能是合法拆借，只提示不拦）
        warning = debt_accounts.repay_transfer_warning(conn, record)
        if warning:
            result["warning"] = warning
        return result
    status = "pending" if needs_category else "confirmed"
    ensure_account(conn, record["account"], record["currency"])
    if record.get("type") == "转账":
        ensure_account(conn, record["target_account"], record["currency"])
    conn.execute(
        """
        insert into entries(transaction_time, type, category, status, amount_cents, currency, transaction_object, account,
            target_account, participant, balance_delta_cents, actual_balance_cents, note, refund_pair_id, activity_id, bill_source)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["transaction_time"],
            record["type"],
            record.get("category"),
            status,
            amount_cents,
            record["currency"],
            record.get("transaction_object"),
            record["account"],
            record.get("target_account"),
            record["participant"],
            record.get("balance_delta_cents"),
            record.get("actual_balance_cents"),
            record.get("note"),
            record.get("refund_pair_id"),
            record.get("activity_id"),
            bill_source,
        ),
    )
    entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
    update_account_balance(conn, record["account"], record["type"], amount_cents, record.get("balance_delta_cents"))
    if record.get("type") == "转账":
        update_account_balance(conn, record["target_account"], "收入", amount_cents)
    if auto_learn and record.get("transaction_object") and record.get("category"):
        add_rule(conn, record["transaction_object"], record["category"], record.get("type"))
    result = {"status": status, "id": entry_id}
    warning = debt_accounts.repay_transfer_warning(conn, record)
    if warning:
        result["warning"] = warning
    return result


def add_rule(conn, merchant, category, tx_type=None):
    merchant = str(merchant or "").strip()
    category = standardize_category(category, tx_type)
    if not merchant:
        raise ValueError("keyword is required")
    if not category:
        raise ValueError("valid category is required")
    if tx_type and tx_type not in ENTRY_TYPES:
        raise ValueError("invalid type")
    if tx_type == "退款":
        # 退款不需要分类/关键词规则：退款分类始终跟随关联的原支出
        raise ValueError("退款类型不需要关键词规则（退款分类跟随关联的原支出）")
    conn.execute(
        """
        insert into merchant_category_rules(merchant, category, type, created_at)
        values (?, ?, ?, ?)
        on conflict(merchant) do update set category=excluded.category, type=excluded.type
        """,
        (merchant, category, tx_type, now_iso()),
    )


def list_keyword_rules():
    init_db()
    conn = connect()
    rows = rows_to_dicts(
        conn.execute(
            """
            select id, merchant keyword, category, type, created_at
            from merchant_category_rules
            order by coalesce(type, ''), category, keyword
            """
        ).fetchall()
    )
    conn.close()
    return rows


def add_keyword_rule(keyword, category, tx_type=None):
    init_db()
    conn = connect()
    try:
        add_rule(conn, keyword, category, tx_type)
        conn.commit()
        row = conn.execute(
            "select id, merchant keyword, category, type, created_at from merchant_category_rules where merchant = ?",
            (str(keyword or "").strip(),),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def apply_keyword_rule_to_unconfirmed(keyword, category, tx_type=None):
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"applied": 0, "ids": []}
    category = standardize_category(category, tx_type)
    if not category:
        raise ValueError("valid category is required")
    init_db()
    conn = connect()
    try:
        candidates = [{"merchant": keyword, "category": category, "type": tx_type}]
        pending_rows = [
            row for row in conn.execute("select * from entries where status = 'pending' order by transaction_time, id").fetchall()
            if match_rule_in_text(candidates, row["transaction_object"]) or match_rule_in_text(candidates, row["note"])
        ]
        rows = [
            row for row in conn.execute("select * from unconfirmed_entries order by transaction_time, id").fetchall()
            if match_rule_in_text(candidates, row["transaction_object"]) or match_rule_in_text(candidates, row["note"])
        ]
        ids = [row["id"] for row in rows]
        if ids:
            conn.execute(
                f"delete from unconfirmed_entries where id in ({', '.join('?' for _ in ids)})",
                tuple(ids),
            )
        conn.commit()
    finally:
        conn.close()
    applied_ids = []
    for row in pending_rows:
        result = confirm_pending_entry(row["id"], category, tx_type)
        if result:
            applied_ids.append(result["id"])
    for row in rows:
        record = dict(row)
        record["category"] = category
        if tx_type:
            record["type"] = tx_type
        record["amount"] = money(record.pop("amount_cents"))
        result = insert_entry(record, raw_payload=parse_attributes(record.get("raw_payload")) or record)
        if result["status"] == "confirmed":
            applied_ids.append(result["id"])
    return {"applied": len(applied_ids), "ids": applied_ids}


def apply_all_keyword_rules_to_unconfirmed():
    init_db()
    conn = connect()
    try:
        candidates = conn.execute(
            "select merchant, category, type from merchant_category_rules order by length(merchant) desc"
        ).fetchall()
        pending_rows = conn.execute("select * from entries where status = 'pending' order by transaction_time, id").fetchall()
        pending_matches = []
        for row in pending_rows:
            rule = match_rule_in_text(candidates, row["transaction_object"]) or match_rule_in_text(candidates, row["note"])
            if rule:
                pending_matches.append((row, rule))
        rows = conn.execute("select * from unconfirmed_entries order by transaction_time, id").fetchall()
        matches = []
        for row in rows:
            rule = match_rule_in_text(candidates, row["transaction_object"]) or match_rule_in_text(candidates, row["note"])
            if rule:
                matches.append((row, rule))
        ids = [row["id"] for row, _ in matches]
        if ids:
            conn.execute(
                f"delete from unconfirmed_entries where id in ({', '.join('?' for _ in ids)})",
                tuple(ids),
            )
        conn.commit()
    finally:
        conn.close()
    applied_ids = []
    for row, rule in pending_matches:
        result = confirm_pending_entry(row["id"], rule["category"], rule["type"])
        if result:
            applied_ids.append(result["id"])
    for row, rule in matches:
        record = dict(row)
        record["category"] = rule["category"]
        if rule["type"]:
            record["type"] = rule["type"]
        record["amount"] = money(record.pop("amount_cents"))
        result = insert_entry(record, raw_payload=parse_attributes(record.get("raw_payload")) or record)
        if result["status"] == "confirmed":
            applied_ids.append(result["id"])
    return {"applied": len(applied_ids), "ids": applied_ids}


def add_keyword_rule_and_apply(keyword, category, tx_type=None):
    rule = add_keyword_rule(keyword, category, tx_type)
    result = apply_keyword_rule_to_unconfirmed(keyword, category, tx_type)
    rule["applied_unconfirmed"] = result["applied"]
    rule["applied_entry_ids"] = result["ids"]
    return rule


def delete_keyword_rule(rule_id):
    init_db()
    conn = connect()
    try:
        conn.execute("delete from merchant_category_rules where id = ?", (rule_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def confirm_pending_entry(entry_id, category, tx_type=None, learn=False, transaction_object=None, note=None, account=None, target_account=None, refund_pair_id=None):
    conn = connect()
    try:
        row = conn.execute("select * from entries where id = ? and status = 'pending'", (entry_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    data = {
        "id": entry_id,
        "category": category,
    }
    if tx_type:
        data["type"] = tx_type
    if account is not None and str(account or "").strip():
        data["account"] = str(account).strip()
    if target_account is not None and str(target_account or "").strip():
        data["target_account"] = str(target_account).strip()
    if transaction_object is not None:
        data["transaction_object"] = str(transaction_object or "").strip()
    if note is not None:
        data["note"] = str(note or "").strip()
    if refund_pair_id is not None and str(refund_pair_id or "").strip():
        data["refund_pair_id"] = refund_pair_id
    update_entry(entry_id, data)
    conn = connect()
    try:
        conn.execute("update entries set status = 'confirmed' where id = ?", (entry_id,))
        if learn:
            keyword = data.get("transaction_object")
            if keyword is None:
                keyword = row["transaction_object"]
            if keyword:
                add_rule(conn, keyword, category, tx_type or row["type"])
        conn.commit()
    finally:
        conn.close()
    return {"status": "confirmed", "id": entry_id}


def update_pending_meta(entry_id, source=None, transaction_object=None, note=None):
    """Save transaction_object/note edits on a pending/unconfirmed entry without confirming it."""
    init_db()
    obj = str(transaction_object if transaction_object is not None else "").strip()
    note_val = str(note if note is not None else "").strip()
    conn = connect()
    try:
        if source == "unconfirmed":
            cur = conn.execute(
                "update unconfirmed_entries set transaction_object = ?, note = ? where id = ?",
                (obj, note_val, entry_id),
            )
        else:
            cur = conn.execute(
                "update entries set transaction_object = ?, note = ? where id = ? and status = 'pending'",
                (obj, note_val, entry_id),
            )
        if cur.rowcount == 0:
            raise ValueError(f"pending entry not found: {entry_id}")
        conn.commit()
        return {"ok": True, "id": entry_id, "transaction_object": obj, "note": note_val}
    finally:
        conn.close()


def confirm_entry(unconfirmed_id, category, tx_type=None, learn=False, transaction_object=None, note=None, source=None, account=None, target_account=None, refund_pair_id=None):
    init_db()
    if source != "unconfirmed":
        pending_result = confirm_pending_entry(unconfirmed_id, category, tx_type, learn, transaction_object, note, account, target_account, refund_pair_id)
        if pending_result:
            return pending_result
    if source == "pending":
        raise SystemExit(f"pending entry not found: {unconfirmed_id}")
    conn = connect()
    row = conn.execute("select * from unconfirmed_entries where id = ?", (unconfirmed_id,)).fetchone()
    if not row:
        raise SystemExit(f"unconfirmed entry not found: {unconfirmed_id}")
    record = dict(row)
    record["category"] = category
    if account is not None and str(account or "").strip():
        record["account"] = str(account).strip()
    if target_account is not None and str(target_account or "").strip():
        record["target_account"] = str(target_account).strip()
    if transaction_object is not None:
        record["transaction_object"] = str(transaction_object or "").strip()
    if note is not None:
        record["note"] = str(note or "").strip()
    if tx_type:
        record["type"] = tx_type
    if refund_pair_id is not None and str(refund_pair_id or "").strip():
        record["refund_pair_id"] = refund_pair_id
    record["amount"] = money(record.pop("amount_cents"))
    conn.execute("delete from unconfirmed_entries where id = ?", (unconfirmed_id,))
    conn.commit()
    conn.close()
    result = insert_entry(record, auto_learn=learn)
    rule_keyword = record.get("transaction_object") or row["transaction_object"]
    if learn and rule_keyword:
        conn = connect()
        add_rule(conn, rule_keyword, category, tx_type or row["type"])
        conn.commit()
        conn.close()
    return result


def update_entry(entry_id, data):
    init_db()
    conn = connect()
    try:
        old = conn.execute("select * from entries where id = ?", (entry_id,)).fetchone()
        if not old:
            raise ValueError(f"entry not found: {entry_id}")
        record = dict(old)
        editable_fields = [
            "transaction_time",
            "type",
            "category",
            "amount",
            "currency",
            "transaction_object",
            "account",
            "target_account",
            "participant",
            "balance_delta_cents",
            "actual_balance_cents",
            "note",
            "refund_pair_id",
            "activity_id",
            "bill_source",
        ]
        for field in editable_fields:
            if field in data:
                record[field] = data[field]
        parsed_time = parse_transaction_time(record.get("transaction_time"))
        if not parsed_time:
            raise ValueError("invalid transaction_time")
        record["transaction_time"] = parsed_time
        record = normalize_record(conn, record, apply_rules=False)
        # 防双计硬拦（编辑同样拦截）：还款口径负债账户不记支出
        debt_accounts.enforce_no_expense_on_repay(conn, record)
        amount_cents = abs(cents(record.get("amount", money(old["amount_cents"]))))
        required_missing = not record.get("transaction_time") or not record.get("account") or not record.get("type") or not amount_cents
        if record.get("type") == "转账" and not record.get("target_account"):
            required_missing = True
        if record.get("type") == "转账" and record.get("target_account") and not is_internal_transfer_record(conn, record):
            required_missing = True
        if record.get("type") == "转账" and record.get("account") == record.get("target_account"):
            raise ValueError("transfer target_account must differ from account")
        needs_category = record.get("type") not in {"转账", "余额修正"} and not record.get("category")
        if required_missing or needs_category:
            raise ValueError("transaction_time, type, category, amount, account and target_account for transfers are required")
        new_status = "confirmed" if old["status"] == "pending" and not needs_category else old["status"]

        update_account_balance(conn, old["account"], old["type"], -old["amount_cents"], old["balance_delta_cents"])
        if old["type"] == "转账" and old["target_account"]:
            update_account_balance(conn, old["target_account"], "收入", -old["amount_cents"])
        # 退款关联校验：仅退款类型保留；原支出必须存在且为支出
        pair_id = record.get("refund_pair_id")
        pair_id = int(pair_id) if str(pair_id or "").strip().isdigit() else None
        if pair_id is not None:
            src = conn.execute(
                "select id from entries where id = ? and type = '支出'", (pair_id,)
            ).fetchone()
            if src is None or record.get("type") != "退款":
                pair_id = None
        activity_id = normalize_activity_id(conn, record.get("activity_id"))
        conn.execute(
            """
            update entries set transaction_time = ?, type = ?, category = ?, status = ?, amount_cents = ?, currency = ?,
                transaction_object = ?, account = ?, target_account = ?, participant = ?,
                balance_delta_cents = ?, actual_balance_cents = ?, note = ?, refund_pair_id = ?, activity_id = ?,
                bill_source = ?
            where id = ?
            """,
            (
                record["transaction_time"],
                record["type"],
                record.get("category"),
                new_status,
                amount_cents,
                record["currency"],
                record.get("transaction_object"),
                record["account"],
                record.get("target_account"),
                record["participant"],
                record.get("balance_delta_cents"),
                record.get("actual_balance_cents"),
                record.get("note"),
                pair_id,
                activity_id,
                str(record.get("bill_source") or "").strip() or None,
                entry_id,
            ),
        )
        update_account_balance(conn, record["account"], record["type"], amount_cents, record.get("balance_delta_cents"))
        if record.get("type") == "转账":
            update_account_balance(conn, record["target_account"], "收入", amount_cents)
        sync_account_debts_from_entries(conn)
        sync_current_balances_from_entries(conn)
        conn.commit()
        result = {"ok": True}
        warning = debt_accounts.repay_transfer_warning(conn, record)
        if warning:
            result["warning"] = warning
        return result
    finally:
        conn.close()


def delete_entry(entry_id):
    init_db()
    conn = connect()
    try:
        entry = conn.execute("select * from entries where id = ?", (entry_id,)).fetchone()
        if not entry:
            raise ValueError(f"entry not found: {entry_id}")
        update_account_balance(conn, entry["account"], entry["type"], -entry["amount_cents"], entry["balance_delta_cents"])
        if entry["type"] == "转账" and entry["target_account"]:
            update_account_balance(conn, entry["target_account"], "收入", -entry["amount_cents"])
        # 删除原支出时，解除指向它的退款关联（退款保留、退回未关联状态）
        conn.execute(
            "update entries set refund_pair_id = null where refund_pair_id = ?", (entry_id,)
        )
        conn.execute("delete from entries where id = ?", (entry_id,))
        sync_account_debts_from_entries(conn)
        sync_current_balances_from_entries(conn)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def link_refund(refund_id, expense_id):
    """手动把一笔退款关联到原支出：退款分类继承原支出分类，统计按净额冲减。"""
    init_db()
    conn = connect()
    try:
        refund = conn.execute("select * from entries where id = ?", (refund_id,)).fetchone()
        if refund is None:
            raise ValueError("退款记录不存在")
        if refund["type"] != "退款":
            raise ValueError("只有退款类型的记录才能关联原支出")
        expense = conn.execute(
            "select * from entries where id = ? and type = '支出'", (int(expense_id),)
        ).fetchone()
        if expense is None:
            raise ValueError("原支出不存在或类型不是支出")
        conn.execute(
            "update entries set refund_pair_id = ?, category = ? where id = ?",
            (int(expense_id), expense["category"] or refund["category"], refund_id),
        )
        conn.commit()
        return {"ok": True, "refund_id": refund_id, "expense_id": int(expense_id)}
    finally:
        conn.close()


def unlink_refund(refund_id):
    """解除退款关联：退款保留、退回未关联状态（不再冲减支出统计）。"""
    init_db()
    conn = connect()
    try:
        refund = conn.execute("select * from entries where id = ?", (refund_id,)).fetchone()
        if refund is None:
            raise ValueError("退款记录不存在")
        if refund["type"] != "退款":
            raise ValueError("只有退款类型的记录才能解除关联")
        conn.execute("update entries set refund_pair_id = null where id = ?", (refund_id,))
        conn.commit()
        return {"ok": True, "refund_id": refund_id}
    finally:
        conn.close()


# ---------- 活动（活动）：与分类平行的第二视角 ----------

def _activity_row(conn, row):
    """活动行附加统计：净额 = 挂载支出 − 已关联到这些支出的退款。"""
    aid = row["id"]
    stat = conn.execute(
        """
        select count(*) n, coalesce(sum(amount_cents), 0) gross
        from entries where type = '支出' and activity_id = ?
        """,
        (aid,),
    ).fetchone()
    refund = conn.execute(
        """
        select coalesce(sum(r.amount_cents), 0) refunded
        from entries r
        where r.type = '退款' and r.refund_pair_id in (
            select id from entries where type = '支出' and activity_id = ?
        )
        """,
        (aid,),
    ).fetchone()
    span = conn.execute(
        """
        select min(transaction_time) first_time, max(transaction_time) last_time
        from entries where type = '支出' and activity_id = ?
        """,
        (aid,),
    ).fetchone()
    out = dict(row)
    out["expense_count"] = stat["n"]
    out["gross_cents"] = stat["gross"]
    out["refund_cents"] = refund["refunded"]
    out["net_cents"] = stat["gross"] - refund["refunded"]
    out["first_time"] = span["first_time"]
    out["last_time"] = span["last_time"]
    return out


def create_activity(name, start_date=None, end_date=None, note=None):
    init_db()
    name = str(name or "").strip()
    if not name:
        raise ValueError("活动名称不能为空")
    conn = connect()
    try:
        if conn.execute("select 1 from activities where name = ?", (name,)).fetchone():
            raise ValueError(f"活动「{name}」已存在")
        ts = now_iso()
        cur = conn.execute(
            "insert into activities(name, start_date, end_date, note, created_at, updated_at) values (?, ?, ?, ?, ?, ?)",
            (name, str(start_date or "").strip() or None, str(end_date or "").strip() or None, str(note or "").strip() or None, ts, ts),
        )
        aid = cur.lastrowid
        conn.commit()
        return {"ok": True, "id": aid}
    finally:
        conn.close()


def update_activity(activity_id, data):
    init_db()
    conn = connect()
    try:
        row = conn.execute("select * from activities where id = ?", (int(activity_id),)).fetchone()
        if not row:
            raise ValueError(f"活动不存在: {activity_id}")
        fields = {}
        for key in ("name", "start_date", "end_date", "note"):
            if key in data:
                value = str(data[key] or "").strip()
                if key == "name":
                    if not value:
                        raise ValueError("活动名称不能为空")
                    dup = conn.execute("select 1 from activities where name = ? and id != ?", (value, int(activity_id))).fetchone()
                    if dup:
                        raise ValueError(f"活动「{value}」已存在")
                fields[key] = value or None
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"update activities set {sets}, updated_at = ? where id = ?",
                (*fields.values(), now_iso(), int(activity_id)),
            )
            conn.commit()
        return {"ok": True, "id": int(activity_id)}
    finally:
        conn.close()


def delete_activity(activity_id):
    """删除活动：账单保留，仅摘除归属（activity_id 置空）。"""
    init_db()
    conn = connect()
    try:
        aid = int(activity_id)
        if not conn.execute("select 1 from activities where id = ?", (aid,)).fetchone():
            raise ValueError(f"活动不存在: {activity_id}")
        conn.execute("update entries set activity_id = null where activity_id = ?", (aid,))
        conn.execute("update unconfirmed_entries set activity_id = null where activity_id = ?", (aid,))
        conn.execute("delete from activities where id = ?", (aid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def list_activities():
    init_db()
    conn = connect()
    try:
        rows = conn.execute("select * from activities order by archived, id").fetchall()
        return [_activity_row(conn, row) for row in rows]
    finally:
        conn.close()


def set_activity_archived(activity_id, archived):
    """归档/恢复活动：归档后不出现在账单页「加入活动」列表，账单数据保留。"""
    init_db()
    conn = connect()
    try:
        aid = int(activity_id)
        if not conn.execute("select 1 from activities where id = ?", (aid,)).fetchone():
            raise ValueError(f"活动不存在: {activity_id}")
        conn.execute(
            "update activities set archived = ?, updated_at = ? where id = ?",
            (1 if archived else 0, now_iso(), aid),
        )
        conn.commit()
        return {"ok": True, "id": aid, "archived": bool(archived)}
    finally:
        conn.close()


def set_entries_activity(entry_ids, activity_id):
    """批量挂载/摘除账单到活动（单事务）。activity_id 为 None 表示摘除。"""
    init_db()
    conn = connect()
    try:
        ids = [int(x) for x in (entry_ids or []) if str(x).strip().lstrip("-").isdigit()]
        if not ids:
            raise ValueError("未选择任何账单")
        placeholders = ",".join("?" for _ in ids)
        if activity_id is None:
            conn.execute(f"update entries set activity_id = null where id in ({placeholders})", ids)
        else:
            aid = int(activity_id)
            row = conn.execute("select archived from activities where id = ?", (aid,)).fetchone()
            if not row:
                raise ValueError(f"活动不存在: {activity_id}")
            if row["archived"]:
                raise ValueError("该活动已归档，请先在总览恢复后再添加账单")
            conn.execute(f"update entries set activity_id = ? where id in ({placeholders})", (aid, *ids))
        conn.commit()
        return {"ok": True, "count": len(ids)}
    finally:
        conn.close()


def activity_entries(activity_id):
    """活动明细：挂载的支出 + 跟随冲减的退款（通过 refund_pair_id 配对）。"""
    init_db()
    conn = connect()
    try:
        aid = int(activity_id)
        if not conn.execute("select 1 from activities where id = ?", (aid,)).fetchone():
            raise ValueError(f"活动不存在: {activity_id}")
        expenses = rows_to_dicts(conn.execute(
            "select * from entries where type = '支出' and activity_id = ? order by transaction_time",
            (aid,),
        ).fetchall())
        if expenses:
            ids = [e["id"] for e in expenses]
            placeholders = ",".join("?" for _ in ids)
            refunds = rows_to_dicts(conn.execute(
                f"select * from entries where type = '退款' and refund_pair_id in ({placeholders}) order by transaction_time",
                ids,
            ).fetchall())
        else:
            refunds = []
        return {"expenses": expenses, "refunds": refunds}
    finally:
        conn.close()


def refund_evidence(refund, expense):
    """退款-支出配对证据分（log-odds）：正分支持是这一对，负分反对。

    返回 {"score": float, "tags": [str], "identity": bool}；硬排除返回 None。
    identity=金额相同或商户匹配（自动关联的身份门）。
    硬过滤：退款>支出；支出在退款24h后（先用后付仅限退款后24h内且金额相同+同商户）；
    转账退款只能源自转账支出；分值封顶见各信号。
    """
    try:
        rt = dt.datetime.fromisoformat(str(refund["transaction_time"])[:19])
        et = dt.datetime.fromisoformat(str(expense["transaction_time"])[:19])
    except (TypeError, ValueError):
        return None
    hours = (rt - et).total_seconds() / 3600  # >0: 支出在退款前
    refund_cents = abs(int(refund["amount_cents"] or 0))
    expense_cents = abs(int(expense["amount_cents"] or 0))
    if hours > 0 and refund_cents > expense_cents:
        return None  # 退款不可能多于支出
    if hours < -24:
        return None  # 先用后付最多允许退款早于扣款记录24h
    post_refund = hours <= 0
    rnote = str(refund["note"] or "")
    enote = str(expense["note"] or "")
    if "转账-退款" in rnote and "转账" not in enote:
        return None  # 转账退款只能源自转账支出
    score, tags = 0.0, []
    exact_amt = refund_cents == expense_cents
    if exact_amt:
        score += 4.5
        tags.append("金额相同")
    else:
        ratio = refund_cents / expense_cents if expense_cents else 0
        if ratio >= 0.8:
            score += 2
            tags.append(f"退款{ratio:.0%}")
        elif ratio >= 0.5:
            score += 0.5
            tags.append(f"退款{ratio:.0%}")
        else:
            score -= 0.5
            tags.append(f"退款仅{ratio:.0%}")
    r_obj = str(refund["transaction_object"] or "").strip()
    e_obj = str(expense["transaction_object"] or "").strip()
    merchant_match = False
    if not r_obj:
        pass  # 转账退款对象为空：中性
    elif r_obj == e_obj:
        score += 2
        tags.append("同商户")
        merchant_match = True
    elif _norm_merchant(r_obj) and _norm_merchant(r_obj) == _norm_merchant(e_obj):
        score += 1.5
        tags.append("同商户(归一)")
        merchant_match = True
    elif _norm_merchant(r_obj) in _norm_merchant(e_obj) or _norm_merchant(e_obj) in _norm_merchant(r_obj):
        score += 1
        tags.append("商户包含")
        merchant_match = True
    else:
        score -= 2
        tags.append("商户不同")
    if (refund["account"] or "") == (expense["account"] or ""):
        score += 1
        tags.append("同账户")
    else:
        score -= 1
        tags.append("账户不同")
    if post_refund:
        # 先用后付：退款到账早于扣款记录，仅金额相同+同商户允许
        if exact_amt and merchant_match:
            score += 0.5
            tags.append("先用后付")
        else:
            return None
    elif hours <= 1:
        score += 1.5
        tags.append("1h内")
    elif hours <= 24:
        score += 1
        tags.append("24h内")
    elif hours <= 24 * 7:
        score += 0.5
        tags.append("7d内")
    elif hours <= 24 * 30:
        score += 0.2
        tags.append("30d内")
    if "转账-退款" in rnote and "转账" in enote:
        score += 1
        tags.append("同为转账")
    return {"score": score, "tags": tags, "identity": exact_amt or merchant_match}


def _norm_merchant(s):
    """商户名归一化：去括号后缀、空格，转小写。"""
    s = re.sub(r"[（(].*?[)）]", "", str(s or ""))
    return re.sub(r"\s+", "", s).lower()


AUTO_LINK_MARGIN = 2.2   # sigmoid(2.2)≈0.90：最优与次优证据差 ≥2.2 视为90%置信
AUTO_LINK_SINGLE = 5.0   # 唯一候选的绝对分门槛（防"真扣款不在数据里"误连）


def auto_link_refunds(conn):
    """对未关联退款跑自动关联，返回 {"auto": [...], "manual": [...], "no_candidate": [...]}。

    按退款时间升序处理；连上时退款分类继承原支出（与 link_refund 同口径）；
    pending 退款连上后转正（分类已定）。歧义/弱信号留人工，绝不猜测。
    """
    refunds = rows_to_dicts(conn.execute(
        "select * from entries where type = '退款' and refund_pair_id is null order by transaction_time, id"
    ).fetchall())
    expenses = rows_to_dicts(conn.execute("select * from entries where type = '支出'").fetchall())
    linked_totals = {}
    for row in conn.execute(
        "select refund_pair_id, sum(amount_cents) s from entries where type = '退款' and refund_pair_id is not null group by refund_pair_id"
    ):
        linked_totals[row["refund_pair_id"]] = row["s"] or 0
    report = {"auto": [], "manual": [], "no_candidate": []}
    for rf in refunds:
        cands = []
        for e in expenses:
            if linked_totals.get(e["id"], 0) >= abs(int(e["amount_cents"] or 0)):
                continue  # 已全额退款，不再接收
            ev = refund_evidence(rf, e)
            if ev is not None:
                cands.append((ev["score"], e, ev["tags"], ev["identity"]))
        cands.sort(key=lambda x: -x[0])
        if not cands or not cands[0][3]:
            report["no_candidate"].append({"refund_id": rf["id"]})
            continue
        s1, e1, tags1, _ = cands[0]
        rivals = [c for c in cands[1:] if c[0] > 0]
        if rivals:
            margin = s1 - rivals[0][0]
            conf = _sigmoid(margin)
            auto = margin >= AUTO_LINK_MARGIN
        else:
            margin = None
            conf = _sigmoid(s1)
            auto = s1 >= AUTO_LINK_SINGLE
        if not auto:
            report["manual"].append({
                "refund_id": rf["id"],
                "best_score": round(s1, 2),
                "confidence": round(conf, 3),
                "best_expense_id": e1["id"],
                "tags": tags1,
            })
            continue
        expense_row = conn.execute("select * from entries where id = ? and type = '支出'", (e1["id"],)).fetchone()
        conn.execute(
            "update entries set refund_pair_id = ?, category = ?, status = 'confirmed' where id = ?",
            (e1["id"], expense_row["category"] or rf["category"], rf["id"]),
        )
        linked_totals[e1["id"]] = linked_totals.get(e1["id"], 0) + abs(int(rf["amount_cents"] or 0))
        report["auto"].append({
            "refund_id": rf["id"],
            "expense_id": e1["id"],
            "score": round(s1, 2),
            "confidence": round(conf, 3),
            "tags": tags1,
        })
    conn.commit()
    return report


def _sigmoid(x):
    if x < -700:
        return 0.0
    return 1 / (1 + math.exp(-x))


def refund_candidates(merchant=None, ttime=None, amount=None, account=None, limit=30):
    """退款关联候选支出：只列退款之前的支出，按匹配度排序。

    付款必然发生在退款前，因此所有候选（含最近支出兜底）严格早于退款时间。
    匹配度：金额相同(+4) > 同商户(+2) > 同账户(+1，退款原路返回)，
    同分按时间距离升序（越接近退款时刻越靠前）。score>0 进 matches，
    score=0 归入 recent 兜底。每项附 tags 供前端展示命中原因。
    """
    conn = connect()
    try:
        merchant = str(merchant or "").strip()
        account = str(account or "").strip()
        ttime_str = str(ttime or "").strip()[:19]
        try:
            cents = abs(int(amount))
        except (TypeError, ValueError):
            cents = 0

        time_filter = "transaction_time < ?" if ttime_str else "1=1"
        time_param = (ttime_str,) if ttime_str else ()
        rows = rows_to_dicts(conn.execute(
            f"""
            select id, transaction_time, amount_cents, category, transaction_object, account
            from entries
            where type = '支出' and {time_filter}
            order by transaction_time desc limit 300
            """,
            time_param,
        ).fetchall())

        # 附带已关联退款数，前端提示"已被其他退款关联"
        linked = {}
        for row in conn.execute(
            "select refund_pair_id, count(*) c from entries where type = '退款' and refund_pair_id is not null group by refund_pair_id"
        ).fetchall():
            linked[row["refund_pair_id"]] = row["c"]

        matches, recent = [], []
        for r in rows:
            tags, score = [], 0
            if cents > 0 and abs(r["amount_cents"]) == cents:
                score += 4
                tags.append("金额相同")
            if merchant and r["transaction_object"] == merchant:
                score += 2
                tags.append("同商户")
            if account and r["account"] == account:
                score += 1
                tags.append("同账户")
            dist = abs(_time_distance(r["transaction_time"], ttime_str)) if ttime_str else 0
            item = dict(r)
            item["score"] = score
            item["tags"] = tags
            item["time_distance"] = dist
            # 仅同账户(弱信号)不足以进匹配组，与无命中一起归入 recent 兜底
            if score >= 2:
                matches.append(item)
            else:
                recent.append(item)

        matches.sort(key=lambda x: (-x["score"], x["time_distance"]))
        matches = matches[:limit]
        recent = recent[:15]
        for item in matches + recent:
            item["linked_refund_count"] = linked.get(item["id"], 0)
        return {"matches": matches, "recent": recent}
    finally:
        conn.close()


def _time_distance(a, b):
    try:
        ta = dt.datetime.fromisoformat(str(a)[:19])
        tb = dt.datetime.fromisoformat(str(b)[:19])
        return (ta - tb).total_seconds()
    except Exception:
        return 0


def transfer(from_account, to_account, amount, note=None, transaction_time=None, currency="CNY"):
    amount_cents = cents(amount)
    if from_account == to_account:
        raise ValueError("transfer accounts must differ")
    init_db()
    conn = connect()
    try:
        ensure_account(conn, from_account, currency)
        ensure_account(conn, to_account, currency)
        ts = now_iso()
        tx_time = transaction_time or ts
        conn.execute(
            """
            insert into entries(transaction_time, type, category, amount_cents, currency, account,
                target_account, participant, note)
            values (?, '转账', '转账', ?, ?, ?, ?, '自己', ?)
            """,
            (tx_time, amount_cents, currency, from_account, to_account, note or "账户间转账"),
        )
        update_account_balance(conn, from_account, "转账", amount_cents)
        update_account_balance(conn, to_account, "收入", amount_cents)
        sync_account_debts_from_entries(conn)
        sync_current_balances_from_entries(conn)
        conn.commit()
    finally:
        conn.close()


def balance_correction_transaction_time(conn, account):
    row = conn.execute(
        """
        select transaction_time
        from entries
        where type != '余额修正'
            and (account = ? or target_account = ?)
        order by transaction_time desc, id desc
        limit 1
        """,
        (account, account),
    ).fetchone()
    latest = parse_transaction_time(row["transaction_time"]) if row else None
    if not latest:
        return now_iso()
    return (dt.datetime.fromisoformat(latest) + dt.timedelta(seconds=1)).isoformat(sep=" ")


def correct_balance(account, actual, note=None):
    init_db()
    conn = connect()
    try:
        ensure_account(conn, account)
        row = conn.execute("select current_balance_cents from accounts where name = ?", (account,)).fetchone()
        actual_cents = cents(actual)
        delta = actual_cents - row["current_balance_cents"]
        if delta == 0:
            return {"delta": 0, "entry_id": None}
        ts = balance_correction_transaction_time(conn, account)
        conn.execute(
            """
            insert into entries(transaction_time, type, category, amount_cents, currency, account, participant,
                balance_delta_cents, actual_balance_cents, note)
            values (?, '余额修正', '余额修正', ?, 'CNY', ?, '自己', ?, ?, ?)
            """,
            (
                ts,
                abs(delta),
                account,
                delta,
                actual_cents,
                note or "手动余额修正",
            ),
        )
        entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        conn.execute(
            "update accounts set current_balance_cents = ?, updated_at = ? where name = ?",
            (actual_cents, now_iso(), account),
        )
        sync_current_balances_from_entries(conn)
        conn.commit()
        return {"delta": delta, "entry_id": entry_id, "transaction_time": ts}
    finally:
        conn.close()


def read_standard_csv(path):
    if Path(path).suffix.lower() != ".csv":
        raise SystemExit("import only accepts a cleaned standard CSV. Convert raw bills before importing.")
    data = Path(path).read_bytes()
    for enc in ["utf-8-sig", "gb18030", "utf-8"]:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def parse_transaction_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        if number > 25569:
            parsed = dt.datetime(1899, 12, 30) + dt.timedelta(days=number)
            return parsed.replace(microsecond=0).isoformat(sep=" ")
    normalized = text
    normalized = re.sub(r"[年月]", "-", normalized)
    normalized = normalized.replace("日", " ")
    normalized = normalized.replace("/", "-")
    normalized = normalized.replace("T", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"^(\d{4})-(\d{1,2})-(\d{1,2})(.*)$", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}{m.group(4)}", normalized)
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(normalized, fmt)
            return parsed.replace(microsecond=0).isoformat(sep=" ")
        except ValueError:
            pass
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        return parsed.replace(microsecond=0).isoformat(sep=" ")
    except ValueError:
        return None


def normalize_import_record(record, row_number):
    normalized = {}
    for key, value in record.items():
        clean_key = str(key or "").strip()
        clean_value = value.strip() if isinstance(value, str) else value
        canonical_key = clean_key
        for target, aliases in IMPORT_FIELD_ALIASES.items():
            if clean_key in aliases:
                canonical_key = target
                break
        if canonical_key and (canonical_key not in normalized or normalized[canonical_key] in (None, "")):
            normalized[canonical_key] = clean_value
    parsed_time = parse_transaction_time(normalized.get("transaction_time"))
    if not parsed_time:
        columns = ", ".join(str(key).strip() for key in record.keys())
        raise SystemExit(f"row {row_number} missing or invalid transaction_time; columns: {columns}")
    normalized["transaction_time"] = parsed_time
    return normalized


def parse_category_path(value):
    text = str(value or "").strip()
    if not text:
        return None, None, None
    text = re.sub(r"\s+", "", text)
    parts = [part for part in re.split(r"[/／>＞|｜]", text) if part]
    if len(parts) >= 2:
        level1, level2 = parts[0], "/".join(parts[1:])
        return level1, level2, f"{level1}/{level2}"
    return parts[0], None, parts[0]


def import_file(path, default_account=None, duplicate_account_map=None):
    return import_file_with_options(path, default_account=default_account, duplicate_account_map=duplicate_account_map)


def imported_status_counts(entry_ids, unconfirmed_ids):
    counts = {"confirmed": 0, "pending": 0, "unconfirmed": 0}
    entry_ids = [int(entry_id) for entry_id in entry_ids]
    unconfirmed_ids = [int(entry_id) for entry_id in unconfirmed_ids]
    conn = connect()
    try:
        if entry_ids:
            rows = conn.execute(
                f"select status, count(*) c from entries where id in ({', '.join('?' for _ in entry_ids)}) group by status",
                tuple(entry_ids),
            ).fetchall()
            for row in rows:
                status = row["status"] or "confirmed"
                counts[status if status in {"confirmed", "pending"} else "confirmed"] += row["c"]
        if unconfirmed_ids:
            counts["unconfirmed"] = conn.execute(
                f"select count(*) c from unconfirmed_entries where id in ({', '.join('?' for _ in unconfirmed_ids)})",
                tuple(unconfirmed_ids),
            ).fetchone()["c"]
        return counts
    finally:
        conn.close()


def import_file_with_options(path, default_account=None, duplicate_account_map=None):
    rows = read_standard_csv(path)
    rows = [normalize_import_record(record, index) for index, record in enumerate(rows, start=2)]
    balance_correction_rows = [
        str(index)
        for index, record in enumerate(rows, start=2)
        if str(record.get("type") or "").strip() == "余额修正"
    ]
    if balance_correction_rows:
        preview = ", ".join(balance_correction_rows[:10])
        suffix = "..." if len(balance_correction_rows) > 10 else ""
        raise SystemExit(
            f"standard CSV import does not accept type=余额修正; use correct-balance instead. rows: {preview}{suffix}"
        )
    init_db()
    counts = {"confirmed": 0, "pending": 0, "unconfirmed": 0}
    preexisting_accounts = existing_account_names()
    imported_entry_ids = []
    imported_unconfirmed_ids = []
    conn = connect()
    try:
        for record in rows:
            if default_account and not record.get("account"):
                record["account"] = default_account
            result = insert_entry_with_conn(conn, record, raw_payload=record)
            counts[result["status"]] += 1
            if result["status"] in {"confirmed", "pending"}:
                imported_entry_ids.append(result["id"])
            elif result["status"] == "unconfirmed":
                imported_unconfirmed_ids.append(result["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    zero_amount_deleted = cleanup_imported_zero_amount_entries(
        imported_entry_ids,
        imported_unconfirmed_ids,
        preexisting_accounts=preexisting_accounts,
    )
    if zero_amount_deleted:
        counts["zero_amount_deleted"] = zero_amount_deleted
        counts["confirmed"] = max(0, counts["confirmed"] - zero_amount_deleted.get("confirmed", 0))
        counts["unconfirmed"] = max(0, counts["unconfirmed"] - zero_amount_deleted.get("unconfirmed", 0))
        imported_entry_ids = [entry_id for entry_id in imported_entry_ids if entry_id not in zero_amount_deleted.get("entry_ids", [])]
        imported_unconfirmed_ids = [
            entry_id for entry_id in imported_unconfirmed_ids
            if entry_id not in zero_amount_deleted.get("unconfirmed_ids", [])
        ]
    duplicate_account_map = normalize_duplicate_account_map(duplicate_account_map)
    confirmed_duplicates = dedupe_imported_entries_by_time(
        imported_entry_ids,
        preexisting_accounts=preexisting_accounts,
        account_alias_map=duplicate_account_map,
    )
    unconfirmed_duplicates = dedupe_imported_unconfirmed_by_time(
        imported_unconfirmed_ids,
        preexisting_accounts=preexisting_accounts,
        account_alias_map=duplicate_account_map,
    )
    duplicates = merge_duplicate_stats(confirmed_duplicates, unconfirmed_duplicates)
    counts.update(imported_status_counts(imported_entry_ids, imported_unconfirmed_ids))
    if duplicates:
        confirmed_deduped = duplicate_total(confirmed_duplicates)
        unconfirmed_deduped = duplicate_total(unconfirmed_duplicates)
        counts["deduped"] = confirmed_deduped + unconfirmed_deduped
        counts["duplicate_accounts"] = duplicate_account_counts(duplicates)
        counts["duplicate_account_pairs"] = duplicate_account_pairs(duplicates)
        counts["notices"] = duplicate_notices(duplicates)
    if zero_amount_deleted:
        counts.setdefault("notices", []).append(
            f"导入发现 0 金额记录【{zero_amount_deleted['total']}】条，已自动删除。"
        )
    dedupe_entries()
    conn = connect()
    try:
        sync_account_debts_from_entries(conn)
        sync_current_balances_from_entries(conn)
        conn.commit()
    finally:
        conn.close()
    return counts


def normalize_duplicate_account_map(value):
    if not value:
        return {}
    if isinstance(value, (str, Path)):
        path = Path(value)
        text = path.read_text(encoding="utf-8") if path.exists() else str(value)
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("duplicate account map must be a JSON object")
    return {
        str(source or "").strip(): str(target or "").strip()
        for source, target in value.items()
        if str(source or "").strip() and str(target or "").strip()
    }


def accounts_match_for_duplicate(imported_account, existing_account, account_alias_map=None):
    imported = str(imported_account or "").strip()
    existing = str(existing_account or "").strip()
    if not imported or not existing:
        return imported == existing
    if imported == existing:
        return True
    account_alias_map = account_alias_map or {}
    return account_alias_map.get(imported) == existing


def import_duplicate_analysis(path, default_account=None, tolerance_seconds=2):
    rows = read_standard_csv(path)
    normalized_rows = [normalize_import_record(record, index) for index, record in enumerate(rows, start=2)]
    conn = connect()
    try:
        candidates = []
        imported_accounts = collections.Counter()
        existing_accounts = collections.Counter(
            row["account"]
            for row in conn.execute("select account from entries where coalesce(account, '') != ''").fetchall()
        )
        for index, record in enumerate(normalized_rows, start=2):
            if default_account and not record.get("account"):
                record["account"] = default_account
            normalized = normalize_record(conn, record)
            account = str(normalized.get("account") or "").strip()
            imported_accounts[account or "未命名账户"] += 1
            imported_ts = entry_timestamp(normalized.get("transaction_time"))
            if imported_ts is None:
                continue
            amount_cents = abs(cents(normalized.get("amount")))
            window_start = dt.datetime.fromtimestamp(imported_ts - tolerance_seconds).isoformat(sep=" ")
            window_end = dt.datetime.fromtimestamp(imported_ts + tolerance_seconds).isoformat(sep=" ")
            matches = conn.execute(
                """
                select id, transaction_time, type, category, amount_cents, account, target_account,
                    transaction_object, note
                from entries
                where transaction_time between ? and ? and amount_cents = ?
                order by abs(strftime('%s', transaction_time) - ?), id
                """,
                (window_start, window_end, amount_cents, int(imported_ts)),
            ).fetchall()
            for match in matches:
                candidates.append({
                    "row_number": index,
                    "imported_time": normalized.get("transaction_time"),
                    "existing_id": match["id"],
                    "existing_time": match["transaction_time"],
                    "time_delta_seconds": abs(entry_timestamp(match["transaction_time"]) - imported_ts),
                    "amount": money(amount_cents),
                    "imported_account": account,
                    "existing_account": match["account"],
                    "imported_type": normalized.get("type"),
                    "existing_type": match["type"],
                    "imported_object": normalized.get("transaction_object"),
                    "existing_object": match["transaction_object"],
                    "imported_note": normalized.get("note"),
                    "existing_note": match["note"],
                })
    finally:
        conn.close()

    pair_counts = collections.Counter(
        (item["imported_account"] or "未命名账户", item["existing_account"] or "未命名账户")
        for item in candidates
    )
    return {
        "tolerance_seconds": tolerance_seconds,
        "imported_accounts": dict(imported_accounts),
        "existing_accounts": dict(existing_accounts),
        "overlapping_account_pairs": [
            {
                "imported_account": imported,
                "existing_account": existing,
                "candidate_count": count,
            }
            for (imported, existing), count in pair_counts.most_common()
        ],
        "candidates": candidates,
        "account_map_format": {"新导入账户名": "数据库已有账户名"},
        "guidance": "代码只列出完整交易时间相差不超过 2 秒且金额一致的候选。只把能确认同一资金账户的名称对写入 duplicate_account_map；不确定时不要写入。",
    }


def standard_csv_analysis(path, default_account=None):
    rows = read_standard_csv(path)
    normalized_rows = [normalize_import_record(record, index) for index, record in enumerate(rows, start=2)]
    init_db()
    conn = connect()
    try:
        stats = {
            "path": str(path),
            "total_rows": len(normalized_rows),
            "types": collections.Counter(),
            "accounts": collections.Counter(),
            "target_accounts": collections.Counter(),
            "categories": collections.Counter(),
            "missing": collections.Counter(),
            "invalid_amount_rows": [],
            "zero_amount_rows": [],
        }
        for index, record in enumerate(normalized_rows, start=2):
            if default_account and not record.get("account"):
                record["account"] = default_account
            try:
                normalized = normalize_record(conn, record)
                amount_cents = abs(cents(normalized.get("amount")))
            except Exception as exc:
                stats["invalid_amount_rows"].append({"row_number": index, "error": str(exc)})
                continue
            tx_type = normalized.get("type") or "未指定"
            account = str(normalized.get("account") or "").strip() or "未命名账户"
            target_account = str(normalized.get("target_account") or "").strip()
            category = str(normalized.get("category") or "").strip() or "未分类"
            stats["types"][tx_type] += 1
            stats["accounts"][account] += 1
            if target_account:
                stats["target_accounts"][target_account] += 1
            stats["categories"][category] += 1
            if not amount_cents:
                stats["zero_amount_rows"].append(index)
            if not normalized.get("type"):
                stats["missing"]["type"] += 1
            if not normalized.get("account"):
                stats["missing"]["account"] += 1
            if normalized.get("type") == "转账" and not normalized.get("target_account"):
                stats["missing"]["target_account_for_transfer"] += 1
            if normalized.get("type") == "转账" and normalized.get("target_account") and not is_internal_transfer_record(conn, normalized):
                stats["missing"]["known_internal_transfer_accounts"] += 1
            if normalized.get("type") not in {"转账", "余额修正"} and not normalized.get("category"):
                stats["missing"]["category"] += 1
        return {
            "path": stats["path"],
            "total_rows": stats["total_rows"],
            "types": dict(stats["types"].most_common()),
            "accounts": dict(stats["accounts"].most_common()),
            "target_accounts": dict(stats["target_accounts"].most_common()),
            "categories": dict(stats["categories"].most_common()),
            "missing": dict(stats["missing"].most_common()),
            "invalid_amount_rows": stats["invalid_amount_rows"][:50],
            "invalid_amount_count": len(stats["invalid_amount_rows"]),
            "zero_amount_rows": stats["zero_amount_rows"][:50],
            "zero_amount_count": len(stats["zero_amount_rows"]),
            "standard_import_fields": [
                "transaction_time",
                "type",
                "category_level1",
                "category_level2",
                "category",
                "amount",
                "currency",
                "transaction_object",
                "account",
                "target_account",
                "participant",
                "note",
                "source",
            ],
        }
    finally:
        conn.close()


def normalize_keyword_rules(value):
    if not value:
        return []
    if isinstance(value, (str, Path)):
        path = Path(value)
        text = path.read_text(encoding="utf-8") if path.exists() else str(value)
        value = json.loads(text)
    if not isinstance(value, list):
        raise ValueError("keyword rules must be a JSON array")
    rules = []
    for index, rule in enumerate(value, start=1):
        if not isinstance(rule, dict):
            raise ValueError(f"keyword rule {index} must be an object")
        keyword = str(rule.get("keyword") or "").strip()
        category = standardize_category(rule.get("category"), rule.get("type"))
        tx_type = str(rule.get("type") or "").strip()
        if not keyword:
            raise ValueError(f"keyword rule {index} missing keyword")
        if tx_type and tx_type not in ENTRY_TYPES:
            raise ValueError(f"keyword rule {index} has invalid type")
        if not category:
            raise ValueError(f"keyword rule {index} has invalid category")
        rules.append({"keyword": keyword, "category": category, "type": tx_type})
    return rules


def db_keyword_rules(conn):
    return [
        {"keyword": row["merchant"], "category": row["category"], "type": row["type"] or ""}
        for row in conn.execute(
            "select merchant, category, type from merchant_category_rules order by length(merchant) desc"
        ).fetchall()
    ]


def standard_row_raw_category(record):
    return "/".join(
        part
        for part in [
            str(record.get("category_level1") or "").strip(),
            str(record.get("category_level2") or "").strip(),
        ]
        if part
    ) or str(record.get("category") or "").strip()


def keyword_candidate_analysis(path, default_account=None):
    rows = read_standard_csv(path)
    normalized_rows = [normalize_import_record(record, index) for index, record in enumerate(rows, start=2)]
    min_count = max(3, math.ceil(len(normalized_rows) * 0.02))
    init_db()
    conn = connect()
    try:
        object_counter = collections.Counter()
        note_counter = collections.Counter()
        raw_category_counter = collections.Counter()
        examples = {}
        unmatched_count = 0
        db_rule_applied = 0
        for index, record in enumerate(normalized_rows, start=2):
            if default_account and not record.get("account"):
                record["account"] = default_account
            without_rules = normalize_record(conn, record, apply_rules=False)
            normalized = normalize_record(conn, record, apply_rules=True)
            if not without_rules.get("category") and normalized.get("category"):
                db_rule_applied += 1
            if normalized.get("type") in {"转账", "余额修正"} or normalized.get("category"):
                continue
            unmatched_count += 1
            fields = {
                "transaction_object": str(normalized.get("transaction_object") or "").strip(),
                "note": str(normalized.get("note") or "").strip(),
                "raw_category": standard_row_raw_category(record),
                "type": str(normalized.get("type") or "").strip(),
                "account": str(normalized.get("account") or "").strip(),
                "amount": str(normalized.get("amount") or "").strip(),
                "transaction_time": str(normalized.get("transaction_time") or "").strip(),
            }
            for key, counter in [
                ("transaction_object", object_counter),
                ("note", note_counter),
                ("raw_category", raw_category_counter),
            ]:
                value = fields[key]
                if value:
                    counter[value] += 1
                    examples.setdefault(key, {}).setdefault(value, fields | {"row_number": index})

        def top(counter, key):
            return [
                {"value": value, "count": count, "example": examples.get(key, {}).get(value)}
                for value, count in counter.most_common()
                if count >= min_count
            ]

        return {
            "path": str(path),
            "total_rows": len(normalized_rows),
            "db_rule_applied": db_rule_applied,
            "unmatched_count": unmatched_count,
            "min_count": min_count,
            "min_count_formula": "max(3, ceil(total_rows * 0.02))",
            "candidates": {
                "transaction_object": top(object_counter, "transaction_object"),
                "note": top(note_counter, "note"),
                "raw_category": top(raw_category_counter, "raw_category"),
            },
            "rule_format": [
                {"keyword": "地铁", "category": "交通/公共交通", "type": "支出"}
            ],
            "guidance": (
                "Group candidates by type first. Only return high-confidence, reusable keyword rules. "
                "Use transaction_object directly only when it clearly represents a stable single category. "
                "Do not create unconditional rules for broad platform names such as 美团, 京东 or 淘宝; "
                "inspect note details for concrete brands or merchants instead. Prefer specific stable brand keywords, "
                "and do not use personal names, platform names, order ids, one-off full titles, or ambiguous words."
            ),
        }
    finally:
        conn.close()


def apply_keyword_rules_to_standard_csv(path, output, keyword_rules):
    rules = normalize_keyword_rules(keyword_rules)
    rows = read_standard_csv(path)
    if not rows:
        fieldnames = [
            "transaction_time",
            "type",
            "category_level1",
            "category_level2",
            "category",
            "amount",
            "currency",
            "transaction_object",
            "account",
            "target_account",
            "participant",
            "note",
            "source",
        ]
    else:
        fieldnames = list(rows[0].keys())
    updated = 0
    matched_rules = collections.Counter()
    for row in rows:
        text = " ".join(str(row.get(field) or "") for field in ["transaction_object", "note"])
        if row.get("category"):
            continue
        for rule in rules:
            if rule["keyword"] not in text:
                continue
            row["category"] = rule["category"]
            if rule["type"]:
                row["type"] = rule["type"]
            updated += 1
            matched_rules[rule["keyword"]] += 1
            break
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "output": str(output_path),
        "rules": len(rules),
        "updated_rows": updated,
        "matched_rules": dict(matched_rules.most_common()),
    }


def unconfirmed_summary(limit=20):
    init_db()
    conn = connect()
    try:
        rows = conn.execute(
            """
            select id, transaction_time, type, category, amount_cents, account, target_account,
                transaction_object, note, null raw_payload
            from entries
            where status = 'pending'
            union all
            select id, transaction_time, type, category, amount_cents, account, target_account,
                transaction_object, note, raw_payload
            from unconfirmed_entries
            order by transaction_time desc, id desc
            """
        ).fetchall()
        reasons = collections.Counter()
        by_type = collections.Counter()
        by_account = collections.Counter()
        by_raw_category = collections.Counter()
        for row in rows:
            tx_type = row["type"] or "未指定"
            by_type[tx_type] += 1
            by_account[(row["account"] or "未命名账户")] += 1
            payload = parse_attributes(row["raw_payload"])
            raw_category = (
                "/".join(part for part in [payload.get("category_level1"), payload.get("category_level2")] if part)
                or payload.get("category")
                or "未指定"
            )
            by_raw_category[str(raw_category)] += 1
            if not row["type"]:
                reasons["missing_type"] += 1
            if not row["account"]:
                reasons["missing_account"] += 1
            if row["type"] == "转账" and not row["target_account"]:
                reasons["missing_target_account_for_transfer"] += 1
            if row["type"] == "转账" and row["target_account"] and not is_internal_transfer_record(conn, dict(row)):
                reasons["unknown_internal_transfer_accounts"] += 1
            if row["type"] not in {"转账", "余额修正"} and not row["category"]:
                reasons["missing_category"] += 1
        return {
            "total": len(rows),
            "reasons": dict(reasons.most_common()),
            "by_type": dict(by_type.most_common()),
            "by_account": dict(by_account.most_common(limit)),
            "by_raw_category": dict(by_raw_category.most_common(limit)),
        }
    finally:
        conn.close()


def existing_account_names():
    conn = connect()
    try:
        names = set(config_account_names())
        names.update(row["name"] for row in conn.execute("select name from accounts").fetchall())
        return names
    finally:
        conn.close()


def entry_timestamp(value):
    parsed = parse_transaction_time(value)
    if not parsed:
        return None
    return dt.datetime.fromisoformat(parsed).timestamp()


def new_duplicate_stats():
    return collections.defaultdict(collections.Counter)


def record_duplicate(stats, removed_account, kept_account):
    removed = str(removed_account or "").strip() or "未命名账户"
    kept = str(kept_account or "").strip() or "未命名账户"
    stats[removed][kept] += 1


def merge_duplicate_stats(*stats_items):
    merged = new_duplicate_stats()
    for stats in stats_items:
        for removed_account, kept_accounts in stats.items():
            merged[removed_account].update(kept_accounts)
    return {account: dict(kept_accounts) for account, kept_accounts in merged.items() if kept_accounts}


def duplicate_total(stats):
    return sum(sum(kept_accounts.values()) for kept_accounts in stats.values())


def duplicate_account_counts(stats):
    return {account: sum(kept_accounts.values()) for account, kept_accounts in stats.items()}


def duplicate_account_pairs(stats):
    return {account: dict(kept_accounts) for account, kept_accounts in stats.items()}


def duplicate_notices(stats):
    notices = []
    for removed_account, kept_accounts in stats.items():
        total = sum(kept_accounts.values())
        if len(kept_accounts) == 1:
            kept_account = next(iter(kept_accounts.keys()))
            notices.append(f"识别到来自【{removed_account}】重复交易【{total}】笔，已合并到【{kept_account}】并去重！")
            continue
        detail = "、".join(f"【{account}】{count}笔" for account, count in kept_accounts.items())
        notices.append(f"识别到来自【{removed_account}】重复交易【{total}】笔，已分别合并到{detail}并去重！")
    return notices


def rollback_entry_balance(conn, entry):
    update_account_balance(conn, entry["account"], entry["type"], -entry["amount_cents"], entry["balance_delta_cents"])
    if entry["type"] == "转账" and entry["target_account"]:
        update_account_balance(conn, entry["target_account"], "收入", -entry["amount_cents"])


def cleanup_unused_import_account(conn, account, preexisting_accounts):
    account = str(account or "").strip()
    if not account or account in preexisting_accounts:
        return
    row = conn.execute(
        "select initial_balance_cents, current_balance_cents from accounts where name = ?",
        (account,),
    ).fetchone()
    if not row or row["initial_balance_cents"] != 0 or row["current_balance_cents"] != 0:
        return
    usage = conn.execute(
        """
        select
            (select count(*) from entries where account = ? or target_account = ?) +
            (select count(*) from unconfirmed_entries where account = ? or target_account = ?) as c
        """,
        (account, account, account, account),
    ).fetchone()["c"]
    if usage == 0:
        conn.execute("delete from accounts where name = ?", (account,))


def cleanup_imported_zero_amount_entries(imported_entry_ids, imported_unconfirmed_ids, preexisting_accounts=None):
    preexisting_accounts = preexisting_accounts or set()
    entry_ids = [int(entry_id) for entry_id in imported_entry_ids]
    unconfirmed_ids = [int(entry_id) for entry_id in imported_unconfirmed_ids]
    removed_entry_ids = []
    removed_unconfirmed_ids = []
    conn = connect()
    try:
        if entry_ids:
            rows = conn.execute(
                f"select * from entries where id in ({', '.join('?' for _ in entry_ids)}) and amount_cents = 0",
                tuple(entry_ids),
            ).fetchall()
            for row in rows:
                rollback_entry_balance(conn, row)
                conn.execute("delete from entries where id = ?", (row["id"],))
                cleanup_unused_import_account(conn, row["account"], preexisting_accounts)
                cleanup_unused_import_account(conn, row["target_account"], preexisting_accounts)
                removed_entry_ids.append(row["id"])
        if unconfirmed_ids:
            rows = conn.execute(
                f"select * from unconfirmed_entries where id in ({', '.join('?' for _ in unconfirmed_ids)}) and amount_cents = 0",
                tuple(unconfirmed_ids),
            ).fetchall()
            for row in rows:
                conn.execute("delete from unconfirmed_entries where id = ?", (row["id"],))
                cleanup_unused_import_account(conn, row["account"], preexisting_accounts)
                cleanup_unused_import_account(conn, row["target_account"], preexisting_accounts)
                removed_unconfirmed_ids.append(row["id"])
        conn.commit()
    finally:
        conn.close()
    total = len(removed_entry_ids) + len(removed_unconfirmed_ids)
    if not total:
        return {}
    return {
        "confirmed": len(removed_entry_ids),
        "unconfirmed": len(removed_unconfirmed_ids),
        "total": total,
        "entry_ids": removed_entry_ids,
        "unconfirmed_ids": removed_unconfirmed_ids,
    }


def dedupe_imported_entries_by_time(imported_entry_ids, tolerance_seconds=2, preexisting_accounts=None, account_alias_map=None):
    if not imported_entry_ids:
        return {}
    preexisting_accounts = preexisting_accounts or set()
    imported_ids = {int(entry_id) for entry_id in imported_entry_ids}
    conn = connect()
    try:
        imported_rows = conn.execute(
            f"select * from entries where id in ({', '.join('?' for _ in imported_ids)}) order by id",
            tuple(imported_ids),
        ).fetchall()
        if not imported_rows:
            return {}
        timestamps = [entry_timestamp(row["transaction_time"]) for row in imported_rows]
        timestamps = [item for item in timestamps if item is not None]
        if not timestamps:
            return {}
        window_start = dt.datetime.fromtimestamp(min(timestamps) - tolerance_seconds).isoformat(sep=" ")
        window_end = dt.datetime.fromtimestamp(max(timestamps) + tolerance_seconds).isoformat(sep=" ")
        candidates = conn.execute(
            """
            select * from entries
            where transaction_time between ? and ?
            order by id
            """,
            (window_start, window_end),
        ).fetchall()
        candidate_data = [(row, entry_timestamp(row["transaction_time"])) for row in candidates]
        candidate_data = [(row, ts) for row, ts in candidate_data if ts is not None]
        duplicate_stats = new_duplicate_stats()
        removed_ids = set()
        for current in imported_rows:
            current_id = int(current["id"])
            if current_id in removed_ids:
                continue
            current_ts = entry_timestamp(current["transaction_time"])
            if current_ts is None:
                continue
            earlier = [
                row for row, ts in candidate_data
                if int(row["id"]) < current_id
                and int(row["id"]) not in imported_ids
                and int(row["id"]) not in removed_ids
                and abs(ts - current_ts) <= tolerance_seconds
                and row["amount_cents"] == current["amount_cents"]
                and accounts_match_for_duplicate(current["account"], row["account"], account_alias_map)
            ]
            if not earlier:
                continue
            kept = earlier[0]
            record_duplicate(duplicate_stats, current["account"], kept["account"])
            rollback_entry_balance(conn, current)
            conn.execute("delete from entries where id = ?", (current_id,))
            cleanup_unused_import_account(conn, current["account"], preexisting_accounts)
            cleanup_unused_import_account(conn, current["target_account"], preexisting_accounts)
            removed_ids.add(current_id)
        conn.commit()
        return merge_duplicate_stats(duplicate_stats)
    finally:
        conn.close()


def dedupe_imported_unconfirmed_by_time(imported_unconfirmed_ids, tolerance_seconds=2, preexisting_accounts=None, account_alias_map=None):
    if not imported_unconfirmed_ids:
        return {}
    preexisting_accounts = preexisting_accounts or set()
    imported_ids = {int(entry_id) for entry_id in imported_unconfirmed_ids}
    conn = connect()
    try:
        imported_rows = conn.execute(
            f"select * from unconfirmed_entries where id in ({', '.join('?' for _ in imported_ids)}) order by id",
            tuple(imported_ids),
        ).fetchall()
        if not imported_rows:
            return {}
        timestamps = [entry_timestamp(row["transaction_time"]) for row in imported_rows]
        timestamps = [item for item in timestamps if item is not None]
        if not timestamps:
            return {}
        window_start = dt.datetime.fromtimestamp(min(timestamps) - tolerance_seconds).isoformat(sep=" ")
        window_end = dt.datetime.fromtimestamp(max(timestamps) + tolerance_seconds).isoformat(sep=" ")
        candidates = conn.execute(
            """
            select * from entries
            where transaction_time between ? and ?
            order by id
            """,
            (window_start, window_end),
        ).fetchall()
        candidate_data = [(row, entry_timestamp(row["transaction_time"])) for row in candidates]
        candidate_data = [(row, ts) for row, ts in candidate_data if ts is not None]
        duplicate_stats = new_duplicate_stats()
        for current in imported_rows:
            current_ts = entry_timestamp(current["transaction_time"])
            if current_ts is None:
                continue
            earlier = [
                row for row, ts in candidate_data
                if abs(ts - current_ts) <= tolerance_seconds
                and row["amount_cents"] == current["amount_cents"]
                and accounts_match_for_duplicate(current["account"], row["account"], account_alias_map)
            ]
            if not earlier:
                continue
            kept = earlier[0]
            record_duplicate(duplicate_stats, current["account"], kept["account"])
            conn.execute("delete from unconfirmed_entries where id = ?", (current["id"],))
            cleanup_unused_import_account(conn, current["account"], preexisting_accounts)
            cleanup_unused_import_account(conn, current["target_account"], preexisting_accounts)
        conn.commit()
        return merge_duplicate_stats(duplicate_stats)
    finally:
        conn.close()


def dedupe_entries():
    conn = connect()
    rows = conn.execute(
        """
        select min(id) keep_id, group_concat(id) ids, count(*) c
        from entries
        group by transaction_time, type, amount_cents, account, coalesce(category, ''),
            coalesce(target_account, ''), coalesce(transaction_object, ''), coalesce(note, '')
        having c > 1
        """
    ).fetchall()
    removed = 0
    for row in rows:
        ids = [int(x) for x in row["ids"].split(",") if int(x) != row["keep_id"]]
        for entry_id in ids:
            entry = conn.execute("select account, target_account, type, amount_cents, balance_delta_cents from entries where id = ?", (entry_id,)).fetchone()
            if entry:
                rollback_entry_balance(conn, entry)
            conn.execute("delete from entries where id = ?", (entry_id,))
            removed += 1
    conn.commit()
    conn.close()
    return removed


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def account_entry_delta_for(account, row):
    if row["type"] == "转账":
        if row["account"] == account:
            return -row["amount_cents"]
        if row["target_account"] == account:
            return row["amount_cents"]
        return 0
    if row["account"] != account:
        return 0
    return account_balance_delta(row["type"], row["amount_cents"], row["balance_delta_cents"]) or 0


def account_computed_balances(conn):
    accounts = rows_to_dicts(conn.execute("select * from accounts order by name").fetchall())
    results = []
    for account in accounts:
        name = account["name"]
        base_cents = account["initial_balance_cents"]
        base_time = account["created_at"]
        scan_after_time = "0001-01-01 00:00:00"
        base_id = 0
        base_type = "初始化"
        correction = conn.execute(
            """
            select id, transaction_time, actual_balance_cents
            from entries
            where type = '余额修正' and account = ? and actual_balance_cents is not null
            order by transaction_time desc, id desc limit 1
            """,
            (name,),
        ).fetchone()
        if correction:
            base_cents = correction["actual_balance_cents"]
            base_time = correction["transaction_time"]
            scan_after_time = correction["transaction_time"]
            base_id = correction["id"]
            base_type = "余额修正"
        rows = conn.execute(
            """
            select id, transaction_time, type, amount_cents, account, target_account, balance_delta_cents
            from entries
            where (account = ? or target_account = ?)
                and (transaction_time > ? or (transaction_time = ? and id > ?))
            order by transaction_time, id
            """,
            (name, name, scan_after_time, scan_after_time, base_id),
        ).fetchall()
        computed = base_cents + sum(account_entry_delta_for(name, row) for row in rows)
        result = dict(account)
        result["computed_balance_cents"] = computed
        result["balance_basis"] = base_type
        result["balance_basis_time"] = base_time
        result["balance_diff_cents"] = computed - account["current_balance_cents"]
        results.append(result)
    return results


def sync_current_balances_from_entries(conn):
    ts = now_iso()
    for account in account_computed_balances(conn):
        if account["computed_balance_cents"] == account["current_balance_cents"]:
            continue
        conn.execute(
            "update accounts set current_balance_cents = ?, updated_at = ? where name = ?",
            (account["computed_balance_cents"], ts, account["name"]),
        )


def net_expense_stats(conn, month):
    """净额口径的支出统计（冲减归属原支出，跨日期/跨月退款也归位到实际支出那天）。

    返回:
        by_day: {day: {"gross": 毛支出, "offset": 退款冲减, "net": 净支出}}（仅本月日期）
        refund_by_category: {原支出分类: 冲减额}（按原支出月归属）
        totals: {"gross", "offset", "net"}
        repay_by_category: {还款分类: 金额}（还款口径负债账户的月供转账计入支出）
    规则: 已关联退款(type=退款, refund_pair_id→原支出)冲减"原支出发生日/分类"；
          未关联退款不进任何收支（只计数待关联）；
          防双计（一笔借来的钱只确认一次支出）：consume 口径花呗消费/还款统计零变化，
          仅 repay 口径负债账户的「信用卡还款/房贷/车贷」分类转账追加计入。
    """
    placeholders = ",".join("?" for _ in debt_accounts.STAT_EXPENSE_TYPES)
    by_day = {}
    for row in conn.execute(
        f"""
        select substr(transaction_time,1,10) day, sum(amount_cents) v from entries
        where type in ({placeholders}) and coalesce(category,'') != '转账' and substr(transaction_time,1,7)=?
        group by day
        """,
        debt_accounts.STAT_EXPENSE_TYPES + (month,),
    ).fetchall():
        by_day[row["day"]] = {"gross": row["v"], "offset": 0, "net": row["v"]}
    refund_by_category = {}
    # 冲减按"原支出"归属：join 原支出取日期/分类，原支出在本月才计入本月统计
    for row in conn.execute(
        """
        select substr(o.transaction_time,1,10) day,
               coalesce(nullif(o.category,''), '退款') category,
               r.amount_cents v
        from entries r join entries o on o.id = r.refund_pair_id
        where r.type = '退款' and o.type = '支出' and substr(o.transaction_time,1,7)=?
        """,
        (month,),
    ).fetchall():
        day = row["day"]
        if day in by_day:
            by_day[day]["offset"] += row["v"]
            by_day[day]["net"] -= row["v"]
        refund_by_category[row["category"]] = refund_by_category.get(row["category"], 0) + row["v"]
    # —— 第二段：还款口径负债账户的月供转账计入支出（分类∈REPAY_TRANSFER_CATEGORIES）——
    # join accounts 限定 expense_mode='repay'，花呗等 consume 口径天然排除，统计零变化；
    # 普通转账（分类='转账'）不在还款类分类，不计支出（借钱给家人还进去不会被误算成月供）
    repay_by_category = {}
    repay_placeholders = ",".join("?" for _ in debt_accounts.REPAY_TRANSFER_CATEGORIES)
    for row in conn.execute(
        f"""
        select substr(e.transaction_time,1,10) day, e.category, sum(e.amount_cents) v
        from entries e join accounts a on a.name = e.target_account
        where e.type = '转账' and a.kind = ? and a.expense_mode = ?
          and e.category in ({repay_placeholders})
          and substr(e.transaction_time,1,7)=?
        group by day, e.category
        """,
        (debt_accounts.KIND_LIABILITY, debt_accounts.EXPENSE_REPAY,)
        + debt_accounts.REPAY_TRANSFER_CATEGORIES + (month,),
    ).fetchall():
        day = row["day"]
        if day not in by_day:
            by_day[day] = {"gross": 0, "offset": 0, "net": 0}
        by_day[day]["gross"] += row["v"]
        by_day[day]["net"] += row["v"]
        repay_by_category[row["category"]] = repay_by_category.get(row["category"], 0) + row["v"]
    totals = {
        "gross": sum(v["gross"] for v in by_day.values()),
        "offset": sum(refund_by_category.values()),
    }
    totals["net"] = totals["gross"] - totals["offset"]
    return by_day, refund_by_category, totals, repay_by_category


def api_summary():
    conn = connect()
    month = dt.datetime.now().strftime("%Y-%m")
    income = conn.execute(
        f"""
        select coalesce(sum(amount_cents),0) v from entries
        where type in ({','.join('?' for _ in debt_accounts.STAT_INCOME_TYPES)}) and coalesce(category,'') != '转账' and substr(transaction_time,1,7)=?
        """,
        debt_accounts.STAT_INCOME_TYPES + (month,),
    ).fetchone()["v"]
    # 退款不算收入：净额制——已关联退款冲减"原支出"（按原支出日/分类归属，跨日期/跨月退款也归位到实际支出那天）
    # refunds 仅统计"本月发生的退款"用于展示；收支冲减一律走 net_expense_stats（按原支出归属）
    refunds = rows_to_dicts(
        conn.execute(
            """
            select id, transaction_time, transaction_object, amount_cents, refund_pair_id from entries
            where type = '退款' and substr(transaction_time,1,7)=?
            """,
            (month,),
        ).fetchall()
    )
    linked_refunds = [r for r in refunds if r["refund_pair_id"]]
    unlinked_refunds = [r for r in refunds if not r["refund_pair_id"]]
    refund_total = sum(r["amount_cents"] for r in linked_refunds)
    refund_unlinked_total = sum(r["amount_cents"] for r in unlinked_refunds)
    by_day, refund_by_category, net_totals, repay_by_category = net_expense_stats(conn, month)
    expense = net_totals["net"]
    expense_gross = net_totals["gross"]
    accounts = rows_to_dicts(conn.execute("select * from accounts order by name").fetchall())
    account_balances = account_computed_balances(conn)
    categories = {}
    for row in conn.execute(
        """
        select coalesce(category, '未分类') category, sum(amount_cents) amount_cents
        from entries where type='支出' and coalesce(category,'') != '转账' and substr(transaction_time,1,7)=?
        group by category order by amount_cents desc
        """,
        (month,),
    ).fetchall():
        categories[row["category"]] = categories.get(row["category"], 0) + row["amount_cents"]
    for cat, refund_cents in refund_by_category.items():
        categories[cat] = categories.get(cat, 0) - refund_cents
    # 还款口径月供并入分类饼图（否则月供计了月支出却不进饼图）
    for cat, repay_cents in repay_by_category.items():
        categories[cat] = categories.get(cat, 0) + repay_cents
    category_rows = [
        {"category": cat, "amount_cents": cents_val}
        for cat, cents_val in categories.items()
        if cents_val > 0
    ]
    category_rows.sort(key=lambda item: item["amount_cents"], reverse=True)
    income_placeholders = ",".join("?" for _ in debt_accounts.STAT_INCOME_TYPES)
    income_by_day = {
        row["day"]: row["v"]
        for row in conn.execute(
            f"""
            select substr(transaction_time,1,10) day, sum(amount_cents) v from entries
            where type in ({income_placeholders}) and coalesce(category,'') != '转账' and substr(transaction_time,1,7)=?
            group by day
            """,
            debt_accounts.STAT_INCOME_TYPES + (month,),
        ).fetchall()
    }
    trend = [
        {
            "day": day,
            "income_cents": income_by_day.get(day, 0),
            "expense_cents": by_day[day]["net"] if day in by_day else 0,
        }
        for day in sorted(set(income_by_day) | set(by_day))
    ]
    unconfirmed = conn.execute(
        """
        select
            (select count(*) from entries where status = 'pending') +
            (select count(*) from unconfirmed_entries) c
        """
    ).fetchone()["c"]
    invest_snapshot = investments.summary_snapshot(conn)
    # 资产管家/负债三型派生字段：debt_type（只读呈现标签）、repaid_cents/interest_cents（实时）
    repaid_map = {
        row["name"]: int(row["v"])
        for row in conn.execute(
            "select target_account name, sum(amount_cents) v from entries where type = '转账' group by target_account"
        ).fetchall()
    }
    conn.close()
    for row in accounts + account_balances:
        kind = row.get("kind") or "asset"
        if kind == "liability":
            repaid = repaid_map.get(row["name"], 0)
            principal = row.get("principal_cents")
            row["debt_type"] = debt_accounts.derive_debt_type(row.get("expense_mode"), row.get("expense_mode_locked"))
            row["repaid_cents"] = repaid
            row["interest_cents"] = (repaid - principal) if principal is not None else None
        else:
            row["debt_type"] = None
            row["repaid_cents"] = None
            row["interest_cents"] = None
    # 净资产卡（鲨鱼口径）：资产侧 = 资产账户 + 债权 + 投资市值；负债侧 = |负债余额合计|；归档≠销账，计入
    asset_total = (
        sum(r["computed_balance_cents"] for r in account_balances if (r.get("kind") or "asset") == "asset")
        + sum(r["computed_balance_cents"] for r in account_balances if r.get("kind") == "receivable")
        + int(invest_snapshot.get("invest_market_cents") or 0)
    )
    liability_total = abs(
        sum(r["computed_balance_cents"] for r in account_balances if r.get("kind") == "liability")
    )
    net_worth = {
        "asset_total_cents": int(asset_total),
        "liability_total_cents": int(liability_total),
        "net_worth_cents": int(asset_total - liability_total),
    }
    return {
        "month": month,
        "income": money(income),
        "expense": money(expense),
        "expense_gross": money(expense_gross),
        "refund": money(refund_total),
        "refund_gross": money(refund_total + refund_unlinked_total),
        "refund_unlinked_count": len(unlinked_refunds),
        "refund_unlinked": money(refund_unlinked_total),
        "balance": money(income - expense),
        "accounts": accounts,
        "account_balances": account_balances,
        "categories": category_rows[:10],
        "trend": trend,
        "unconfirmed_count": unconfirmed,
        "net_worth": net_worth,
        **invest_snapshot,
    }


def invest_curve(conn, query_string):
    """账户/大类/标的的市值与成本随时间变化曲线（基于交易流水 + 价格历史合成）。"""
    import collections

    query = urllib.parse.parse_qs(query_string)
    scope = (query.get("scope") or ["account"])[0]
    scope_id = int(query.get("id", [0])[0])
    if scope == "asset":
        asset_ids = [r["id"] for r in conn.execute("select id from invest_assets where id = ?", (scope_id,))]
    elif scope == "category":
        asset_ids = [r["id"] for r in conn.execute(
            "select s.id id from invest_assets s join invest_accounts a on s.account_id = a.id where a.category_id = ?",
            (scope_id,))]
    else:
        asset_ids = [r["id"] for r in conn.execute("select id from invest_assets where account_id = ?", (scope_id,))]
    if not asset_ids:
        return {"ok": False, "points": []}
    marks = {}
    for r in conn.execute(
        f"select asset_id, price_date, close_cents from invest_price_history where asset_id in ({','.join('?'*len(asset_ids))})"
        " order by price_date", asset_ids):
        marks.setdefault(r["asset_id"], []).append((r["price_date"], r["close_cents"]))
    flows = collections.defaultdict(Decimal)
    events = []
    for t in conn.execute(
        f"select asset_id, trade_time, trade_type, quantity, amount_cents from invest_trades"
        f" where trade_type in ('买入','卖出') and asset_id in ({','.join('?'*len(asset_ids))}) order by trade_time",
        asset_ids):
        day = str(t["trade_time"])[:10]
        qty = investments._q(t["quantity"]) if t["quantity"] else Decimal("0")
        flows[t["asset_id"]] += qty if t["trade_type"] == "买入" else -qty
        events.append((day, dict(t)))
    if not events:
        return {"ok": False, "points": []}
    # 以事件日为节点推进，市值 = 持仓 × 最近已知价（无价则用摊薄成本近似）
    qty_now = {aid: Decimal("0") for aid in asset_ids}
    cost_now = {aid: Decimal("0") for aid in asset_ids}
    avg_cost = {aid: Decimal("0") for aid in asset_ids}
    buy_qty = {aid: Decimal("0") for aid in asset_ids}
    buy_cost = {aid: Decimal("0") for aid in asset_ids}
    points = []
    for day, t in events:
        aid = t["asset_id"]
        qty = investments._q(t["quantity"]) if t["quantity"] else Decimal("0")
        amount = Decimal(int(t["amount_cents"] or 0))
        if t["trade_type"] == "买入":
            qty_now[aid] += qty
            buy_qty[aid] += qty
            buy_cost[aid] += amount
            avg_cost[aid] = buy_cost[aid] / buy_qty[aid] if buy_qty[aid] > 0 else Decimal("0")
            cost_now[aid] = avg_cost[aid] * qty_now[aid]
        else:
            qty_now[aid] = max(qty_now[aid] - qty, Decimal("0"))
            cost_now[aid] = avg_cost[aid] * qty_now[aid]
        market = Decimal("0")
        has_price = False
        for aid in asset_ids:
            mark = None
            for pd, pc in reversed(marks.get(aid, [])):
                if pd <= day:
                    mark = pc
                    break
            if mark is not None:
                market += Decimal(mark) * qty_now[aid]
                has_price = True
            else:
                market += cost_now[aid]
        points.append({
            "date": day,
            "market_cents": int(market.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            "cost_cents": int(sum(cost_now.values()).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            "has_price": has_price,
        })
    return {"ok": True, "points": points}


@contextlib.contextmanager
def invest_conn():
    """理财模块专用连接：自动提交/回滚并关闭。"""
    conn = investments.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    # Windows 下 Nagle + Delayed-ACK 相互作用会让每个请求固定多等 ~130ms
    # （连静态文件和 404 都慢），TCP_NODELAY 直接消除该延迟
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        size = int(self.headers.get("content-length", "0"))
        if not size:
            return {}
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def send_file(self, path, content_type=None):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        init_db()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_file(INDEX_PATH, "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            rel = urllib.parse.unquote(parsed.path.removeprefix("/static/"))
            target = (STATIC_PATH / rel).resolve()
            if STATIC_PATH.resolve() not in target.parents or not target.is_file():
                self.send_json({"error": "not found"}, 404)
                return
            self.send_file(target)
            return
        conn = connect()
        try:
            if parsed.path == "/api/summary":
                self.send_json(api_summary())
            elif parsed.path == "/api/entries":
                rows = conn.execute("select * from entries order by transaction_time desc, id desc limit 200").fetchall()
                self.send_json(rows_to_dicts(rows))
            elif parsed.path == "/api/entries-all":
                rows = conn.execute("select * from entries order by transaction_time desc, id desc").fetchall()
                self.send_json(rows_to_dicts(rows))
            elif parsed.path == "/api/accounts":
                self.send_json(rows_to_dicts(conn.execute("select * from accounts order by name").fetchall()))
            elif parsed.path == "/api/account-debts":
                self.send_json(list_account_debts(conn))
            elif parsed.path == "/api/account-names":
                self.send_json(account_names())
            elif parsed.path == "/api/account-filter-names":
                self.send_json(historical_account_names())
            elif parsed.path == "/api/unconfirmed":
                apply_all_keyword_rules_to_unconfirmed()
                self.send_json(rows_to_dicts(conn.execute(
                    """
                    select 'pending' source, id, transaction_time, type, category, status, amount_cents, currency, transaction_object,
                        account, target_account, participant, balance_delta_cents, actual_balance_cents, note,
                        refund_pair_id, bill_source, null raw_payload
                    from entries
                    where status = 'pending'
                    union all
                    select 'unconfirmed' source, id, transaction_time, type, category, status, amount_cents, currency, transaction_object,
                        account, target_account, participant, balance_delta_cents, actual_balance_cents, note,
                        refund_pair_id, bill_source, raw_payload
                    from unconfirmed_entries
                    order by transaction_time desc, id desc
                    """
                ).fetchall()))
            elif parsed.path == "/api/categories":
                self.send_json(flatten_categories())
            elif parsed.path == "/api/category-tree":
                self.send_json(category_tree())
            elif parsed.path == "/api/keyword-rules":
                self.send_json(list_keyword_rules())
            elif parsed.path == "/api/refund-candidates":
                query = urllib.parse.parse_qs(parsed.query)
                self.send_json(refund_candidates(
                    (query.get("merchant") or [None])[0],
                    (query.get("time") or [None])[0],
                    (query.get("amount") or [None])[0],
                    (query.get("account") or [None])[0],
                ))
            elif parsed.path == "/api/activities":
                self.send_json(list_activities())
            elif parsed.path == "/api/activity-entries":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    self.send_json(activity_entries((query.get("id") or [0])[0]))
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
            elif parsed.path == "/api/invest/overview":
                self.send_json(investments.overview(conn))
            elif parsed.path == "/api/invest/kline":
                query = urllib.parse.parse_qs(parsed.query)
                asset = conn.execute(
                    "select * from invest_assets where id = ?", (int(query.get("asset_id", [0])[0]),)
                ).fetchone()
                if asset is None:
                    self.send_json({"ok": False, "error": "asset not found"}, 404)
                else:
                    interval = (query.get("interval") or ["day"])[0]
                    self.send_json(invest_quotes.kline_for_asset(conn, dict(asset), interval))
                    conn.commit()
            elif parsed.path == "/api/invest/curve":
                self.send_json(invest_curve(conn, parsed.query))
            elif parsed.path == "/api/export.csv":
                rows = conn.execute("select * from entries order by transaction_time desc").fetchall()
                out = io.StringIO()
                writer = csv.writer(out)
                headers = rows[0].keys() if rows else ["id", "transaction_time", "type", "category", "amount"]
                writer.writerow(headers)
                for row in rows:
                    writer.writerow([row[h] for h in headers])
                data = out.getvalue().encode("utf-8-sig")
                self.send_response(200)
                self.send_header("content-type", "text/csv; charset=utf-8")
                self.send_header("content-disposition", "attachment; filename=bookkeeping.csv")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    def do_POST(self):
        init_db()
        parsed = urllib.parse.urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/add":
                self.send_json(insert_entry(data, auto_learn=bool(data.get("learn"))))
            elif parsed.path == "/api/entry-update":
                self.send_json(update_entry(int(data["id"]), data))
            elif parsed.path == "/api/entry-delete":
                self.send_json(delete_entry(int(data["id"])))
            elif parsed.path == "/api/confirm":
                self.send_json(confirm_entry(
                    int(data["id"]),
                    data["category"],
                    data.get("type"),
                    bool(data.get("learn")),
                    data.get("transaction_object"),
                    data.get("note"),
                    data.get("source"),
                    data.get("account"),
                    data.get("target_account"),
                    data.get("refund_pair_id"),
                ))
            elif parsed.path == "/api/refund-link":
                self.send_json(link_refund(int(data["refund_id"]), int(data["expense_id"])))
            elif parsed.path == "/api/refund-auto-link":
                conn = connect()
                self.send_json(auto_link_refunds(conn))
                conn.close()
            elif parsed.path == "/api/activity-add":
                self.send_json(create_activity(data.get("name"), data.get("start_date"), data.get("end_date"), data.get("note")))
            elif parsed.path == "/api/activity-update":
                self.send_json(update_activity(int(data["id"]), data))
            elif parsed.path == "/api/activity-delete":
                self.send_json(delete_activity(int(data["id"])))
            elif parsed.path == "/api/activity-archive":
                self.send_json(set_activity_archived(int(data["id"]), bool(data.get("archived", True))))
            elif parsed.path == "/api/entry-activity":
                self.send_json(set_entries_activity(data.get("ids"), data.get("activity_id")))
            elif parsed.path == "/api/refund-unlink":
                self.send_json(unlink_refund(int(data["refund_id"])))
            elif parsed.path == "/api/pending-update":
                self.send_json(update_pending_meta(
                    int(data["id"]),
                    data.get("source"),
                    data.get("transaction_object"),
                    data.get("note"),
                ))
            elif parsed.path == "/api/account-set":
                set_account(
                    data["name"], data.get("balance"), data.get("currency", "CNY"), data.get("note"), data.get("kind"),
                    expense_mode=data.get("expense_mode"), principal=data.get("principal"), asset_type=data.get("asset_type"),
                )
                self.send_json({"ok": True})
            elif parsed.path == "/api/account-add":
                add_account_name(data["name"])
                self.send_json({"ok": True})
            elif parsed.path == "/api/account-archive":
                self.send_json(set_account_archived(data["name"], bool(data.get("archived", True))))
            elif parsed.path == "/api/account-rename":
                self.send_json(rename_account_name(data["old_name"], data["new_name"]))
            elif parsed.path == "/api/account-delete":
                self.send_json(delete_account_name(data["name"]))
            elif parsed.path == "/api/account-debt-delete":
                self.send_json(hide_account_debt(int(data["id"])))
            elif parsed.path == "/api/category-add":
                add_category(data["type"], data["category"])
                self.send_json({"ok": True})
            elif parsed.path == "/api/category-rename":
                self.send_json(rename_category(data["type"], data["old_category"], data["new_category"]))
            elif parsed.path == "/api/category-delete":
                self.send_json(delete_category(data["type"], data["category"]))
            elif parsed.path == "/api/keyword-rule-add":
                self.send_json(add_keyword_rule_and_apply(data["keyword"], data["category"], data.get("type") or None))
            elif parsed.path == "/api/keyword-rule-delete":
                self.send_json(delete_keyword_rule(int(data["id"])))
            elif parsed.path == "/api/correct-balance":
                self.send_json(correct_balance(data["account"], data["actual"], data.get("note")))
            elif parsed.path == "/api/invest/category-add":
                with invest_conn() as ic:
                    self.send_json(investments.add_category(ic, data.get("name")))
            elif parsed.path == "/api/invest/category-delete":
                with invest_conn() as ic:
                    self.send_json(investments.delete_category(ic, int(data["id"])))
            elif parsed.path == "/api/invest/account-add":
                with invest_conn() as ic:
                    self.send_json(investments.add_account(ic, int(data["category_id"]), data.get("name"), data.get("note")))
            elif parsed.path == "/api/invest/account-delete":
                with invest_conn() as ic:
                    self.send_json(investments.delete_account(ic, int(data["id"])))
            elif parsed.path == "/api/invest/asset-add":
                with invest_conn() as ic:
                    self.send_json(investments.add_asset(
                        ic, int(data["account_id"]), data.get("name"), data.get("code"),
                        data.get("quantity") or "0", data.get("price_cents"), data.get("note"),
                        data.get("quote_currency") or "CNY", data.get("fx_cny_micros") or 1000000))
            elif parsed.path == "/api/invest/asset-delete":
                with invest_conn() as ic:
                    self.send_json(investments.delete_asset(ic, int(data["id"])))
            elif parsed.path == "/api/invest/asset-price":
                with invest_conn() as ic:
                    self.send_json(investments.update_asset_price(
                        ic, int(data["id"]), data.get("price_cents"),
                        quote_currency=data.get("quote_currency"), fx_cny_micros=data.get("fx_cny_micros")))
            elif parsed.path == "/api/invest/trade-add":
                with invest_conn() as ic:
                    self.send_json(investments.add_trade(ic, data))
            elif parsed.path == "/api/invest/trade-delete":
                with invest_conn() as ic:
                    self.send_json(investments.delete_trade(ic, int(data["id"])))
            elif parsed.path == "/api/invest/asset-settle":
                with invest_conn() as ic:
                    self.send_json(investments.settle_asset(ic, int(data["id"])))
            elif parsed.path == "/api/invest/account-settle":
                with invest_conn() as ic:
                    self.send_json(investments.settle_account(ic, int(data["id"])))
            elif parsed.path == "/api/invest/refresh-prices":
                with invest_conn() as ic:
                    self.send_json(investments.refresh_prices(ic))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)


def print_table(rows, fields):
    if not rows:
        print("(none)")
        return
    widths = {f: max(len(f), *(len(str(row.get(f, ""))) for row in rows)) for f in fields}
    print("  ".join(f.ljust(widths[f]) for f in fields))
    print("  ".join("-" * widths[f] for f in fields))
    for row in rows:
        print("  ".join(str(row.get(f, "")).ljust(widths[f]) for f in fields))


def command_list(args):
    init_db()
    conn = connect()
    where = []
    params = []
    if args.month:
        where.append("substr(transaction_time,1,7)=?")
        params.append(args.month)
    if args.type:
        where.append("type=?")
        params.append(args.type)
    if args.category:
        where.append("category like ?")
        params.append(f"%{args.category}%")
    sql = "select id, transaction_time, type, category, status, amount_cents, account, target_account, transaction_object, note from entries"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by transaction_time desc, id desc limit ?"
    params.append(args.limit)
    rows = []
    for row in conn.execute(sql, params).fetchall():
        d = dict(row)
        d["amount"] = money(d.pop("amount_cents"))
        rows.append(d)
    print_table(rows, ["id", "transaction_time", "type", "category", "status", "amount", "account", "target_account", "transaction_object", "note"])
    conn.close()


def command_summary(args):
    init_db()
    month = args.month or dt.datetime.now().strftime("%Y-%m")
    conn = connect()
    rows = conn.execute(
        """
        select type, coalesce(category,'未分类') category, sum(amount_cents) amount_cents
        from entries
        where substr(transaction_time,1,7)=? and type not in ('转账','余额修正') and coalesce(category,'') != '转账'
        group by type, category
        order by type, amount_cents desc
        """,
        (month,),
    ).fetchall()
    print(f"Month: {month}")
    for row in rows:
        print(f"{row['type']}\t{row['category']}\t{money(row['amount_cents'])}")
    income_total = sum(r["amount_cents"] for r in rows if r["type"] in debt_accounts.STAT_INCOME_TYPES)
    expense_gross = sum(r["amount_cents"] for r in rows if r["type"] in debt_accounts.STAT_EXPENSE_TYPES)
    refund_total = sum(r["amount_cents"] for r in rows if r["type"] == "退款")
    print("--- 净额汇总（退款冲减支出，不计收入）---")
    print(f"收入\t{money(income_total)}")
    print(f"支出(毛)\t{money(expense_gross)}")
    print(f"退款冲减\t-{money(refund_total)}")
    print(f"支出(净)\t{money(expense_gross - refund_total)}")
    print(f"结余\t{money(income_total - expense_gross + refund_total)}")
    conn.close()


def command_accounts(_args):
    init_db()
    conn = connect()
    rows = []
    for row in conn.execute("select name, initial_balance_cents, current_balance_cents, currency, updated_at from accounts order by name"):
        d = dict(row)
        d["initial_balance"] = money(d.pop("initial_balance_cents"))
        d["current_balance"] = money(d.pop("current_balance_cents"))
        rows.append(d)
    print_table(rows, ["name", "initial_balance", "current_balance", "currency", "updated_at"])
    conn.close()


def command_unconfirmed(_args):
    init_db()
    conn = connect()
    rows = []
    for row in conn.execute(
        """
        select 'pending' source, id, transaction_time, type, category, status, amount_cents, account, target_account, transaction_object, note
        from entries
        where status = 'pending'
        union all
        select 'unconfirmed' source, id, transaction_time, type, category, status, amount_cents, account, target_account, transaction_object, note
        from unconfirmed_entries
        order by transaction_time desc, id desc
        """
    ):
        d = dict(row)
        d["amount"] = money(d.pop("amount_cents"))
        rows.append(d)
    print_table(rows, ["source", "id", "transaction_time", "type", "category", "status", "amount", "account", "target_account", "transaction_object", "note"])
    conn.close()


def command_unconfirmed_summary(args):
    print(json.dumps(unconfirmed_summary(args.limit), ensure_ascii=False, indent=2))


def command_export(args):
    init_db()
    conn = connect()
    rows = conn.execute("select * from entries order by transaction_time desc").fetchall()
    target = open(args.output, "w", newline="", encoding="utf-8-sig") if args.output else sys.stdout
    with target:
        writer = csv.writer(target)
        if rows:
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow([row[k] for k in row.keys()])
    conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Local-first SQLite bookkeeping skill")
    parser.add_argument("--db", dest="db_path", help="SQLite database path. Defaults to BOOKKEEPING_DB_PATH or ~/.local/share/bookkeeping/bookkeeping.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-db")
    p.add_argument("--db-path", dest="init_db_path", help="Initialize and use this SQLite database path")

    sub.add_parser("db-path")

    p = sub.add_parser("add")
    p.add_argument("--type", required=True, choices=sorted(ENTRY_TYPES))
    p.add_argument("--amount", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--target-account", dest="target_account")
    p.add_argument("--category")
    p.add_argument("--transaction-object", dest="transaction_object")
    p.add_argument("--merchant", dest="transaction_object")
    p.add_argument("--counterparty", dest="transaction_object")
    p.add_argument("--participant", default="自己")
    p.add_argument("--note")
    p.add_argument("--currency", default="CNY")
    p.add_argument("--time", dest="transaction_time")
    p.add_argument("--learn", action="store_true")

    p = sub.add_parser("transfer")
    p.add_argument("--from-account", required=True)
    p.add_argument("--to-account", required=True)
    p.add_argument("--amount", required=True)
    p.add_argument("--note")
    p.add_argument("--time", dest="transaction_time")

    p = sub.add_parser("account-set")
    p.add_argument("name")
    p.add_argument("--balance")
    p.add_argument("--currency", default="CNY")
    p.add_argument("--note")
    p.add_argument("--kind", choices=["asset", "liability", "receivable"])
    p.add_argument("--expense-mode", dest="expense_mode", choices=["consume", "repay"])
    p.add_argument("--principal")
    p.add_argument("--asset-type", dest="asset_type", choices=["cash", "bank", "virtual", "custom"])

    p = sub.add_parser("account-add", help="Create an account by name")
    p.add_argument("--name", required=True)

    p = sub.add_parser("account-rename", help="Rename an account")
    p.add_argument("--old", dest="old_name", required=True)
    p.add_argument("--new", dest="new_name", required=True)

    p = sub.add_parser("account-archive", help="Archive or restore a liability/receivable account")
    p.add_argument("name")
    p.add_argument("--restore", action="store_true")

    p = sub.add_parser("account-delete", help="Delete an unused account")
    p.add_argument("name")

    p = sub.add_parser("entry-update", help="Update a confirmed entry")
    p.add_argument("id", type=int)
    p.add_argument("--type", choices=sorted(ENTRY_TYPES))
    p.add_argument("--amount")
    p.add_argument("--account")
    p.add_argument("--target-account", dest="target_account")
    p.add_argument("--category")
    p.add_argument("--transaction-object", dest="transaction_object")
    p.add_argument("--note")
    p.add_argument("--time", dest="transaction_time")
    p.add_argument("--bill-source", dest="bill_source")
    p.add_argument("--activity-id", dest="activity_id")

    p = sub.add_parser("entry-delete", help="Delete a confirmed entry")
    p.add_argument("id", type=int)

    p = sub.add_parser("refund-link", help="Link a refund to its original expense")
    p.add_argument("--refund-id", dest="refund_id", type=int, required=True)
    p.add_argument("--expense-id", dest="expense_id", type=int, required=True)

    p = sub.add_parser("refund-unlink", help="Unlink a refund from its expense")
    p.add_argument("--refund-id", dest="refund_id", type=int, required=True)

    p = sub.add_parser("refund-candidates", help="List expense candidates for a refund")
    p.add_argument("--merchant")
    p.add_argument("--time", dest="transaction_time")
    p.add_argument("--amount")
    p.add_argument("--account")

    p = sub.add_parser("activity-add", help="Create an activity")
    p.add_argument("--name", required=True)
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument("--note")

    p = sub.add_parser("activity-update", help="Rename or edit an activity")
    p.add_argument("--id", dest="activity_id", type=int, required=True)
    p.add_argument("--name")
    p.add_argument("--start-date", dest="start_date")
    p.add_argument("--end-date", dest="end_date")
    p.add_argument("--note")

    p = sub.add_parser("activity-delete", help="Delete an activity (entries stay)")
    p.add_argument("--id", dest="activity_id", type=int, required=True)

    p = sub.add_parser("activity-list", help="List activities with net spend")

    p = sub.add_parser("activity-archive", help="Archive or restore an activity")
    p.add_argument("--id", dest="activity_id", type=int, required=True)
    p.add_argument("--restore", action="store_true")

    p = sub.add_parser("activity-entries", help="List entries in an activity")
    p.add_argument("--id", dest="activity_id", type=int, required=True)

    p = sub.add_parser("entry-activity", help="Attach or detach entries to an activity")
    p.add_argument("--entry-id", dest="entry_ids", action="append", required=True)
    p.add_argument("--activity-id", dest="activity_id", type=int)

    p = sub.add_parser("correct-balance")
    p.add_argument("account")
    p.add_argument("--actual", required=True)
    p.add_argument("--note")

    p = sub.add_parser("import")
    p.add_argument("path")
    p.add_argument("--account")
    p.add_argument("--duplicate-account-map", help="JSON object/file mapping imported account names to existing account names for cross-source duplicate removal")

    p = sub.add_parser("import-check")
    p.add_argument("path")
    p.add_argument("--account")
    p.add_argument("--output", help="Write standard CSV analysis JSON")

    p = sub.add_parser("keyword-candidates")
    p.add_argument("path")
    p.add_argument("--account")
    p.add_argument("--output", help="Write keyword candidate analysis JSON")

    p = sub.add_parser("apply-keyword-rules")
    p.add_argument("path")
    p.add_argument("--keyword-rules", required=True, help="JSON array/file of {keyword, category, type} rules")
    p.add_argument("--output", required=True)

    p = sub.add_parser("import-duplicates")
    p.add_argument("path")
    p.add_argument("--account")
    p.add_argument("--output", help="Write duplicate candidate analysis JSON for review")

    sub.add_parser("refund-auto-link", help="Auto-link refunds to expenses at >=90% confidence")

    p = sub.add_parser("confirm")
    p.add_argument("id", type=int)
    p.add_argument("--category", required=True)
    p.add_argument("--type", choices=sorted(ENTRY_TYPES))
    p.add_argument("--note")
    p.add_argument("--learn", action="store_true")

    p = sub.add_parser("rule-add")
    p.add_argument("merchant")
    p.add_argument("--category", required=True)
    p.add_argument("--type", choices=sorted(ENTRY_TYPES))

    sub.add_parser("rule-list")

    p = sub.add_parser("rule-delete")
    p.add_argument("id", type=int)

    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--month")
    p.add_argument("--type")
    p.add_argument("--category")

    p = sub.add_parser("summary")
    p.add_argument("--month")

    sub.add_parser("accounts")
    sub.add_parser("unconfirmed")

    p = sub.add_parser("unconfirmed-summary")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("export")
    p.add_argument("--output")

    p = sub.add_parser("dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    set_database_path(getattr(args, "init_db_path", None) or getattr(args, "db_path", None))
    if args.cmd == "init-db":
        init_db()
        print(f"initialized {DB_PATH}")
    elif args.cmd == "db-path":
        print(DB_PATH)
    elif args.cmd == "add":
        result = insert_entry(vars(args), auto_learn=args.learn)
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "transfer":
        transfer(args.from_account, args.to_account, args.amount, args.note, args.transaction_time)
        print("ok")
    elif args.cmd == "account-set":
        set_account(
            args.name, args.balance, args.currency, args.note,
            kind=getattr(args, "kind", None),
            expense_mode=getattr(args, "expense_mode", None),
            principal=getattr(args, "principal", None),
            asset_type=getattr(args, "asset_type", None),
        )
        print(json.dumps({"ok": True, "name": args.name}, ensure_ascii=False))
    elif args.cmd == "account-add":
        add_account_name(args.name)
        print(json.dumps({"ok": True, "name": args.name}, ensure_ascii=False))
    elif args.cmd == "account-rename":
        print(json.dumps(rename_account_name(args.old_name, args.new_name), ensure_ascii=False))
    elif args.cmd == "account-archive":
        print(json.dumps(set_account_archived(args.name, not args.restore), ensure_ascii=False))
    elif args.cmd == "account-delete":
        print(json.dumps(delete_account_name(args.name), ensure_ascii=False))
    elif args.cmd == "entry-update":
        data = {}
        for key in ("type", "amount", "account", "target_account", "category", "transaction_object", "note", "transaction_time", "bill_source", "activity_id"):
            value = getattr(args, key, None)
            if value is not None:
                data[key] = value
        print(json.dumps(update_entry(args.id, data), ensure_ascii=False))
    elif args.cmd == "entry-delete":
        print(json.dumps(delete_entry(args.id), ensure_ascii=False))
    elif args.cmd == "refund-link":
        print(json.dumps(link_refund(args.refund_id, args.expense_id), ensure_ascii=False))
    elif args.cmd == "refund-unlink":
        print(json.dumps(unlink_refund(args.refund_id), ensure_ascii=False))
    elif args.cmd == "refund-candidates":
        print(json.dumps(refund_candidates(args.merchant, args.transaction_time, args.amount, args.account), ensure_ascii=False))
    elif args.cmd == "activity-add":
        print(json.dumps(create_activity(args.name, args.start_date, args.end_date, args.note), ensure_ascii=False))
    elif args.cmd == "activity-update":
        data = {k: getattr(args, k) for k in ("name", "start_date", "end_date", "note") if getattr(args, k, None) is not None}
        print(json.dumps(update_activity(args.activity_id, data), ensure_ascii=False))
    elif args.cmd == "activity-delete":
        print(json.dumps(delete_activity(args.activity_id), ensure_ascii=False))
    elif args.cmd == "activity-list":
        print(json.dumps(list_activities(), ensure_ascii=False))
    elif args.cmd == "activity-archive":
        print(json.dumps(set_activity_archived(args.activity_id, not args.restore), ensure_ascii=False))
    elif args.cmd == "activity-entries":
        print(json.dumps(activity_entries(args.activity_id), ensure_ascii=False))
    elif args.cmd == "entry-activity":
        print(json.dumps(set_entries_activity(args.entry_ids, args.activity_id), ensure_ascii=False))
    elif args.cmd == "correct-balance":
        print(json.dumps(correct_balance(args.account, args.actual, args.note), ensure_ascii=False))
    elif args.cmd == "import":
        result = import_file_with_options(args.path, args.account, args.duplicate_account_map)
        for notice in result.get("notices", []):
            print(notice)
        # 导入后自动关联退款：高置信（≥90%）自动连，其余留人工
        conn = connect()
        result["refund_auto_link"] = auto_link_refunds(conn)
        conn.close()
        auto = result["refund_auto_link"].get("auto", [])
        if auto:
            result["notices"].append(f"自动关联 {len(auto)} 笔退款（≥90%置信），详见 refund_auto_link")
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "refund-auto-link":
        conn = connect()
        result = auto_link_refunds(conn)
        conn.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "import-check":
        result = standard_csv_analysis(args.path, args.account)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"标准 CSV 检查: {output}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "keyword-candidates":
        result = keyword_candidate_analysis(args.path, args.account)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"关键词候选分析: {output}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "apply-keyword-rules":
        result = apply_keyword_rules_to_standard_csv(args.path, args.output, args.keyword_rules)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "import-duplicates":
        result = import_duplicate_analysis(args.path, args.account)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"重复候选分析: {output}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "confirm":
        print(json.dumps(confirm_entry(args.id, args.category, args.type, args.learn, note=args.note), ensure_ascii=False))
    elif args.cmd == "rule-add":
        result = add_keyword_rule_and_apply(args.merchant, args.category, args.type)
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "rule-list":
        print_table(list_keyword_rules(), ["id", "type", "category", "keyword"])
    elif args.cmd == "rule-delete":
        print(json.dumps(delete_keyword_rule(args.id), ensure_ascii=False))
    elif args.cmd == "list":
        command_list(args)
    elif args.cmd == "summary":
        command_summary(args)
    elif args.cmd == "accounts":
        command_accounts(args)
    elif args.cmd == "unconfirmed":
        command_unconfirmed(args)
    elif args.cmd == "unconfirmed-summary":
        command_unconfirmed_summary(args)
    elif args.cmd == "export":
        command_export(args)
    elif args.cmd == "dashboard":
        init_db()
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"Dashboard: http://{args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
