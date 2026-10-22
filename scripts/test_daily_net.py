# -*- coding: utf-8 -*-
"""按日净支出统计（net_expense_stats）TDD：退款冲减归属原支出日，跨日期/跨月也归位。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_daily_net_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts"))
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts/bookkeeping_app"))

import bookkeeping as bk  # noqa: E402

bk.init_db()

# ---- 数据 ----
# 10-04: 支出 300（餐饮）+ 支出 200（旅行）；同日退款 100 冲减餐饮支出
# 10-05: 退款 200 冲减 10-04 的旅行支出（跨日归位）
# 10-31: 支出 500（交通）；11-02 退款 500 冲减它（跨月归位）
# 10-06: 退款 88 未关联 → 不冲减任何收支
a1 = bk.insert_entry({"transaction_time": "2026-10-04 10:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 300.00, "account": "现金"})["id"]
a2 = bk.insert_entry({"transaction_time": "2026-10-04 12:00:00", "type": "支出", "category": "旅行", "amount": 200.00, "account": "现金"})["id"]
a3 = bk.insert_entry({"transaction_time": "2026-10-31 20:00:00", "type": "支出", "category": "交通/打车", "amount": 500.00, "account": "现金"})["id"]
r1 = bk.insert_entry({"transaction_time": "2026-10-04 18:00:00", "type": "退款", "category": "餐饮/早午晚餐", "amount": 100.00, "account": "现金"})["id"]
r2 = bk.insert_entry({"transaction_time": "2026-10-05 09:00:00", "type": "退款", "category": "旅行", "amount": 200.00, "account": "现金"})["id"]
r3 = bk.insert_entry({"transaction_time": "2026-11-02 09:00:00", "type": "退款", "category": "交通/打车", "amount": 500.00, "account": "现金"})["id"]
r4 = bk.insert_entry({"transaction_time": "2026-10-06 09:00:00", "type": "退款", "category": "旅行", "amount": 88.00, "account": "现金"})["id"]
bk.link_refund(r1, a1)
bk.link_refund(r2, a2)
bk.link_refund(r3, a3)

# ---- 1. 十月按日净额：同日冲减 + 跨日归位 + 跨月退款也归属 10-31 ----
# （第 4 返回值为还款口径 repay_by_category，本测试无 repay 负债，恒为空）
by_day, refund_by_category, totals, repay_by_category = bk.net_expense_stats(bk.connect(), "2026-10")
assert repay_by_category == {}, repay_by_category
assert by_day["2026-10-04"] == {"gross": 50000, "offset": 30000, "net": 20000}, by_day["2026-10-04"]
assert by_day["2026-10-31"] == {"gross": 50000, "offset": 50000, "net": 0}, by_day["2026-10-31"]
assert "2026-10-05" not in by_day, "退款日(10-05)本身无支出，不应出现在净额表"
assert totals["gross"] == 100000 and totals["offset"] == 80000 and totals["net"] == 20000, totals

# ---- 2. 分类冲减按原支出分类 ----
assert refund_by_category.get("餐饮/早午晚餐") == 10000, refund_by_category
assert refund_by_category.get("旅行") == 20000, refund_by_category
assert refund_by_category.get("交通/打车") == 50000, refund_by_category

# ---- 3. 十一月：退款发生在 11 月但原支出在 10 月 → 11 月无毛支出、无冲减 ----
by_day11, _, totals11, _ = bk.net_expense_stats(bk.connect(), "2026-11")
assert by_day11 == {} and totals11 == {"gross": 0, "offset": 0, "net": 0}, (by_day11, totals11)

# ---- 4. 未关联退款不冲减 ----
assert totals["offset"] == 80000, "未关联的 88 元退款不应计入冲减"

# ---- 5. 与 api_summary 口径联动：当月汇总 net = 毛 − 冲减 ----
conn = bk.connect()
summary_gross = conn.execute(
    "select coalesce(sum(amount_cents),0) v from entries where type='支出' and coalesce(category,'')!='转账' and substr(transaction_time,1,7)='2026-10'"
).fetchone()["v"]
conn.close()
assert summary_gross == totals["gross"], (summary_gross, totals["gross"])

print("test_daily_net: 全部断言通过")
