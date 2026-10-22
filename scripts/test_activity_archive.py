# -*- coding: utf-8 -*-
"""活动归档 TDD 测试：临时库运行，断言核心行为。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_archive_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts"))
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts/bookkeeping_app"))

import bookkeeping as bk  # noqa: E402

bk.init_db()

e1 = bk.insert_entry({"transaction_time": "2026-10-04 10:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 50.00, "account": "现金"})["id"]
act = bk.create_activity("老活动")
aid = act["id"]

# ---- 1. 新活动默认未归档 ----
rows = bk.list_activities()
row = [r for r in rows if r["id"] == aid][0]
assert row["archived"] == 0, row

# ---- 2. 归档 ----
out = bk.set_activity_archived(aid, True)
assert out["archived"] is True
row = [r for r in bk.list_activities() if r["id"] == aid][0]
assert row["archived"] == 1

# ---- 3. 归档后挂载被拒绝 ----
try:
    bk.set_entries_activity([e1], aid)
    raise AssertionError("归档活动挂载应被拒绝")
except ValueError as err:
    assert "归档" in str(err), err

# ---- 4. 归档不清账单：已挂载的保留 ----
bk.set_activity_archived(aid, False)  # 先恢复再挂载
bk.set_entries_activity([e1], aid)
bk.set_activity_archived(aid, True)  # 再归档
detail = bk.activity_entries(aid)
assert len(detail["expenses"]) == 1, detail
conn = bk.connect()
assert conn.execute("select activity_id from entries where id = ?", (e1,)).fetchone()["activity_id"] == aid

# ---- 5. 恢复后可继续挂载 ----
bk.set_activity_archived(aid, False)
bk.set_entries_activity([e1], aid)  # 不报错
assert True

# ---- 6. 不存在的活动 ----
try:
    bk.set_activity_archived(99999, True)
    raise AssertionError("应报活动不存在")
except ValueError:
    pass

print("test_activity_archive: 6 组断言全部通过")
