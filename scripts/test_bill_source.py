# -*- coding: utf-8 -*-
"""流水来源 bill_source TDD 测试：临时库运行，断言核心行为。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_bill_source_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts"))
sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts/bookkeeping_app"))

import bookkeeping as bk  # noqa: E402

bk.init_db()


def table_columns(table):
    conn = bk.connect()
    try:
        return [row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()]
    finally:
        conn.close()


# ---- 1. 两表都有 bill_source 列 ----
assert "bill_source" in table_columns("entries"), "entries 缺 bill_source 列"
assert "bill_source" in table_columns("unconfirmed_entries"), "unconfirmed_entries 缺 bill_source 列"

# ---- 2. init_db 幂等：跑两次不报错、列不重复 ----
bk.init_db()
cols = table_columns("entries")
assert cols.count("bill_source") == 1, cols

# ---- 3. insert_entry 带 bill_source 写入 entries ----
eid = bk.insert_entry({
    "transaction_time": "2026-10-19 12:00:00",
    "type": "支出",
    "category": "餐饮/早午晚餐",
    "amount": 30.00,
    "account": "支付宝",
    "bill_source": "支付宝",
})["id"]
conn = bk.connect()
row = conn.execute("select bill_source from entries where id = ?", (eid,)).fetchone()
conn.close()
assert row["bill_source"] == "支付宝", row

# ---- 4. 不带 bill_source 写入为 NULL ----
eid2 = bk.insert_entry({
    "transaction_time": "2026-10-19 13:00:00",
    "type": "支出",
    "category": "餐饮/饮料",
    "amount": 15.00,
    "account": "微信",
})["id"]
conn = bk.connect()
row = conn.execute("select bill_source from entries where id = ?", (eid2,)).fetchone()
conn.close()
assert row["bill_source"] is None, row

# ---- 5. update_entry 可改 bill_source（含清空）----
bk.update_entry(eid2, {"bill_source": "微信"})
conn = bk.connect()
row = conn.execute("select bill_source from entries where id = ?", (eid2,)).fetchone()
conn.close()
assert row["bill_source"] == "微信", row

bk.update_entry(eid2, {"bill_source": ""})
conn = bk.connect()
row = conn.execute("select bill_source from entries where id = ?", (eid2,)).fetchone()
conn.close()
assert row["bill_source"] in (None, ""), row  # 空串存 NULL 或空均可

# ---- 6. 缺账户进 unconfirmed_entries 也保留 bill_source ----
res = bk.insert_entry({
    "transaction_time": "2026-10-19 14:00:00",
    "type": "支出",
    "amount": 20.00,
    "bill_source": "某银行App",
})
assert res["status"] == "unconfirmed", res
conn = bk.connect()
row = conn.execute("select bill_source, account from unconfirmed_entries where id = ?", (res["id"],)).fetchone()
conn.close()
assert row["bill_source"] == "某银行App", row

# ---- 7. 待确认转正后 bill_source 保留（补上账户一起确认）----
out = bk.confirm_entry(res["id"], "餐饮/早午晚餐", "支出", source="unconfirmed", account="某银行")
conn = bk.connect()
row = conn.execute("select bill_source from entries where transaction_time = ? and amount_cents = 2000", ("2026-10-19 14:00:00",)).fetchone()
conn.close()
assert row is not None and row["bill_source"] == "某银行App", row

# ---- 8. 标准 CSV 导入：source 列映射到 bill_source ----
import csv as _csv  # noqa: E402
csv_path = TMP + ".csv"
with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
    writer = _csv.writer(fh)
    writer.writerow(["transaction_time", "type", "category_level1", "category_level2", "category",
                     "amount", "currency", "transaction_object", "account", "target_account",
                     "participant", "note", "source"])
    writer.writerow(["2026-10-19 15:00:00", "支出", "餐饮", "早午晚餐", "餐饮/早午晚餐",
                     "25.00", "CNY", "测试商户", "支付宝", "", "自己", "", "支付宝"])
    writer.writerow(["2026-10-19 16:00:00", "支出", "餐饮", "饮料", "餐饮/饮料",
                     "18.00", "CNY", "测试商户2", "微信", "", "自己", "", ""])
counts = bk.import_file(csv_path)
assert counts["confirmed"] == 2, counts
conn = bk.connect()
rows = conn.execute("select bill_source from entries where transaction_time in ('2026-10-19 15:00:00','2026-10-19 16:00:00') order by transaction_time").fetchall()
conn.close()
assert rows[0]["bill_source"] == "支付宝", rows[0]
assert rows[1]["bill_source"] in (None, ""), rows[1]

print("test_bill_source: ALL PASS")
