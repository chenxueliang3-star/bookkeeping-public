"""投资理财模块（净额制记账）。

三层结构：大类 → 账户(币安/券商/基金平台) → 持仓标的(BTC/ETH/个股/基金)。

记账口径（与用户确认的净额制一致）：
- 买入 = 转账（现金账户 → 理财·XX 账户），不产生支出；
- 卖出 = 转账（理财·XX 账户 → 现金账户），不产生收入；
- 分红/利息 = 真金白银，记 收入/投资收益（或 利息收入），计入收支统计；
- 手续费 = 记 支出/其他·手续费，计入收支统计；
- 已实现盈亏 / 浮盈 只做展示，不进收支统计（避免虚增）。

本模块供 bookkeeping.py 在 init_db 时调用（幂等迁移），并提供 /api/invest-* 处理器。
"""

import datetime as _dt
import json
import os
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

KIND_INVEST = "invest"
SUPPORTED_QUOTE_CURRENCIES = ("CNY", "USD")

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "bookkeeping"
DB_PATH = Path(os.environ.get("BOOKKEEPING_DB_PATH", DEFAULT_DATA_DIR / "bookkeeping.db")).expanduser()

TRADE_TYPES = ("买入", "卖出", "分红", "利息", "手续费")
CASH_TYPES = {"买入", "卖出", "分红", "利息", "手续费"}

DEFAULT_CATEGORIES = ("数字货币", "股票", "基金")


def set_db_path(value):
    global DB_PATH
    DB_PATH = Path(value).expanduser()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def _now_iso():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _q(value):
    return Decimal(str(value or "0")).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def money(cents):
    return round((cents or 0) / 100, 2)


# ---------------------------------------------------------------- schema

def ensure_schema(conn):
    conn.execute(
        """
        create table if not exists invest_categories(
            id integer primary key autoincrement,
            name text not null unique,
            created_at text not null
        )"""
    )
    conn.execute(
        """
        create table if not exists invest_accounts(
            id integer primary key autoincrement,
            category_id integer not null references invest_categories(id),
            name text not null,
            note text default '',
            created_at text not null,
            unique(category_id, name)
        )"""
    )
    conn.execute(
        """
        create table if not exists invest_assets(
            id integer primary key autoincrement,
            account_id integer not null references invest_accounts(id),
            name text not null,
            code text default '',
            quantity text not null default '0',
            current_price_cents integer,
            quote_currency text not null default 'CNY',
            current_fx_cny_micros integer not null default 1000000,
            note text default '',
            created_at text not null
        )"""
    )
    conn.execute(
        """
        create table if not exists invest_trades(
            id integer primary key autoincrement,
            asset_id integer not null references invest_assets(id),
            trade_type text not null,
            quantity text,
            price_cents integer,
            price_currency text not null default 'CNY',
            fx_cny_micros integer not null default 1000000,
            amount_cents integer not null,
            fee_cents integer not null default 0,
            cost_basis_cents integer,
            realized_cents integer,
            trade_time text not null,
            cash_account text default '',
            entry_id integer,
            note text default '',
            created_at text not null
        )"""
    )
    conn.execute(
        """
        create table if not exists invest_price_history(
            id integer primary key autoincrement,
            asset_id integer not null references invest_assets(id),
            price_date text not null,
            open_cents integer, high_cents integer, low_cents integer, close_cents integer,
            source text default 'manual',
            created_at text not null,
            unique(asset_id, price_date, source)
        )"""
    )
    cols = [row[1] for row in conn.execute("pragma table_info(accounts)")]
    if "kind" not in cols:
        conn.execute("alter table accounts add column kind text not null default 'asset'")
    # 结盈余基线：标的已结浮盈 / 账户已结已实现盈亏（防止重复计入收支）
    asset_cols = [row[1] for row in conn.execute("pragma table_info(invest_assets)")]
    if "settled_surplus_cents" not in asset_cols:
        conn.execute("alter table invest_assets add column settled_surplus_cents integer not null default 0")
    account_cols = [row[1] for row in conn.execute("pragma table_info(invest_accounts)")]
    if "settled_realized_cents" not in account_cols:
        conn.execute("alter table invest_accounts add column settled_realized_cents integer not null default 0")
    if "quote_currency" not in asset_cols:
        conn.execute("alter table invest_assets add column quote_currency text not null default 'CNY'")
    if "current_fx_cny_micros" not in asset_cols:
        conn.execute("alter table invest_assets add column current_fx_cny_micros integer not null default 1000000")
    trade_cols = [row[1] for row in conn.execute("pragma table_info(invest_trades)")]
    if "price_currency" not in trade_cols:
        conn.execute("alter table invest_trades add column price_currency text not null default 'CNY'")
    if "fx_cny_micros" not in trade_cols:
        conn.execute("alter table invest_trades add column fx_cny_micros integer not null default 1000000")
    migrate_legacy_crypto_currency(conn)


_MIGRATE_DONE = False


def migrate_legacy_crypto_currency(conn):
    """Convert legacy crypto prices that were entered as USD into CNY cash values."""
    # 本迁移是一次性的：没有待迁移数据就永久跳过，绝不能在每次 init_db 时
    # 都同步发起汇率 HTTP 请求（曾导致所有写操作白等 ~1.2s，像卡死）。
    global _MIGRATE_DONE
    if _MIGRATE_DONE:
        return
    crypto_codes = ("BTC", "ETH", "USDT", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LINK", "DOT")
    placeholders = ",".join("?" for _ in crypto_codes)
    assets = conn.execute(
        f"select * from invest_assets where upper(code) in ({placeholders}) and quote_currency = 'CNY'",
        crypto_codes,
    ).fetchall()
    if not assets:
        _MIGRATE_DONE = True
        return
    try:
        from quotes import _fetch_usd_cny_micros
        usd_cny_micros = _fetch_usd_cny_micros()
    except Exception:
        # Do not guess an exchange rate. The migration will retry next startup.
        return
    for asset in assets:
        conn.execute(
            "update invest_assets set quote_currency = 'USD', current_fx_cny_micros = ? where id = ?",
            (usd_cny_micros, asset["id"]),
        )
        for trade in conn.execute("select * from invest_trades where asset_id = ?", (asset["id"],)).fetchall():
            old_amount = int(trade["amount_cents"] or 0)
            new_amount = int((Decimal(old_amount) * Decimal(usd_cny_micros) / Decimal(1000000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if new_amount != old_amount:
                entry = conn.execute("select * from entries where id = ?", (trade["entry_id"],)).fetchone() if trade["entry_id"] else None
                if entry is not None:
                    delta = new_amount - int(entry["amount_cents"] or 0)
                    if entry["type"] == "转账":
                        _cash_apply(conn, entry["account"], -delta)
                        _cash_apply(conn, entry["target_account"], delta)
                    elif entry["type"] == "收入":
                        _cash_apply(conn, entry["account"], delta)
                    elif entry["type"] == "支出":
                        _cash_apply(conn, entry["account"], -delta)
                    conn.execute("update entries set amount_cents = ? where id = ?", (new_amount, entry["id"]))
                conn.execute("update invest_trades set amount_cents = ? where id = ?", (new_amount, trade["id"]))
            conn.execute(
                "update invest_trades set price_currency = 'USD', fx_cny_micros = ? where id = ?",
                (usd_cny_micros, trade["id"]),
            )


# ---------------------------------------------------------------- cash flow

def _ensure_invest_cash_account(conn, invest_account_name):
    row = conn.execute("select kind from accounts where name = ?", (invest_account_name,)).fetchone()
    ts = _now_iso()
    if row is None:
        conn.execute(
            "insert into accounts(name, initial_balance_cents, current_balance_cents, currency, kind, created_at, updated_at)"
            " values (?, 0, 0, 'CNY', ?, ?, ?)",
            (invest_account_name, KIND_INVEST, ts, ts),
        )
    elif (row["kind"] or "asset") != KIND_INVEST:
        conn.execute("update accounts set kind = ?, updated_at = ? where name = ?", (KIND_INVEST, ts, invest_account_name))


def invest_account_name(invest_account_row_name):
    return f"理财·{invest_account_row_name}"


def _cash_apply(conn, account, cents_delta):
    if not account:
        return
    row = conn.execute("select 1 from accounts where name = ?", (account,)).fetchone()
    if row is None:
        ts = _now_iso()
        conn.execute(
            "insert into accounts(name, initial_balance_cents, current_balance_cents, currency, kind, created_at, updated_at)"
            " values (?, 0, 0, 'CNY', 'asset', ?, ?)",
            (account, ts, ts),
        )
    conn.execute(
        "update accounts set current_balance_cents = current_balance_cents + ?, updated_at = ? where name = ?",
        (cents_delta, _now_iso(), account),
    )


def _write_cash_flow(conn, trade, asset, inv_acc_row_name):
    """按交易类型生成账本分录并更新现金/理财账户余额，返回 entry_id。"""
    invest_full_name = invest_account_name(inv_acc_row_name)
    _ensure_invest_cash_account(conn, invest_full_name)
    ttype = trade["trade_type"]
    amount = int(trade["amount_cents"])
    ts = _now_iso()
    ttime = trade["trade_time"] or ts
    cash = trade["cash_account"] or ""
    obj = f"理财·{inv_acc_row_name} {asset['name']}"
    note = trade["note"] or ""

    if ttype == "买入":
        conn.execute(
            "insert into entries(transaction_time, type, category, amount_cents, currency, account, target_account,"
            " participant, note) values (?, '转账', '转账', ?, 'CNY', ?, ?, '自己', ?)",
            (ttime, amount, cash, invest_full_name, note or "理财买入"),
        )
        entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        _cash_apply(conn, cash, -amount)
        _cash_apply(conn, invest_full_name, amount)
    elif ttype == "卖出":
        conn.execute(
            "insert into entries(transaction_time, type, category, amount_cents, currency, account, target_account,"
            " participant, note) values (?, '转账', '转账', ?, 'CNY', ?, ?, '自己', ?)",
            (ttime, amount, invest_full_name, cash, note or "理财卖出回款"),
        )
        entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        _cash_apply(conn, invest_full_name, -amount)
        _cash_apply(conn, cash, amount)
    elif ttype in ("分红", "利息"):
        category = "利息收入" if ttype == "利息" else "投资收益"
        conn.execute(
            "insert into entries(transaction_time, type, category, amount_cents, currency, account,"
            " participant, note) values (?, '收入', ?, ?, 'CNY', ?, '自己', ?)",
            (ttime, category, amount, cash, note or f"理财{ttype}"),
        )
        entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        _cash_apply(conn, cash, amount)
    elif ttype == "手续费":
        conn.execute(
            "insert into entries(transaction_time, type, category, amount_cents, currency, account,"
            " participant, note) values (?, '支出', '其他/手续费', ?, 'CNY', ?, '自己', ?)",
            (ttime, amount, cash, note or "理财手续费"),
        )
        entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
        _cash_apply(conn, cash, -amount)
    else:
        raise ValueError(f"unknown trade type: {ttype}")
    return entry_id


def _revert_cash_flow(conn, trade, inv_acc_row_name):
    """删除交易时回滚账本分录与账户余额。"""
    invest_full_name = invest_account_name(inv_acc_row_name)
    entry_id = trade["entry_id"]
    if entry_id:
        row = conn.execute("select * from entries where id = ?", (entry_id,)).fetchone()
        if row is not None:
            if row["type"] == "转账":
                _cash_apply(conn, row["account"], row["amount_cents"])
                _cash_apply(conn, row["target_account"], -row["amount_cents"])
            elif row["type"] == "收入":
                _cash_apply(conn, row["account"], -row["amount_cents"])
            elif row["type"] == "支出":
                _cash_apply(conn, row["account"], row["amount_cents"])
            conn.execute("delete from entries where id = ?", (entry_id,))
    else:
        # 无分录（理论上不出现），仅按规则回滚余额
        ttype = trade["trade_type"]
        amount = int(trade["amount_cents"] or 0)
        cash = trade["cash_account"] or ""
        if ttype == "买入":
            _cash_apply(conn, cash, amount)
            _cash_apply(conn, invest_full_name, -amount)
        elif ttype == "卖出":
            _cash_apply(conn, cash, -amount)
            _cash_apply(conn, invest_full_name, amount)
        elif ttype == "结盈余":
            _cash_apply(conn, invest_full_name, -amount)
        elif ttype in ("分红", "利息"):
            _cash_apply(conn, cash, -amount)
        elif ttype == "手续费":
            _cash_apply(conn, cash, amount)


# ---------------------------------------------------------------- positions

def asset_positions(conn, asset_rows):
    """按标的聚合持仓：数量、摊薄成本、市值、浮盈、已实现盈亏。"""
    result = {}
    for asset in asset_rows:
        result[asset["id"]] = {
            "quantity": Decimal("0"),
            "cost_total": Decimal("0"),
            "realized_cents": 0,
            "has_cost_gap": False,
        }
    for trade in conn.execute(
        f"select * from invest_trades where trade_type in ('买入','卖出') order by trade_time, id"
    ):
        pos = result.get(trade["asset_id"])
        if pos is None:
            continue
        qty = _q(trade["quantity"])
        amount = Decimal(int(trade["amount_cents"] or 0))
        if trade["trade_type"] == "买入":
            pos["quantity"] += qty
            pos["cost_total"] += amount
        else:
            if qty > 0 and pos["quantity"] > 0:
                avg = pos["cost_total"] / pos["quantity"]
                basis = avg * qty
                realized = amount - basis
                pos["realized_cents"] += int(realized.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                pos["quantity"] -= qty
                pos["cost_total"] -= basis
                if trade["cost_basis_cents"] is None:
                    conn.execute(
                        "update invest_trades set cost_basis_cents = ?, realized_cents = ? where id = ?",
                        (int(basis.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                         int(realized.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), trade["id"]),
                    )
            else:
                pos["has_cost_gap"] = True
    return result


def _unpositioned_invest_balance_cents(conn):
    """未在理财模块建仓的理财账户（accounts.kind='invest'）余额合计。

    已建仓账户的市值已在 overview 持仓里计算，余额不再重复计入，
    避免"账户余额 + 持仓市值"双重统计。适用于 C2C 买U等只做转账、
    尚未在理财页记录持仓的场景。
    """
    with_assets = {row["name"] for row in conn.execute(
        "select distinct a.name from invest_accounts a join invest_assets s on s.account_id = a.id"
    ).fetchall()}
    total = 0
    for row in conn.execute("select name, current_balance_cents from accounts where kind = 'invest'"):
        if row["name"] not in with_assets:
            total += int(row["current_balance_cents"] or 0)
    return total


def overview(conn):
    categories = [dict(r) for r in conn.execute("select * from invest_categories order by id")]
    accounts = [dict(r) for r in conn.execute("select * from invest_accounts order by id")]
    assets = [dict(r) for r in conn.execute("select * from invest_assets order by id")]
    trades_by_asset = {}
    for trade in conn.execute("select * from invest_trades order by trade_time desc, id desc"):
        trades_by_asset.setdefault(trade["asset_id"], []).append(dict(trade))
    positions = asset_positions(conn, assets)

    total_market = 0
    total_cost = 0
    total_realized = 0
    month = _dt.datetime.now().strftime("%Y-%m")
    month_realized = 0
    cat_out = []
    for cat in categories:
        cat_accounts = [a for a in accounts if a["category_id"] == cat["id"]]
        acc_out = []
        cat_market = 0
        cat_cost = 0
        cat_realized = 0
        for acc in cat_accounts:
            acc_assets = [a for a in assets if a["account_id"] == acc["id"]]
            asset_out = []
            for asset in acc_assets:
                pos = positions.get(asset["id"], {"quantity": Decimal("0"), "cost_total": Decimal("0"), "realized_cents": 0})
                qty = pos["quantity"]
                cost_cents = int(pos["cost_total"])
                price = asset["current_price_cents"]
                quote_currency = asset["quote_currency"] or "CNY"
                fx_cny_micros = int(asset["current_fx_cny_micros"] or 1000000)
                market = int((qty * Decimal(price) * Decimal(fx_cny_micros) / Decimal(1000000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if (price is not None and qty != 0) else None
                unrealized = (market - cost_cents) if (market is not None and (qty != 0 or cost_cents != 0)) else None
                settled_cents = int(asset["settled_surplus_cents"] or 0)
                if qty != 0 and price is not None:
                    unsettled = market - cost_cents - settled_cents
                else:
                    unsettled = -settled_cents if settled_cents else 0
                asset_out.append({
                    "id": asset["id"],
                    "account_id": acc["id"],
                    "name": asset["name"],
                    "code": asset["code"] or "",
                    "quantity": float(qty) if qty else 0.0,
                    "cost_cents": cost_cents,
                    "avg_cost_cents": int((pos["cost_total"] / qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if qty > 0 else None,
                    "price_cents": price,
                    "price_currency": quote_currency,
                    "price_cny_cents": int((Decimal(price) * Decimal(fx_cny_micros) / Decimal(1000000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if price is not None else None,
                    "fx_cny_micros": fx_cny_micros,
                    "market_cents": market,
                    "unrealized_cents": unrealized,
                    "settled_surplus_cents": settled_cents,
                    "unsettled_cents": unsettled,
                    "realized_cents": pos["realized_cents"],
                    "has_cost_gap": pos["has_cost_gap"],
                    "note": asset["note"] or "",
                    "trades": trades_by_asset.get(asset["id"], []),
                })
                if market is not None:
                    total_market += market
                    cat_market += market
                total_cost += cost_cents
                cat_cost += cost_cents
                total_realized += pos["realized_cents"]
                cat_realized += pos["realized_cents"]
                for trade in trades_by_asset.get(asset["id"], []):
                    if trade["trade_time"][:7] == month and trade["realized_cents"]:
                        month_realized += trade["realized_cents"]
            acc_settled_realized = int(acc["settled_realized_cents"] or 0)
            acc_realized = sum(a["realized_cents"] for a in asset_out)
            acc_unsettled = (
                sum(a["unsettled_cents"] for a in asset_out if a["unsettled_cents"] is not None)
                + (acc_realized - acc_settled_realized)
            )
            acc_out.append({
                "id": acc["id"],
                "name": acc["name"],
                "note": acc["note"] or "",
                "full_account_name": invest_account_name(acc["name"]),
                "assets": asset_out,
                "market_cents": sum(a["market_cents"] for a in asset_out if a["market_cents"] is not None) or None,
                "cost_cents": sum(a["cost_cents"] for a in asset_out),
                "realized_cents": acc_realized,
                "settled_realized_cents": acc_settled_realized,
                "unsettled_cents": acc_unsettled,
            })
        cat_out.append({
            "id": cat["id"],
            "name": cat["name"],
            "accounts": acc_out,
            "market_cents": cat_market if cat_market else None,
            "cost_cents": cat_cost,
            "realized_cents": cat_realized,
        })
    # 未建仓的理财账户余额计入总市值与累计投入（与总览 KPI 同口径）
    unpositioned_balance = _unpositioned_invest_balance_cents(conn)
    total_market += unpositioned_balance
    total_cost += unpositioned_balance
    return {
        "categories": cat_out,
        "totals": {
            "market_cents": total_market,
            "cost_cents": total_cost,
            "unrealized_cents": (total_market - total_cost) if total_market else None,
            "realized_total_cents": total_realized,
            "realized_month_cents": month_realized,
            "unsettled_cents": sum(
                acc["unsettled_cents"] for cat in cat_out for acc in cat["accounts"]
            ),
        },
    }


def refresh_prices(conn):
    """Refresh supported assets and return a compact result for the UI."""
    from quotes import auto_update_price
    assets = [dict(r) for r in conn.execute("select * from invest_assets order by id")]
    updated = 0
    failed = 0
    for asset in assets:
        try:
            if auto_update_price(conn, asset) is not None:
                updated += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"ok": True, "updated": updated, "failed": failed, "total": len(assets), "refreshed_at": _now_iso()}


def summary_snapshot(conn):
    """给总览 KPI 用的轻量快照：理财总市值（以现价与持仓计）。

    overview 已包含未建仓理财账户的余额（见 _unpositioned_invest_balance_cents），
    两个口径天然一致，无需在此重复计算。
    """
    try:
        data = overview(conn)
        totals = data["totals"]
        return {
            "invest_market_cents": totals["market_cents"],
            "invest_cost_cents": totals["cost_cents"],
            "invest_unrealized_cents": totals["unrealized_cents"],
            "invest_realized_month_cents": totals["realized_month_cents"],
        }
    except Exception:
        return {"invest_market_cents": 0, "invest_cost_cents": 0, "invest_unrealized_cents": None, "invest_realized_month_cents": 0}


# ---------------------------------------------------------------- CRUD

def add_category(conn, name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("大类名称不能为空")
    conn.execute("insert or ignore into invest_categories(name, created_at) values (?, ?)", (name, _now_iso()))
    return {"ok": True}


def delete_category(conn, category_id):
    used = conn.execute("select 1 from invest_accounts where category_id = ? limit 1", (category_id,)).fetchone()
    if used:
        raise ValueError("大类下还有账户，先删除账户")
    conn.execute("delete from invest_categories where id = ?", (category_id,))
    return {"ok": True}


def add_account(conn, category_id, name, note=""):
    name = str(name or "").strip()
    if not name:
        raise ValueError("账户名称不能为空")
    if not conn.execute("select 1 from invest_categories where id = ?", (category_id,)).fetchone():
        raise ValueError("大类不存在")
    conn.execute(
        "insert or ignore into invest_accounts(category_id, name, note, created_at) values (?, ?, ?, ?)",
        (category_id, name, note or "", _now_iso()),
    )
    return {"ok": True}


def delete_account(conn, account_id):
    used = conn.execute("select 1 from invest_assets where account_id = ? limit 1", (account_id,)).fetchone()
    if used:
        raise ValueError("账户下还有持仓标的，先删除标的")
    conn.execute("delete from invest_accounts where id = ?", (account_id,))
    return {"ok": True}


def add_asset(conn, account_id, name, code="", quantity="0", current_price_cents=None, note="", quote_currency="CNY", fx_cny_micros=1000000):
    name = str(name or "").strip()
    if not name:
        raise ValueError("标的名称不能为空")
    if not conn.execute("select 1 from invest_accounts where id = ?", (account_id,)).fetchone():
        raise ValueError("账户不存在")
    price = int(current_price_cents) if current_price_cents not in (None, "") else None
    quote_currency = str(quote_currency or "CNY").upper()
    if quote_currency not in SUPPORTED_QUOTE_CURRENCIES:
        raise ValueError("报价币种必须是 CNY 或 USD")
    fx_cny_micros = int(fx_cny_micros or 1000000)
    if fx_cny_micros <= 0:
        raise ValueError("汇率必须大于 0")
    conn.execute(
        "insert into invest_assets(account_id, name, code, quantity, current_price_cents, quote_currency, current_fx_cny_micros, note, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, name, str(code or "").strip(), str(_q(quantity or "0")), price, quote_currency, fx_cny_micros, note or "", _now_iso()),
    )
    return {"ok": True}


def delete_asset(conn, asset_id):
    for trade in conn.execute("select * from invest_trades where asset_id = ?", (asset_id,)).fetchall():
        acc = conn.execute(
            "select a.name name from invest_accounts a join invest_assets s on s.account_id = a.id where s.id = ?",
            (asset_id,),
        ).fetchone()
        _revert_cash_flow(conn, trade, acc["name"] if acc else "")
    conn.execute("delete from invest_trades where asset_id = ?", (asset_id,))
    conn.execute("delete from invest_price_history where asset_id = ?", (asset_id,))
    conn.execute("delete from invest_assets where id = ?", (asset_id,))
    return {"ok": True}


def update_asset_price(conn, asset_id, price_cents, source="manual", quote_currency=None, fx_cny_micros=None):
    price = int(price_cents)
    if price <= 0:
        raise ValueError("现价必须大于 0")
    asset = conn.execute("select * from invest_assets where id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValueError("标的不存在")
    quote_currency = str(quote_currency or asset["quote_currency"] or "CNY").upper()
    fx_cny_micros = int(fx_cny_micros or asset["current_fx_cny_micros"] or 1000000)
    if quote_currency not in SUPPORTED_QUOTE_CURRENCIES or fx_cny_micros <= 0:
        raise ValueError("报价币种或汇率无效")
    today = _dt.date.today().isoformat()
    conn.execute("update invest_assets set current_price_cents = ?, quote_currency = ?, current_fx_cny_micros = ? where id = ?", (price, quote_currency, fx_cny_micros, asset_id))
    conn.execute(
        "insert or replace into invest_price_history(asset_id, price_date, close_cents, source, created_at)"
        " values (?, ?, ?, ?, ?)",
        (asset_id, today, price, source, _now_iso()),
    )
    return {"ok": True}


def add_trade(conn, payload):
    asset = conn.execute("select * from invest_assets where id = ?", (payload.get("asset_id"),)).fetchone()
    if asset is None:
        raise ValueError("标的不存在")
    ttype = payload.get("trade_type")
    if ttype not in TRADE_TYPES:
        raise ValueError(f"交易类型必须是 {TRADE_TYPES} 之一")
    cash = str(payload.get("cash_account") or "").strip()
    if not cash:
        raise ValueError("请选择资金账户")
    ttime = str(payload.get("trade_time") or "").strip() or _now_iso()
    note = str(payload.get("note") or "").strip()
    qty = _q(payload.get("quantity") or "0")
    price_cents = int(payload["price_cents"]) if payload.get("price_cents") not in (None, "") else None
    price_currency = str(payload.get("price_currency") or asset["quote_currency"] or "CNY").upper()
    fx_cny_micros = int(payload.get("fx_cny_micros") or asset["current_fx_cny_micros"] or 1000000)
    fee = int(payload.get("fee_cents") or 0)

    if ttype in ("买入", "卖出"):
        if qty <= 0:
            raise ValueError("买入/卖出必须填数量")
        if price_cents is None or price_cents <= 0:
            raise ValueError("买入/卖出必须填单价")
        if price_currency not in SUPPORTED_QUOTE_CURRENCIES or fx_cny_micros <= 0:
            raise ValueError("报价币种或汇率无效")
        gross = int((qty * Decimal(price_cents) * Decimal(fx_cny_micros) / Decimal(1000000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        amount = gross + fee if ttype == "买入" else max(gross - fee, 0)
    else:
        amount = int(payload.get("amount_cents") or 0)
        if amount <= 0:
            raise ValueError(f"{ttype}必须填金额")
        qty = Decimal("0")
        price_cents = None

    acc = conn.execute(
        "select a.name name from invest_accounts a join invest_assets s on s.account_id = a.id where s.id = ?",
        (asset["id"],),
    ).fetchone()
    inv_name = acc["name"]
    trade = {
        "trade_type": ttype,
        "quantity": str(qty) if ttype in ("买入", "卖出") else None,
        "price_cents": price_cents,
        "price_currency": price_currency,
        "fx_cny_micros": fx_cny_micros,
        "amount_cents": amount,
        "fee_cents": fee,
        "cost_basis_cents": None,
        "realized_cents": None,
        "trade_time": ttime,
        "cash_account": cash,
        "note": note,
    }
    entry_id = _write_cash_flow(conn, trade, asset, inv_name)
    trade["entry_id"] = entry_id
    conn.execute(
        "insert into invest_trades(asset_id, trade_type, quantity, price_cents, price_currency, fx_cny_micros, amount_cents, fee_cents,"
        " cost_basis_cents, realized_cents, trade_time, cash_account, entry_id, note, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (asset["id"], ttype, trade["quantity"], price_cents, price_currency, fx_cny_micros, amount, fee, None, None, ttime, cash, entry_id, note, _now_iso()),
    )
    # 买入后若标的未设现价，用买入单价充当现价，方便新用户上手
    if ttype == "买入" and asset["current_price_cents"] is None and price_cents:
        conn.execute("update invest_assets set current_price_cents = ?, quote_currency = ?, current_fx_cny_micros = ? where id = ?", (price_cents, price_currency, fx_cny_micros, asset["id"]))
    return {"ok": True}


def delete_trade(conn, trade_id):
    trade = conn.execute("select * from invest_trades where id = ?", (trade_id,)).fetchone()
    if trade is None:
        raise ValueError("交易不存在")
    acc = conn.execute(
        "select a.name name from invest_accounts a join invest_assets s on s.account_id = a.id where s.id = ?",
        (trade["asset_id"],),
    ).fetchone()
    _revert_cash_flow(conn, trade, acc["name"] if acc else "")
    conn.execute("delete from invest_trades where id = ?", (trade_id,))
    return {"ok": True}


# ---------------------------------------------------------------- 结盈余
#
# 口径（与净额制一致）：
# - 未结浮盈 = 市值 − 摊薄成本 − 已结基线(settled_surplus_cents)；
# - 结盈余把未结部分真正计入收支：盈利 → 收入/投资收益，亏损 → 支出/投资亏损，
#   资金同步进出"理财·XX"账户（账户余额向市值看齐）；
# - 已实现盈亏平时不进收支，账户级一键结盈余时一次性结转（基线 settled_realized_cents 防重复）；
# - 每次结转同时写一条 invest_trades(type=结盈余) 便于审计与回滚。


def _market_value_cents(qty, price_cents, fx_cny_micros=1000000):
    return int((Decimal(qty) * Decimal(int(price_cents)) * Decimal(int(fx_cny_micros)) / Decimal(1000000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _asset_unsettled(conn, asset, positions):
    """返回 (unsettled_cents|None, market_cents|None, cost_cents, settled_cents)。

    - 持仓中且已设现价：unsettled = 市值 − 成本 − 已结基线；
    - 已平仓（数量为0）：unsettled = −已结基线（回收此前结过的浮盈，防止与已实现盈亏重复计收）；
    - 持仓中但未设现价：None（无法估值，跳过）。
    """
    pos = positions.get(asset["id"])
    settled = int(asset["settled_surplus_cents"] or 0)
    if pos is None:
        return None, None, 0, settled
    qty = pos["quantity"]
    cost = int(pos["cost_total"])
    price = asset["current_price_cents"]
    fx_cny_micros = int(asset["current_fx_cny_micros"] or 1000000)
    if qty <= 0:
        return -settled, 0, 0, settled
    if price is None:
        return None, None, cost, settled
    market = _market_value_cents(qty, price, fx_cny_micros)
    return market - cost - settled, market, cost, settled


def _write_settle_entry(conn, inv_full_name, delta_cents, ttime, note):
    """盈余落账：正记 收入/投资收益，负记 支出/投资亏损，资金进出理财账户。返回 entry_id。"""
    _ensure_invest_cash_account(conn, inv_full_name)
    if delta_cents > 0:
        conn.execute(
            "insert into entries(transaction_time, type, category, amount_cents, currency, account, participant, note)"
            " values (?, '收入', '投资收益', ?, 'CNY', ?, '自己', ?)",
            (ttime, delta_cents, inv_full_name, note),
        )
    else:
        conn.execute(
            "insert into entries(transaction_time, type, category, amount_cents, currency, account, participant, note)"
            " values (?, '支出', '投资亏损', ?, 'CNY', ?, '自己', ?)",
            (ttime, -delta_cents, inv_full_name, note),
        )
    entry_id = conn.execute("select last_insert_rowid()").fetchone()[0]
    _cash_apply(conn, inv_full_name, delta_cents)
    return entry_id


def settle_asset(conn, asset_id, note_prefix=""):
    """单只持仓兑现浮盈：盈亏计入收支并进理财账户，持仓数量不变，成本抬到当前市值，浮盈归零。"""
    asset = conn.execute("select * from invest_assets where id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValueError("持仓不存在")
    acc = conn.execute("select name from invest_accounts where id = ?", (asset["account_id"],)).fetchone()
    if acc is None:
        raise ValueError("账户不存在")
    positions = asset_positions(conn, [asset])
    unsettled, _market, _cost, _settled = _asset_unsettled(conn, asset, positions)
    if unsettled is None:
        raise ValueError("持仓需要先有现价，才能兑现浮盈")
    if unsettled == 0:
        raise ValueError("暂无可兑现浮盈")
    pos = positions.get(asset_id)
    qty = pos["quantity"] if pos else Decimal("0")
    if qty <= 0:
        raise ValueError("已经没有持仓，不能兑现浮盈。卖掉的盈亏请用卖出记录")
    inv_full = invest_account_name(acc["name"])
    ttime = _now_iso()
    note = f"{note_prefix}兑现浮盈·{asset['name']}"
    entry_id = _write_settle_entry(conn, inv_full, unsettled, ttime, note)
    conn.execute(
        "insert into invest_trades(asset_id, trade_type, quantity, price_cents, amount_cents, fee_cents,"
        " cost_basis_cents, realized_cents, trade_time, cash_account, entry_id, note, created_at)"
        " values (?, '兑现浮盈', NULL, NULL, ?, 0, NULL, NULL, ?, ?, ?, ?, ?)",
        (asset_id, unsettled, ttime, inv_full, entry_id, note, ttime),
    )
    # 把成本抬到当前市值：补一笔零数量买入，持仓数量不变，之后只计算新涨出来的部分。
    price = int(asset["current_price_cents"])
    conn.execute(
        "insert into invest_trades(asset_id, trade_type, quantity, price_cents, amount_cents, fee_cents,"
        " cost_basis_cents, realized_cents, trade_time, cash_account, entry_id, note, created_at)"
        " values (?, '买入', '0', ?, ?, 0, NULL, NULL, ?, NULL, NULL, ?, ?)",
        (asset_id, price, unsettled, ttime, f"兑现浮盈后重置成本·{asset['name']}", ttime),
    )
    return {"ok": True, "settled_cents": unsettled, "asset": asset["name"]}


def settle_account(conn, account_id):
    """账户一键结盈余：全部持仓标的的未结浮盈 + 未结已实现盈亏，逐笔计入收支，钱进理财账户。"""
    acc = conn.execute("select * from invest_accounts where id = ?", (account_id,)).fetchone()
    if acc is None:
        raise ValueError("账户不存在")
    assets = conn.execute("select * from invest_assets where account_id = ?", (account_id,)).fetchall()
    if not assets:
        raise ValueError("账户下没有标的")
    positions = asset_positions(conn, assets)
    total = 0
    settled_assets = 0
    for asset in assets:
        unsettled, _market, _cost, _s = _asset_unsettled(conn, asset, positions)
        if unsettled is None or unsettled == 0:
            continue
        total += unsettled
        settled_assets += 1
    realized_total = sum(positions[a["id"]]["realized_cents"] for a in assets if a["id"] in positions)
    realized_delta = realized_total - int(acc["settled_realized_cents"] or 0)
    total += realized_delta
    if total == 0:
        raise ValueError("暂无可结盈余（浮盈与已实现盈亏均已结转）")
    inv_full = invest_account_name(acc["name"])
    for asset in assets:
        unsettled, _market, _cost, _s = _asset_unsettled(conn, asset, positions)
        if unsettled is None or unsettled == 0:
            continue
        settle_asset(conn, asset["id"], note_prefix="账户一键")
    if realized_delta != 0:
        _write_settle_entry(
            conn, inv_full, realized_delta, _now_iso(),
            f"账户一键结盈余·已实现盈亏结转（{acc['name']}）",
        )
    conn.execute("update invest_accounts set settled_realized_cents = ? where id = ?", (realized_total, account_id))
    return {"ok": True, "settled_cents": total, "assets": settled_assets, "realized_cents": realized_delta}


# ---------------------------------------------------------------- migration

def migrate_seed_and_legacy(conn):
    """幂等迁移：初始大类 + 存量两笔币安卖币款转为卖出回款转账。"""
    for name in DEFAULT_CATEGORIES:
        conn.execute("insert or ignore into invest_categories(name, created_at) values (?, ?)", (name, _now_iso()))
    legacy = conn.execute(
        "select id, transaction_time, amount_cents, transaction_object, note from entries"
        " where type = '收入' and category = '币安卖币' and status = 'confirmed'"
    ).fetchall()
    if not legacy:
        return
    cat = conn.execute("select id from invest_categories where name = '数字货币'").fetchone()
    if cat is None:
        return
    conn.execute(
        "insert or ignore into invest_accounts(category_id, name, note, created_at) values (?, '币安', '迁移自动创建', ?)",
        (cat["id"], _now_iso()),
    )
    acc = conn.execute("select id from invest_accounts where name = '币安'").fetchone()
    conn.execute(
        "insert or ignore into invest_assets(account_id, name, code, quantity, note, created_at)"
        " values (?, '数字货币（待细分）', '', '0', '成本未回填：编辑买入交易后自动算盈亏', ?)",
        (acc["id"], _now_iso()),
    )
    asset = conn.execute("select * from invest_assets where account_id = ? order by id limit 1", (acc["id"],)).fetchone()
    inv_full = invest_account_name("币安")
    _ensure_invest_cash_account(conn, inv_full)
    for row in legacy:
        ts = _now_iso()
        entry = conn.execute("select * from entries where id = ?", (row["id"],)).fetchone()
        if entry is None:
            continue
        cash = entry["account"] or "余额宝"
        # 余额宝此前按收入 +amount 计，转为转账后：理财·币安 -amount、现金账户不变
        conn.execute(
            "update entries set type = '转账', category = '转账', account = ?, target_account = ?,"
            " note = ? where id = ?",
            (inv_full, cash, (entry["note"] or "") + "（原币安卖币·理财卖出回款）", row["id"]),
        )
        _cash_apply(conn, inv_full, -entry["amount_cents"])
        conn.execute(
            "insert into invest_trades(asset_id, trade_type, quantity, price_cents, amount_cents, fee_cents,"
            " cost_basis_cents, realized_cents, trade_time, cash_account, entry_id, note, created_at)"
            " values (?, '卖出', NULL, NULL, ?, 0, NULL, NULL, ?, ?, ?, ?, ?)",
            (asset["id"], entry["amount_cents"], entry["transaction_time"], cash, row["id"],
             f"迁移自收入记录（买家 {entry['transaction_object'] or ''}）", ts),
        )


def run_all(conn):
    ensure_schema(conn)
    migrate_seed_and_legacy(conn)
