# -*- coding: utf-8 -*-
"""退款候选测试：只列退款之前的支出，按匹配度排序（金额相同 > 同商户 > 同账户）。"""
import os, sys, tempfile, json

TMP = os.path.join(tempfile.gettempdir(), "tmp_refund_amount_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookkeeping_app"))
import bookkeeping as bk

bk.init_db()

# 原支出1：同金额、交易对象为空（退款前2天）
bk.insert_entry({"transaction_time": "2026-09-08 21:00:00", "type": "支出", "category": "网娱-场地费",
                 "amount": 40.00, "account": "农业银行储蓄卡(7270)", "transaction_object": "", "note": "", "status": "confirmed"})
# 原支出2：不同金额、肯德基（在退款之后，应被时间过滤排除）
bk.insert_entry({"transaction_time": "2026-09-10 12:00:00", "type": "支出", "category": "餐饮",
                 "amount": 25.00, "account": "零钱", "transaction_object": "肯德基", "note": "", "status": "confirmed"})
# 原支出3：不同金额、肯德基（退款前，进 recent 兜底）
bk.insert_entry({"transaction_time": "2026-09-07 12:00:00", "type": "支出", "category": "餐饮",
                 "amount": 25.00, "account": "零钱", "transaction_object": "肯德基", "note": "", "status": "confirmed"})
# 退款：交易对象为空
bk.insert_entry({"transaction_time": "2026-09-09 21:52:00", "type": "退款", "category": "网娱-场地费",
                 "amount": 40.00, "account": "农业银行储蓄卡(7270)", "transaction_object": "", "note": "", "status": "confirmed"})

conn = bk.connect()
refund = conn.execute("select id, amount_cents, transaction_time, account from entries where type='退款'").fetchone()
conn.close()

data = bk.refund_candidates(merchant="", ttime=refund["transaction_time"], amount=refund["amount_cents"], account=refund["account"])
ids_in_result = [c["id"] for c in data["matches"] + data["recent"]]
assert len(ids_in_result) == len(set(ids_in_result)), "同一条候选不应重复出现"

# 同金额支出应以「金额相同+同账户」身份排第一
assert data["matches"], f"应有匹配候选: {json.dumps(data, ensure_ascii=False)}"
top = data["matches"][0]
assert top["amount_cents"] == 4000, f"金额错误: {top['amount_cents']}"
assert "金额相同" in top["tags"] and "同账户" in top["tags"], f"标签错误: {top['tags']}"
assert top["transaction_time"] < refund["transaction_time"], "候选必须早于退款时间"

# 退款之后的支出（09-10 肯德基）不得出现在任何候选里
assert all(c["transaction_time"] < refund["transaction_time"] for c in data["matches"] + data["recent"]), "存在晚于退款时间的候选"

# recent 兜底只含退款前且无命中的支出（09-07 肯德基 25 元）
assert any(r["amount_cents"] == 2500 and r["transaction_time"] == "2026-09-07 12:00:00" for r in data["recent"]), f"recent 兜底缺失: {json.dumps(data['recent'], ensure_ascii=False)}"

# 同商户+同金额+同账户三重命中应排最前
bk.insert_entry({"transaction_time": "2026-09-10 12:00:00", "type": "支出", "category": "餐饮",
                 "amount": 25.00, "account": "零钱", "transaction_object": "肯德基", "note": "", "status": "confirmed"})
data2 = bk.refund_candidates(merchant="肯德基", ttime="2026-09-10 13:00:00", amount=2500, account="零钱")
assert data2["matches"], "应有匹配候选"
top2 = data2["matches"][0]
assert top2["tags"] == ["金额相同", "同商户", "同账户"], f"三重命中标签错误: {top2['tags']}"
assert top2["transaction_time"] == "2026-09-10 12:00:00", "三重命中应排最前"
assert all(c["score"] <= top2["score"] for c in data2["matches"][1:]), "排序错误：存在分数更高的候选排在后面"

print("PASS: refund candidates OK (时间过滤 + 匹配度排序)")
