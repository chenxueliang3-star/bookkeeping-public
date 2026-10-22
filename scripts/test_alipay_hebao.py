# -*- coding: utf-8 -*-
"""TDD：支付宝小荷包消费（收/支=不计收支）应入账为支出，划转类仍跳过。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_alipay_hebao_test.db")
os.path.exists(TMP) and os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookkeeping_app"))

import bookkeeping as bk  # noqa: E402
import import_alipay_bill as ia  # noqa: E402

bk.init_db()

MAP = {"支付宝小荷包(?约会经费)": "支付宝小荷包(?约会经费)"}

# 场景1：小荷包付款的真实消费（不计收支 + 消费类交易分类 + 交易成功）
row_expense = {
    "交易时间": "2026-10-18 12:51:34",
    "交易分类": "餐饮美食",
    "交易对方": "淘宝闪购",
    "对方账号": "e50***@alibaba-inc.com",
    "商品说明": "麦当劳·巨无霸烤肉(黄河广场店)",
    "收/支": "不计收支",
    "金额": "34.30",
    "收/付款方式": "支付宝小荷包(?约会经费)",
    "交易状态": "交易成功",
    "交易订单号": "x1",
    "商家订单号": "y1",
    "备注": "",
}
record, reason = ia.classify(row_expense, MAP)
assert record is not None, f"小荷包消费被跳过: {reason}"
assert record["type"] == "支出", record["type"]
assert record["account"] == "支付宝小荷包(?约会经费)", record["account"]
assert record["amount"] == "34.30"
print("PASS 1: 小荷包消费 → 支出（账户=小荷包）")

# 场景2：真实的内部划转（交易分类=转账）仍跳过
row_transfer = dict(row_expense, **{"交易分类": "转账", "交易对方": "小荷包", "商品说明": "转入小荷包"})
record, reason = ia.classify(row_transfer, MAP)
assert record is None, f"划转误入账: {record}"
print("PASS 2: 划转（转账类）仍跳过")

# 场景3：余额宝自动转入仍跳过
row_yeb = dict(row_expense, **{"交易分类": "投资理财", "交易对方": "余额宝", "商品说明": "自动转入", "收/付款方式": "农业银行储蓄卡(5072)"})
record, reason = ia.classify(row_yeb, MAP)
assert record is None
print("PASS 3: 余额宝转入仍跳过")

# 场景4：无法识别的不计收支仍跳过（保守不丢未知数据进错类）
row_unknown = dict(row_expense, **{"交易分类": "其他", "收/付款方式": "某种未知渠道"})
record, reason = ia.classify(row_unknown, MAP)
assert record is None, f"未知渠道误入账: {record}"
print("PASS 4: 未知渠道不计收支仍跳过")

# 场景5：端到端——清洗后的记录可入库且统计正确
TMP2 = os.path.join(tempfile.gettempdir(), "tmp_alipay_hebao_test2.db")
os.path.exists(TMP2) and os.remove(TMP2)
os.environ["BOOKKEEPING_DB_PATH"] = TMP2
bk.init_db()
rows = [row_expense]
records, skipped = ia.classify_rows(rows, MAP)
assert len(records) == 1
clean = records[0][1]
eid = bk.insert_entry({
    "transaction_time": clean["transaction_time"],
    "type": clean["type"],
    "category": "餐饮/早午晚餐",
    "amount": float(clean["amount"]),
    "account": clean["account"],
    "transaction_object": clean["transaction_object"],
    "note": clean["note"],
    "status": "confirmed",
})["id"]
summary = bk.api_summary()
assert float(summary["expense"]) > 0
print(f"PASS 5: 入库后支出统计 = {summary['expense']} 元（entry id {eid}）")

# 场景6：花呗主动还款（不计收支 + 信用借还 + 储蓄卡付款）应入账为还款，不再当未知划转丢弃
row_huabei = {
    "交易时间": "2026-10-01 23:34:33",
    "交易分类": "信用借还",
    "交易对方": "花呗",
    "对方账号": "/",
    "商品说明": "花呗主动还款-2026年10月账单",
    "收/支": "不计收支",
    "金额": "144.42",
    "收/付款方式": "农业银行储蓄卡(5072)",
    "交易状态": "还款成功",
    "交易订单号": "x2",
    "商家订单号": "",
    "备注": "",
}
record, reason = ia.classify(row_huabei, MAP)
assert record is not None, f"花呗还款被跳过: {reason}"
assert record["type"] == "还款", record["type"]
assert record["category"] == "花呗", record["category"]
assert record["account"] == "农业银行储蓄卡(5072)", record["account"]
print("PASS 6: 花呗还款（不计收支+信用借还）→ 还款/花呗")

print("\nALL PASS")
