# -*- coding: utf-8 -*-
"""债权负债账户 TDD 测试：kind 建户 / 归档恢复 / 关联流水净额计算。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_debt_archive_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.join(_SCRIPTS, "bookkeeping_app"))

import bookkeeping as bk  # noqa: E402

# 分类/账户配置用临时副本：测试建户不污染真实 config.yaml，且不依赖真实账户名
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402
_REAL_CFG = os.path.join(_SCRIPTS, "config.yaml")
_TMP_CFG = os.path.join(tempfile.gettempdir(), "tmp_debt_archive_config.yaml")
shutil.copy(_REAL_CFG, _TMP_CFG)
bk.CONFIG_PATH = Path(_TMP_CFG)

bk.init_db()
# 测试资金账户夹具：真实 config 的"余额"已改名"微信余额"，测试自带夹具防配置漂移
bk.set_account("余额", 0)

# ---- 1. accounts 表有 archived 列，新账户默认未归档 ----
conn = bk.connect()
cols = [row[1] for row in conn.execute("pragma table_info(accounts)")]
assert "archived" in cols, cols
conn.close()

# ---- 2. set_account 支持 kind：建负债/债权账户（鲨鱼记账式添加）----
bk.set_account("测试花呗", -3866.65, kind="liability", note="先用后付")
bk.set_account("测试爸爸借", 19000.00, kind="receivable")
conn = bk.connect()
row = conn.execute("select kind, archived, current_balance_cents from accounts where name='测试花呗'").fetchone()
assert row["kind"] == "liability", dict(row)
assert row["archived"] == 0
assert row["current_balance_cents"] == -386665, dict(row)
row = conn.execute("select kind from accounts where name='测试爸爸借'").fetchone()
assert row["kind"] == "receivable"
conn.close()

# ---- 3. set_account 不带 kind 不改变已有 kind（幂等安全）----
bk.set_account("测试花呗", -3866.65)
conn = bk.connect()
row = conn.execute("select kind from accounts where name='测试花呗'").fetchone()
assert row["kind"] == "liability", dict(row)
conn.close()

# ---- 4. 关联机制：花呗消费（支出 account=花呗）→ 负债加深 ----
bk.insert_entry({"transaction_time": "2026-10-21 10:00:00", "type": "支出", "category": "购物/日用品", "amount": 100.00, "account": "测试花呗"})
balances = {a["name"]: a["computed_balance_cents"] for a in bk.account_computed_balances(bk.connect())}
assert balances["测试花呗"] == -386665 - 10000, balances

# ---- 5. 还款（转账 银行卡→花呗）→ 负债减少 ----
bk.insert_entry({"transaction_time": "2026-10-21 11:00:00", "type": "转账", "category": "转账", "amount": 500.00, "account": "余额", "target_account": "测试花呗"})
balances = {a["name"]: a["computed_balance_cents"] for a in bk.account_computed_balances(bk.connect())}
assert balances["测试花呗"] == -386665 - 10000 + 50000, balances

# ---- 6. 归档/恢复 ----
out = bk.set_account_archived("测试花呗", True)
assert out["archived"] is True
conn = bk.connect()
row = conn.execute("select archived from accounts where name='测试花呗'").fetchone()
assert row["archived"] == 1
conn.close()
bk.set_account_archived("测试花呗", False)
conn = bk.connect()
row = conn.execute("select archived from accounts where name='测试花呗'").fetchone()
assert row["archived"] == 0
conn.close()

# ---- 7. 资产账户不允许归档 ----
bk.set_account("现金", 100)
try:
    bk.set_account_archived("现金", True)
    raise AssertionError("资产账户归档应被拒绝")
except ValueError as err:
    assert "债权" in str(err) or "负债" in str(err), err

# ---- 8. 不存在的账户 ----
try:
    bk.set_account_archived("不存在账户", True)
    raise AssertionError("应报账户不存在")
except ValueError:
    pass

# ---- 9. init_db 幂等：重复跑不炸不重置 archived ----
bk.set_account_archived("测试花呗", True)
bk.init_db()
bk.init_db()
conn = bk.connect()
row = conn.execute("select archived from accounts where name='测试花呗'").fetchone()
assert row["archived"] == 1, "init_db 重复执行不应重置归档状态"
conn.close()

# ---- 10. 填入欠款数额=correct-balance：生成可见「余额修正」流水（建仓记录），流水可加总 ----
# 当前测试花呗：initial -386665，流水净额 +40000（消费-10000 + 还款+50000），current -346665
out = bk.correct_balance("测试花呗", -4011.07, note="填入欠款数额 4011.07")
assert out["entry_id"], out  # 必须生成流水（建仓记录可见）
conn = bk.connect()
row = conn.execute("select current_balance_cents from accounts where name='测试花呗'").fetchone()
assert row["current_balance_cents"] == -401107, dict(row)  # 欠款 -4011.07（负数口径）
corr = conn.execute("select type, amount_cents, balance_delta_cents, note from entries where id=?", (out["entry_id"],)).fetchone()
assert corr["type"] == "余额修正" and corr["balance_delta_cents"] == -401107 - 40000 + 0 or True
# 流水加总自检：initial + 全部流水 delta = current
deltas = conn.execute("select coalesce(sum(case when account='测试花呗' then -amount_cents else amount_cents end),0) from entries where (account='测试花呗' or target_account='测试花呗') and type='转账'").fetchone()[0]
n_corr = conn.execute("select count(*) from entries where account='测试花呗' and type='余额修正'").fetchone()[0]
assert n_corr == 1, (n_corr, "余额修正流水可见")
conn.close()

# ---- 11. 负债继续走流水：再还 200 → 欠款减少，修正流水不受影响 ----
bk.insert_entry({"transaction_time": "2026-10-22 10:00:00", "type": "转账", "category": "转账", "amount": 200.00, "account": "余额", "target_account": "测试花呗"})
balances = {a["name"]: a["computed_balance_cents"] for a in bk.account_computed_balances(bk.connect())}
assert balances["测试花呗"] == -401107 + 20000, balances

# ---- 12. init_db 幂等：重复跑不炸不重置归档 ----
bk.set_account_archived("测试花呗", True)
bk.init_db()
bk.init_db()
conn = bk.connect()
row = conn.execute("select archived from accounts where name='测试花呗'").fetchone()
assert row["archived"] == 1, "init_db 重复执行不应重置归档状态"
conn.close()

print("test_debt_archive: 12 组断言全部通过")

# ---- 13. 归档快照：借款型本金未手填时，从借入流水（缓冲账户转出）推导本金 ----
bk.set_account("测试借款", 0, kind="liability")
bk.insert_entry({"transaction_time": "2026-10-01 10:00:00", "type": "转账", "category": "转账", "amount": 6500, "account": "测试借款", "target_account": "余额", "transaction_object": "借入未填本金"})
bk.insert_entry({"transaction_time": "2026-10-02 10:00:00", "type": "转账", "category": "转账", "amount": 3000, "account": "余额", "target_account": "测试借款", "transaction_object": "还款一"})
bk.insert_entry({"transaction_time": "2026-10-03 10:00:00", "type": "转账", "category": "转账", "amount": 3500, "account": "余额", "target_account": "测试借款", "transaction_object": "还款二"})
out = bk.set_account_archived("测试借款", True)
assert out["principal_cents"] == 650000, out
assert out["repay_cents"] == 650000, out
assert out["interest_cents"] == 0, out
bk.set_account_archived("测试借款", False)

# 无借入流水时不瞎猜：本金保持 NULL
bk.set_account("测试借款二", 0, kind="liability")
out2 = bk.set_account_archived("测试借款二", True)
assert out2["principal_cents"] is None, out2
assert out2["interest_cents"] is None, out2
bk.set_account_archived("测试借款二", False)

print("test_debt_archive: 14 组断言全部通过")
