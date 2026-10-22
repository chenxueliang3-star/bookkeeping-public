# -*- coding: utf-8 -*-
"""退款自动关联测试：证据函数单测 + 临时库集成。运行：python test_refund_auto_link.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bookkeeping as bk

# ---------- 证据函数单测 ----------

def R(time="2026-10-10 12:00:00", amt=2840, acc="零钱", obj="智电驿站", note=""):
    return {"transaction_time": time, "amount_cents": amt, "account": acc, "transaction_object": obj, "note": note}

def E(time="2026-10-10 10:00:00", amt=3000, acc="零钱", obj="智电驿站", note=""):
    return {"transaction_time": time, "amount_cents": amt, "account": acc, "transaction_object": obj, "note": note}

ev = bk.refund_evidence(R(amt=3000), E())
assert ev is not None and ev["identity"], ev
assert ev["score"] >= 8, ev  # 金额相同4.5+同商户2+同账户1+2h内1 = 8.5

# 退款 > 支出 → 硬排除
assert bk.refund_evidence(R(amt=5000), E(amt=3000)) is None

# 先用后付：支出在退款后 4 分钟 + 金额相同 + 同商户 → 允许
ev = bk.refund_evidence(R(time="2026-09-10 00:05:00"), E(time="2026-09-10 00:09:00", amt=2840))
assert ev is not None and ev["identity"] and ev["score"] > 0, ev
# 先用后付但金额不同 → 排除
assert bk.refund_evidence(R(time="2026-09-10 00:05:00"), E(time="2026-09-10 00:09:00", amt=3810)) is None
# 支出在退款后超过 24h → 排除
assert bk.refund_evidence(R(time="2026-09-10 00:05:00"), E(time="2026-09-11 01:00:00")) is None

# 转账退款只能源自转账支出
assert bk.refund_evidence(R(obj=None, note="微信交易类型:转账-退款"), E(obj="西西", note="微信交易类型:商户消费")) is None
ev = bk.refund_evidence(R(obj=None, note="微信交易类型:转账-退款", amt=4000), E(obj="西西", note="转账备注:微信转账；微信交易类型:转账", amt=4000))
assert ev is not None, "转账对转账应允许"
assert ev["score"] >= 5.5, ev  # 金额相同4.5+同账户1+24h内1+转账1（对象为空0）

# 商户归一化：去括号后缀
ev = bk.refund_evidence(R(obj="美团"), E(obj="美团外卖App"))
assert ev is not None, ev

# ---------- 集成测试（临时库） ----------

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["BOOKKEEPING_DB_PATH"] = tmp
bk.set_database_path(tmp)
bk.init_db()

# 场景A：唯一合理候选 → 自动关联 + 分类继承
bk.insert_entry({"transaction_time": "2026-10-10 10:00:00", "type": "支出", "category": "居家/快递费",
                 "amount": 30.00, "account": "零钱", "transaction_object": "智电驿站"})
r_a = bk.insert_entry({"transaction_time": "2026-10-10 12:00:00", "type": "退款", "amount": 28.40,
                       "account": "零钱", "transaction_object": "智电驿站"})
rep = bk.auto_link_refunds(bk.connect())
assert [x["refund_id"] for x in rep["auto"]] == [r_a["id"]], rep
conn = bk.connect()
row = conn.execute("select refund_pair_id, category, status from entries where id = ?", (r_a["id"],)).fetchone()
assert row["refund_pair_id"] is not None, row
assert row["category"] == "居家/快递费", row
conn.close()

# 场景B：两笔一模一样的支出 → 歧义留人工
bk.insert_entry({"transaction_time": "2026-10-11 10:00:00", "type": "支出", "category": "居家/快递费",
                 "amount": 30.00, "account": "零钱", "transaction_object": "智电驿站"})
r_b = bk.insert_entry({"transaction_time": "2026-10-11 12:00:00", "type": "退款", "amount": 30.00,
                       "account": "零钱", "transaction_object": "智电驿站"})
rep = bk.auto_link_refunds(bk.connect())
b_entry = [x for x in rep["manual"] if x["refund_id"] == r_b["id"]]
assert b_entry, f"歧义场景应留人工: {rep}"

# 场景C：流水里只有退款没有扣款 → no_candidate，绝不自动连
r_c = bk.insert_entry({"transaction_time": "2026-10-12 12:00:00", "type": "退款", "amount": 19.90,
                       "account": "零钱", "transaction_object": "拼多多平台商户"})
rep = bk.auto_link_refunds(bk.connect())
assert any(x["refund_id"] == r_c["id"] for x in rep["no_candidate"]), rep
conn = bk.connect()
row = conn.execute("select refund_pair_id from entries where id = ?", (r_c["id"],)).fetchone()
assert row["refund_pair_id"] is None, row
conn.close()

# 场景D：pending 退款（无分类）自动连后转正（用独特商户避免与前面场景形成等价竞争）
bk.insert_entry({"transaction_time": "2026-10-13 10:00:00", "type": "支出", "category": "交通/加油充电",
                 "amount": 30.00, "account": "零钱", "transaction_object": "美团充电宝"})
r_d = bk.insert_entry({"transaction_time": "2026-10-13 11:00:00", "type": "退款", "amount": 30.00,
                       "account": "零钱", "transaction_object": "美团充电宝"})
conn = bk.connect()
st = conn.execute("select status from entries where id = ?", (r_d["id"],)).fetchone()["status"]
conn.close()
assert st == "pending", st  # 无分类先 pending
rep = bk.auto_link_refunds(bk.connect())
assert any(x["refund_id"] == r_d["id"] for x in rep["auto"]), rep
conn = bk.connect()
row = conn.execute("select status, category, refund_pair_id from entries where id = ?", (r_d["id"],)).fetchone()
assert row["status"] == "confirmed" and row["category"] == "交通/加油充电" and row["refund_pair_id"], row
conn.close()

# 场景E：已全额退款的支出不再接收新退款
r_e = bk.insert_entry({"transaction_time": "2026-10-13 12:00:00", "type": "退款", "amount": 30.00,
                       "account": "零钱", "transaction_object": "智电驿站"})
rep = bk.auto_link_refunds(bk.connect())
assert not any(x.get("expense_id") for x in rep["auto"] if x["refund_id"] == r_e["id"]), rep

print("test_refund_auto_link: 全部通过")
