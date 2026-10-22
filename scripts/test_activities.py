# -*- coding: utf-8 -*-
"""动作（activities）功能 TDD 测试：临时库运行，断言核心行为。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_activities_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts"))
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts/bookkeeping_app"))

import bookkeeping as bk  # noqa: E402

bk.init_db()

# ---- 准备数据：3 笔支出（40/100/25）+ 1 笔退款 40 关联到第一笔 ----
e1 = bk.insert_entry({"transaction_time": "2026-10-04 10:00:00", "type": "支出", "category": "交通/火车高铁", "amount": 40.00, "account": "现金", "transaction_object": "高铁票"})["id"]
e2 = bk.insert_entry({"transaction_time": "2026-10-04 12:00:00", "type": "支出", "category": "居家/住宿房租", "amount": 100.00, "account": "现金", "transaction_object": "酒店"})["id"]
e3 = bk.insert_entry({"transaction_time": "2026-10-05 09:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 25.00, "account": "现金", "transaction_object": "午餐"})["id"]
r1 = bk.insert_entry({"transaction_time": "2026-10-06 09:00:00", "type": "退款", "category": "交通/火车高铁", "amount": 40.00, "account": "现金", "transaction_object": "高铁票退票"})["id"]
bk.link_refund(r1, e1)

# ---- 1. 建动作 ----
act = bk.create_activity("10月旅游", "2026-10-04", "2026-10-05")
aid = act["id"]
assert aid > 0
try:
    bk.create_activity("10月旅游")
    raise AssertionError("重名应被拒绝")
except ValueError:
    pass

# ---- 2. 批量挂账单 ----
bk.set_entries_activity([e1, e2, e3], aid)
conn = bk.connect()
tagged = conn.execute("select count(*) c from entries where activity_id = ?", (aid,)).fetchone()["c"]
assert tagged == 3, f"应有3笔挂载，实际{tagged}"

# ---- 3. 统计：毛 165，冲减 40（退款跟随原支出），净 125 ----
rows = bk.list_activities()
row = [r for r in rows if r["id"] == aid][0]
assert row["expense_count"] == 3, row
assert row["gross_cents"] == 16500, row
assert row["refund_cents"] == 4000, row
assert row["net_cents"] == 12500, row
assert row["start_date"] == "2026-10-04" and row["end_date"] == "2026-10-05"

# ---- 4. 动作明细：3 支出 + 1 跟随退款 ----
detail = bk.activity_entries(aid)
assert len(detail["expenses"]) == 3
assert len(detail["refunds"]) == 1 and detail["refunds"][0]["id"] == r1

# ---- 5. 摘除一笔：统计同步变化 ----
bk.set_entries_activity([e3], None)
row = [r for r in bk.list_activities() if r["id"] == aid][0]
assert row["gross_cents"] == 14000 and row["net_cents"] == 10000, row

# ---- 6. 编辑动作改名 ----
bk.update_activity(aid, {"name": "国庆出游"})
row = [r for r in bk.list_activities() if r["id"] == aid][0]
assert row["name"] == "国庆出游"

# ---- 7. 删除动作：账单保留、归属清空 ----
bk.delete_activity(aid)
conn = bk.connect()
left = conn.execute("select count(*) c from entries where id in (?,?,?) and activity_id is null", (e1, e2, e3)).fetchone()["c"]
total = conn.execute("select count(*) c from entries where id in (?,?,?)", (e1, e2, e3)).fetchone()["c"]
conn.close()
assert left == 3 and total == 3, "删除动作后账单应保留且归属清空"
assert not [r for r in bk.list_activities() if r["id"] == aid]

# ---- 8. update_entry 可编辑 activity_id（空串→None）----
act2 = bk.create_activity("约会")["id"]
bk.update_entry(e1, {"activity_id": str(act2)})
conn = bk.connect()
v = conn.execute("select activity_id from entries where id = ?", (e1,)).fetchone()["activity_id"]
conn.close()
assert v == act2
bk.update_entry(e1, {"activity_id": ""})
conn = bk.connect()
v = conn.execute("select activity_id from entries where id = ?", (e1,)).fetchone()["activity_id"]
conn.close()
assert v is None

# ---- 9. 挂不存在的动作被拒 ----
try:
    bk.set_entries_activity([e1], 99999)
    raise AssertionError("不存在的动作应被拒绝")
except ValueError:
    pass

print("ALL ACTIVITY TESTS PASSED")
