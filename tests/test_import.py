# -*- coding: utf-8 -*-
"""支付宝清洗、微信清洗、流水来源从导入到确认都保留。"""
import csv
import os
import unittest

from conftest import use_empty_ledger
import bookkeeping as bk
import import_alipay_bill as alipay
import import_wechat_bill as wechat
from fixtures import alipay_row, wechat_row

ACCOUNT_MAP = {"小荷包": "小荷包"}


def setUpModule():
    use_empty_ledger()
    bk.set_account("现金", 0)
    bk.set_account("工资卡", 0)


class AlipayTest(unittest.TestCase):
    def test_pocket_spend_is_expense(self):
        record, reason = alipay.classify(alipay_row(), ACCOUNT_MAP)
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["type"], "支出")
        self.assertEqual(record["account"], "小荷包")
        self.assertEqual(record["amount"], "30.00")

    def test_internal_moves_are_skipped(self):
        transfer = alipay_row(**{"交易分类": "转账", "交易对方": "小荷包", "商品说明": "转入小荷包"})
        record, _ = alipay.classify(transfer, ACCOUNT_MAP)
        self.assertIsNone(record)

        yuebao = alipay_row(**{"交易分类": "投资理财", "交易对方": "余额宝", "商品说明": "自动转入", "收/付款方式": "工资卡"})
        record, _ = alipay.classify(yuebao, ACCOUNT_MAP)
        self.assertIsNone(record)

        unknown = alipay_row(**{"交易分类": "其他", "收/付款方式": "未知渠道"})
        record, _ = alipay.classify(unknown, ACCOUNT_MAP)
        self.assertIsNone(record)

    def test_credit_repay_is_repayment(self):
        row = alipay_row(**{
            "交易分类": "信用借还",
            "交易对方": "花呗",
            "商品说明": "信用卡主动还款",
            "收/支": "不计收支",
            "金额": "100.00",
            "收/付款方式": "工资卡",
            "交易状态": "还款成功",
        })
        record, reason = alipay.classify(row, ACCOUNT_MAP)
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["type"], "还款")
        self.assertEqual(record["category"], "花呗")
        self.assertEqual(record["account"], "工资卡")

    def test_cleaned_row_can_be_inserted(self):
        records, _ = alipay.classify_rows([alipay_row()], ACCOUNT_MAP)
        self.assertEqual(len(records), 1)
        clean = records[0][1]
        bk.set_account("小荷包", 0)
        bk.insert_entry({
            "transaction_time": clean["transaction_time"],
            "type": clean["type"],
            "category": "餐饮/早午晚餐",
            "amount": float(clean["amount"]),
            "account": clean["account"],
            "transaction_object": clean["transaction_object"],
            "note": clean["note"],
            "status": "confirmed",
        })


class WechatTest(unittest.TestCase):
    def test_topups_are_transfers(self):
        record, reason = wechat.classify(wechat_row(), {})
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["type"], "转账")
        self.assertEqual(record["category"], "转账")
        self.assertEqual(record["account"], "工资卡")
        self.assertEqual(record["target_account"], "零钱")
        self.assertEqual(record["source"], "微信")

        record, reason = wechat.classify(wechat_row(**{
            "交易类型": "转入零钱通-来自工资卡",
            "交易对方": "",
            "金额(元)": "200.00",
            "当前状态": "支付成功",
        }), {})
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["type"], "转账")
        self.assertEqual(record["account"], "工资卡")
        self.assertEqual(record["target_account"], "零钱通")

    def test_merchant_spend_stays_expense(self):
        record, reason = wechat.classify(wechat_row(**{
            "交易类型": "商户消费",
            "交易对方": "示例便利店",
            "收/支": "支出",
            "金额(元)": "3.50",
            "当前状态": "支付成功",
        }), {})
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["type"], "支出")
        self.assertIn(record["target_account"], (None, ""))


class BillSourceTest(unittest.TestCase):
    def test_column_exists_once(self):
        bk.init_db()
        for table in ("entries", "unconfirmed_entries"):
            cols = self._columns(table)
            self.assertEqual(cols.count("bill_source"), 1)

    def test_source_survives_insert_update_and_confirm(self):
        entry_id = bk.insert_entry({"transaction_time": "2026-06-01 12:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 30, "account": "现金", "bill_source": "支付宝", "transaction_object": "来源面馆"})["id"]
        self.assertEqual(self._source("entries", entry_id), "支付宝")

        bare = bk.insert_entry({"transaction_time": "2026-06-01 13:00:00", "type": "支出", "category": "餐饮/饮料", "amount": 15, "account": "现金", "transaction_object": "来源饮料"})["id"]
        self.assertIsNone(self._source("entries", bare))
        bk.update_entry(bare, {"bill_source": "微信"})
        self.assertEqual(self._source("entries", bare), "微信")
        bk.update_entry(bare, {"bill_source": ""})
        self.assertIn(self._source("entries", bare), (None, ""))

        pending = bk.insert_entry({"transaction_time": "2026-06-01 14:00:00", "type": "支出", "amount": 20, "bill_source": "示例银行App", "transaction_object": "来源待确认"})
        self.assertEqual(pending["status"], "unconfirmed")
        self.assertEqual(self._source("unconfirmed_entries", pending["id"]), "示例银行App")
        bk.confirm_entry(pending["id"], "餐饮/早午晚餐", "支出", source="unconfirmed", account="现金")
        conn = bk.connect()
        try:
            row = conn.execute(
                "select bill_source from entries where transaction_object = ?", ("来源待确认",)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["bill_source"], "示例银行App")

    def test_standard_csv_source_maps_to_bill_source(self):
        path = os.path.join(os.path.dirname(bk.DB_PATH), "sample.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["transaction_time", "type", "category_level1", "category_level2", "category", "amount", "currency", "transaction_object", "account", "target_account", "participant", "note", "source"])
            writer.writerow(["2026-06-02 15:00:00", "支出", "餐饮", "早午晚餐", "餐饮/早午晚餐", "25.00", "CNY", "导入商户甲", "现金", "", "自己", "", "支付宝"])
            writer.writerow(["2026-06-02 16:00:00", "支出", "餐饮", "饮料", "餐饮/饮料", "18.00", "CNY", "导入商户乙", "现金", "", "自己", "", ""])
        counts = bk.import_file(path)
        self.assertEqual(counts["confirmed"], 2)
        conn = bk.connect()
        try:
            rows = conn.execute(
                "select transaction_object, bill_source from entries where transaction_object in ('导入商户甲','导入商户乙') order by transaction_time"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows[0]["bill_source"], "支付宝")
        self.assertIn(rows[1]["bill_source"], (None, ""))

    def _columns(self, table):
        conn = bk.connect()
        try:
            return [row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()]
        finally:
            conn.close()

    def _source(self, table, entry_id):
        conn = bk.connect()
        try:
            return conn.execute(f"select bill_source from {table} where id = ?", (entry_id,)).fetchone()["bill_source"]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
