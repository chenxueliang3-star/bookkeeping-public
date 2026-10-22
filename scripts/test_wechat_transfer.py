# -*- coding: utf-8 -*-
"""微信适配脚本内部划转 TDD：零钱充值/转入零钱通 应建模为转账。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy/skills/bookkeeping/scripts"))

from import_wechat_bill import classify  # noqa: E402


def row(**overrides):
    base = {
        "交易时间": "2026-07-21 00:49:19",
        "交易类型": "零钱充值",
        "交易对方": "农业银行(7270)",
        "商品": "",
        "收/支": "",
        "金额(元)": "1000",
        "支付方式": "农业银行储蓄卡(7270)",
        "当前状态": "充值完成",
    }
    base.update(overrides)
    return base


# ---- 1. 零钱充值 → 转账（卡→零钱）----
record, skip = classify(row(), {})
assert record is not None, skip
assert record["type"] == "转账", record
assert record["category"] == "转账", record
assert record["account"] == "农业银行储蓄卡(7270)", record
assert record["target_account"] == "零钱", record
assert record["source"] == "微信", record

# ---- 2. 转入零钱通 → 转账（卡→零钱通）----
record, skip = classify(row(**{
    "交易时间": "2026-07-22 11:20:00",
    "交易类型": "转入零钱通-来自农业银行(7270)",
    "交易对方": "",
    "金额(元)": "7000",
    "支付方式": "农业银行储蓄卡(7270)",
    "当前状态": "支付成功",
}), {})
assert record is not None, skip
assert record["type"] == "转账", record
assert record["account"] == "农业银行储蓄卡(7270)", record
assert record["target_account"] == "零钱通", record

# ---- 3. 正常支出不受影响 ----
record, skip = classify(row(**{
    "交易时间": "2026-07-23 10:00:00",
    "交易类型": "商户消费",
    "交易对方": "美宜佳便利店",
    "收/支": "支出",
    "金额(元)": "3.5",
    "当前状态": "支付成功",
}), {})
assert record is not None, skip
assert record["type"] == "支出", record
assert record["target_account"] in (None, ""), record

print("test_wechat_transfer: ALL PASS")
