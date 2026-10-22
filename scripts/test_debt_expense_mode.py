# -*- coding: utf-8 -*-
"""负债三型（信用卡型/借款型/贷款型）TDD：统计口径 / 利息 / 防双计。
对应 docs/design-debt-expense-mode.md §11 测试计划 13 条。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_debt_expense_mode_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.join(_SCRIPTS, "bookkeeping_app"))

import bookkeeping as bk  # noqa: E402
import debt_accounts as da  # noqa: E402

# 分类配置也用临时副本：测试建户不污染真实 config.yaml 的 accounts 列表
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402
_REAL_CFG = os.path.join(_SCRIPTS, "config.yaml")
_TMP_CFG = os.path.join(tempfile.gettempdir(), "tmp_debt_expense_mode_config.yaml")
shutil.copy(_REAL_CFG, _TMP_CFG)
bk.CONFIG_PATH = Path(_TMP_CFG)

bk.init_db()
# 测试资金账户夹具：真实 config 的"余额"已改名"微信余额"，测试自带夹具防配置漂移
# （insert_entry 的转账要求 account/target 均为已知账户，否则静默进待确认、断言空转）
bk.set_account("余额", 0)

MONTH = "2026-02"


def net(month=MONTH):
    return bk.net_expense_stats(bk.connect(), month)


def account_row(name):
    conn = bk.connect()
    row = conn.execute("select * from accounts where name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---- 1. 花呗消费统计不变：消费 100 计支出、还款转账 100 不计，月净支出 = 100（现状回归）----
bk.set_account("花呗", -500, kind="liability")
assert account_row("花呗")["expense_mode"] == "consume"
assert account_row("花呗")["expense_mode_locked"] == 1
bk.insert_entry({"transaction_time": f"{MONTH}-01 10:00:00", "type": "支出", "category": "购物/日用品", "amount": 100.00, "account": "花呗"})
bk.insert_entry({"transaction_time": f"{MONTH}-02 10:00:00", "type": "转账", "category": "信用卡还款", "amount": 100.00, "account": "余额", "target_account": "花呗"})
by_day, refund_by_category, totals, repay_by_category = net()
assert totals["net"] == 10000, totals
assert "信用卡还款" not in repay_by_category, repay_by_category

# ---- 2. 房贷月供计入支出：repay 账户「房贷」4640 → 当月净支出 +4640，分类含「房贷」----
bk.set_account("房贷", -1000000, kind="liability")
row = account_row("房贷")
assert row["expense_mode"] == "repay" and row["expense_mode_locked"] == 0, row
bk.insert_entry({"transaction_time": f"{MONTH}-05 10:00:00", "type": "转账", "category": "房贷", "amount": 4640.00, "account": "余额", "target_account": "房贷"})
by_day, refund_by_category, totals, repay_by_category = net()
assert totals["net"] == 10000 + 464000, totals
assert repay_by_category == {"房贷": 464000}, repay_by_category
assert by_day[f"{MONTH}-05"]["net"] == 464000, by_day
# api_summary 只算当前月：记一笔当月月供，断言饼图分类含「房贷」
import datetime  # noqa: E402
today = datetime.date.today().strftime("%Y-%m-%d")
bk.insert_entry({"transaction_time": f"{today} 10:00:00", "type": "转账", "category": "房贷", "amount": 2000.00, "account": "余额", "target_account": "房贷"})
summary = bk.api_summary()
assert any(c["category"] == "房贷" and c["amount_cents"] == 200000 for c in summary["categories"]), summary["categories"]

# ---- 3. 普通转账不计：repay 账户收到「转账」分类转账 → 支出不变，响应含 warning ----
result = bk.insert_entry({"transaction_time": f"{MONTH}-06 10:00:00", "type": "转账", "category": "转账", "amount": 300.00, "account": "余额", "target_account": "房贷"})
assert result.get("warning"), result
by_day, _, totals, repay_by_category = net()
assert totals["net"] == 10000 + 464000, totals

# ---- 4. 双重计数硬拦：repay 负债账户记 type=支出 → 拒绝；同一笔走转账只计一次 ----
try:
    bk.insert_entry({"transaction_time": f"{MONTH}-07 10:00:00", "type": "支出", "category": "居家/住宿房租", "amount": 4640.00, "account": "房贷"})
    raise AssertionError("repay 负债账户记支出应被硬拦")
except ValueError as err:
    assert "还款口径" in str(err), err
bk.insert_entry({"transaction_time": f"{MONTH}-07 10:00:00", "type": "转账", "category": "房贷", "amount": 4640.00, "account": "余额", "target_account": "房贷"})
by_day, _, totals, _ = net()
assert totals["net"] == 10000 + 464000 + 464000, totals  # 月供只计一次（第 7 日这笔）

# ---- 5. 网贷案例：本金 20000，12 笔还款各 1834 → 实时 interest_cents = 2008 ----
bk.set_account("网贷", 0, kind="liability", expense_mode="consume", principal="20000")
row = account_row("网贷")
assert row["expense_mode"] == "consume" and row["expense_mode_locked"] == 0, row
assert row["principal_cents"] == 2000000, row
for i in range(12):
    bk.insert_entry({
        "transaction_time": f"2026-{i % 12 + 1:02d}-15 10:00:00",
        "type": "转账", "category": "转账", "amount": 1834.00,
        "account": "余额", "target_account": "网贷",
    })
summary = bk.api_summary()
rows = {a["name"]: a for a in summary["account_balances"]}
assert rows["网贷"]["repaid_cents"] == 12 * 183400, rows["网贷"]
assert rows["网贷"]["interest_cents"] == 12 * 183400 - 2000000 == 200800, rows["网贷"]
assert rows["网贷"]["debt_type"] == "借款", rows["网贷"]

# ---- 6. 归档快照：归档响应含快照；追加流水不影响快照；恢复清空、重新归档重算 ----
out = bk.set_account_archived("网贷", True)
assert out["principal_cents"] == 2000000 and out["repay_cents"] == 12 * 183400, out
assert out["interest_cents"] == 200800, out
bk.insert_entry({"transaction_time": "2026-12-20 10:00:00", "type": "转账", "category": "转账", "amount": 100.00, "account": "余额", "target_account": "网贷"})
row = account_row("网贷")
assert row["archived_repay_cents"] == 12 * 183400, row  # 快照不随后续流水变化
assert row["archived_interest_cents"] == 200800, row
bk.set_account_archived("网贷", False)
row = account_row("网贷")
assert row["archived_principal_cents"] is None and row["archived_repay_cents"] is None and row["archived_interest_cents"] is None, row
bk.set_account_archived("网贷", True)
row = account_row("网贷")
assert row["archived_repay_cents"] == 12 * 183400 + 10000, row  # 重新归档重算
bk.set_account_archived("网贷", False)

# ---- 7. 口径写死：花呗 expense_mode_locked=1，改口径 → 400 且值不变 ----
try:
    bk.set_account("花呗", expense_mode="repay")
    raise AssertionError("锁定账户改口径应被拒绝")
except ValueError as err:
    assert "锁定" in str(err), err
assert account_row("花呗")["expense_mode"] == "consume"

# ---- 8. 口径切换统计变化：借款-XX 由 consume 改 repay 后，还款类转账从「不计」变「计入」----
bk.set_account("借款-老李", 0, kind="liability")  # 默认 consume
total_before_switch = net()[2]["net"]
assert total_before_switch == 10000 + 464000 + 464000, total_before_switch  # 前序各笔的基准
bk.insert_entry({"transaction_time": f"{MONTH}-08 10:00:00", "type": "转账", "category": "房贷", "amount": 800.00, "account": "余额", "target_account": "借款-老李"})
assert net()[2]["net"] == total_before_switch, "consume 口径下还款转账不计入支出"
bk.set_account("借款-老李", expense_mode="repay")
assert account_row("借款-老李")["expense_mode"] == "repay"
_, _, totals_after, repay_after = net()
assert totals_after["net"] == total_before_switch + 80000, (total_before_switch, totals_after)
assert repay_after.get("房贷") == 464000 + 464000 + 80000, repay_after  # 2 月两笔月供 + 口径切换后 800（当月 2000 那笔属 10 月）

# ---- 9. 幂等迁移：init_db 连跑两次，expense_mode/locked/principal 与流水不变 ----
before = {
    name: {k: row[k] for k in ("expense_mode", "expense_mode_locked", "principal_cents")}
    for name, row in [(n, account_row(n)) for n in ("花呗", "房贷", "网贷", "借款-老李")]
}
conn = bk.connect()
entries_before = conn.execute("select count(*) c, coalesce(sum(amount_cents),0) s from entries").fetchone()
conn.close()
bk.init_db()
bk.init_db()
after = {
    name: {k: row[k] for k in ("expense_mode", "expense_mode_locked", "principal_cents")}
    for name, row in [(n, account_row(n)) for n in ("花呗", "房贷", "网贷", "借款-老李")]
}
assert before == after, (before, after)
conn = bk.connect()
entries_after = conn.execute("select count(*) c, coalesce(sum(amount_cents),0) s from entries").fetchone()
conn.close()
assert dict(entries_before) == dict(entries_after), (dict(entries_before), dict(entries_after))

# ---- 10. principal 为 NULL：summary 返回 interest_cents=null ----
bk.set_account("借款-未填本金", 0, kind="liability")
summary = bk.api_summary()
rows = {a["name"]: a for a in summary["account_balances"]}
assert rows["借款-未填本金"]["principal_cents"] is None, rows["借款-未填本金"]
assert rows["借款-未填本金"]["interest_cents"] is None, rows["借款-未填本金"]

# ---- 11. 信用卡型无本金/无利息：API 忽略 principal → principal_cents 保持 NULL；归档快照三列 NULL ----
try:
    bk.set_account("花呗", principal="3000")
    raise AssertionError("信用卡型传本金应被拒绝")
except ValueError as err:
    assert "本金" in str(err), err
assert account_row("花呗")["principal_cents"] is None
out = bk.set_account_archived("花呗", True)
assert out["principal_cents"] is None and out["repay_cents"] is None and out["interest_cents"] is None, out
row = account_row("花呗")
assert row["archived_principal_cents"] is None and row["archived_repay_cents"] is None and row["archived_interest_cents"] is None, row
bk.set_account_archived("花呗", False)

# ---- 12. 三型徽章派生（与前端 deriveDebtType 同规则的后端断言）----
assert da.derive_debt_type("consume", 1) == "信用卡"
assert da.derive_debt_type("consume", 0) == "借款"
assert da.derive_debt_type("repay", 0) == "贷款"
assert da.derive_debt_type("repay", 1) == "贷款"
assert da.derive_debt_type("", 0) is None
summary = bk.api_summary()
rows = {a["name"]: a for a in summary["account_balances"]}
assert rows["花呗"]["debt_type"] == "信用卡", rows["花呗"]
assert rows["房贷"]["debt_type"] == "贷款"
assert rows["借款-老李"]["debt_type"] == "贷款"  # 测试 8 已切 repay

# ---- 13. 类型化添加：负债→贷款型提交后 expense_mode='repay'、principal 落库；信用卡型锁定 ----
bk.set_account("车贷", -80000, kind="liability", expense_mode="repay", principal="80000")
row = account_row("车贷")
assert row["expense_mode"] == "repay" and row["principal_cents"] == 8000000, row
bk.set_account("E2E花呗型", -300, kind="liability", expense_mode="consume")
row = account_row("E2E花呗型")
assert row["expense_mode"] == "consume" and row["expense_mode_locked"] == 1, row
# 还款类分类基建：config 转账组含 房贷/车贷（幂等，valid 平铺）
assert "房贷" in bk.valid_categories() and "车贷" in bk.valid_categories()
# guess_expense_mode 单元断言
assert da.guess_expense_mode("花呗") == ("consume", 1)
assert da.guess_expense_mode("房贷") == ("repay", 0)
assert da.guess_expense_mode("车贷") == ("repay", 0)
assert da.guess_expense_mode("借款-张三") == ("consume", 0)

# ---- 14. principal 正数校验（QA 轮次 2 补充）：0/负数/带负号字符串均拒绝，principal_cents 保持 NULL ----
bk.set_account("借款-本金校验", 0, kind="liability")
for bad in (0, -100, "−100", "-50"):
    try:
        bk.set_account("借款-本金校验", principal=bad)
        raise AssertionError(f"principal={bad!r} 应被拒绝")
    except ValueError:
        pass
assert account_row("借款-本金校验")["principal_cents"] is None, account_row("借款-本金校验")
# 合法正数仍可写入；未传/空串维持 NULL=未填
bk.set_account("借款-本金校验", principal="12000")
assert account_row("借款-本金校验")["principal_cents"] == 1200000
bk.set_account("借款-本金校验", principal="")
assert account_row("借款-本金校验")["principal_cents"] == 1200000  # 空串不改动既有本金

print("test_debt_expense_mode: 14 组断言全部通过")
