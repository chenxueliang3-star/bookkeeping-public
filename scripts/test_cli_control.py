# -*- coding: utf-8 -*-
"""CLI 日常控制：流水改删、退款手动关联、活动挂载/归档、资产增改。走 main() 不直调业务函数。"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

TMP = os.path.join(tempfile.gettempdir(), "tmp_cli_control_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.join(_SCRIPTS, "bookkeeping_app"))

import bookkeeping as bk  # noqa: E402


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        bk.main(argv)
    text = buf.getvalue().strip()
    return json.loads(text) if text.startswith("{") or text.startswith("[") else text


bk.init_db()

# ---- 流水：改分类 / 删 ----
added = run(["add", "--type", "支出", "--amount", "30", "--account", "现金",
             "--category", "交通/加油充电", "--transaction-object", "充电桩",
             "--time", "2026-10-10 10:00:00"])
eid = added["id"]
upd = run(["entry-update", str(eid), "--note", "含押金"])
assert upd.get("ok") is True or upd.get("id") == eid or "ok" in str(upd).lower(), upd
conn = bk.connect()
assert conn.execute("select note from entries where id=?", (eid,)).fetchone()["note"] == "含押金"
conn.close()

# ---- 退款关联 / 解除 ----
refund = run(["add", "--type", "退款", "--amount", "28.40", "--account", "现金",
              "--transaction-object", "充电桩", "--time", "2026-10-10 12:00:00"])
rid = refund["id"]
linked = run(["refund-link", "--refund-id", str(rid), "--expense-id", str(eid)])
assert linked["ok"] is True and linked["expense_id"] == eid, linked
conn = bk.connect()
assert conn.execute("select refund_pair_id from entries where id=?", (rid,)).fetchone()["refund_pair_id"] == eid
conn.close()
cands = run(["refund-candidates", "--merchant", "充电桩"])
assert isinstance(cands, (dict, list))
unlinked = run(["refund-unlink", "--refund-id", str(rid)])
assert unlinked["ok"] is True, unlinked
conn = bk.connect()
assert conn.execute("select refund_pair_id from entries where id=?", (rid,)).fetchone()["refund_pair_id"] is None
conn.close()
run(["refund-link", "--refund-id", str(rid), "--expense-id", str(eid)])

# ---- 活动：建、挂、列表、归档拒绝再挂、摘、恢复 ----
act = run(["activity-add", "--name", "充电测试"])
aid = act["id"]
hung = run(["entry-activity", "--entry-id", str(eid), "--activity-id", str(aid)])
assert hung["ok"] is True and hung["count"] == 1, hung
acts = run(["activity-list"])
rows = acts if isinstance(acts, list) else acts.get("activities") or acts.get("items")
row = [r for r in rows if r["id"] == aid][0]
assert row["net_cents"] == 160, row  # 30 - 28.40
run(["activity-archive", "--id", str(aid)])
try:
    run(["entry-activity", "--entry-id", str(eid), "--activity-id", str(aid)])
    raise AssertionError("归档活动不应再挂账单")
except ValueError as err:
    assert "归档" in str(err), err
run(["activity-archive", "--id", str(aid), "--restore"])
run(["entry-activity", "--entry-id", str(eid)])  # 摘除
conn = bk.connect()
assert conn.execute("select activity_id from entries where id=?", (eid,)).fetchone()["activity_id"] is None
conn.close()

# ---- 资产：建户、改名、设类型 ----
run(["account-add", "--name", "测试储蓄卡"])
run(["account-set", "测试储蓄卡", "--balance", "100", "--asset-type", "bank"])
ren = run(["account-rename", "--old", "测试储蓄卡", "--new", "招行测试卡"])
assert ren.get("ok") is True or "ok" in str(ren).lower(), ren
names = [r["name"] for r in bk.list_accounts()] if hasattr(bk, "list_accounts") else None
conn = bk.connect()
row = conn.execute("select name, asset_type from accounts where name='招行测试卡'").fetchone()
conn.close()
assert row is not None and row["asset_type"] == "bank", row

# ---- 删流水 ----
deleted = run(["entry-delete", str(eid)])
assert deleted.get("ok") is True or "ok" in str(deleted).lower(), deleted
conn = bk.connect()
assert conn.execute("select 1 from entries where id=?", (eid,)).fetchone() is None
conn.close()

print("test_cli_control: 全部通过")
