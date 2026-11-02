# -*- coding: utf-8 -*-
"""退款自动关联，以及手动关联时的候选排序。"""
import unittest

from conftest import use_empty_ledger
import bookkeeping as bk


def setUpModule():
    use_empty_ledger()
    bk.set_account("现金", 0)
    bk.set_account("零钱", 0)


def refund(time="2026-05-10 12:00:00", amt=3000, acc="零钱", obj="示例充电站", note=""):
    return {"transaction_time": time, "amount_cents": amt, "account": acc, "transaction_object": obj, "note": note}


def expense(time="2026-05-10 10:00:00", amt=3000, acc="零钱", obj="示例充电站", note=""):
    return {"transaction_time": time, "amount_cents": amt, "account": acc, "transaction_object": obj, "note": note}


class EvidenceTest(unittest.TestCase):
    def test_same_amount_merchant_account(self):
        ev = bk.refund_evidence(refund(), expense())
        self.assertIsNotNone(ev)
        self.assertTrue(ev["identity"])
        self.assertGreaterEqual(ev["score"], 8)

    def test_refund_larger_than_expense_is_rejected(self):
        self.assertIsNone(bk.refund_evidence(refund(amt=5000), expense(amt=3000)))

    def test_pay_later_allows_only_same_amount_within_a_day(self):
        ev = bk.refund_evidence(refund(time="2026-05-10 00:05:00", amt=3000), expense(time="2026-05-10 00:09:00", amt=3000))
        self.assertIsNotNone(ev)
        self.assertTrue(ev["identity"])
        self.assertIsNone(bk.refund_evidence(refund(time="2026-05-10 00:05:00"), expense(time="2026-05-10 00:09:00", amt=3800)))
        self.assertIsNone(bk.refund_evidence(refund(time="2026-05-10 00:05:00"), expense(time="2026-05-11 01:00:00")))

    def test_transfer_refund_only_matches_transfer(self):
        self.assertIsNone(bk.refund_evidence(
            refund(obj=None, note="微信交易类型:转账-退款"),
            expense(obj="示例朋友", note="微信交易类型:商户消费"),
        ))
        ev = bk.refund_evidence(
            refund(obj=None, note="微信交易类型:转账-退款", amt=4000),
            expense(obj="示例朋友", note="转账备注:微信转账；微信交易类型:转账", amt=4000),
        )
        self.assertIsNotNone(ev)
        self.assertGreaterEqual(ev["score"], 5.5)

    def test_merchant_suffix_in_brackets_still_matches(self):
        self.assertIsNotNone(bk.refund_evidence(refund(obj="示例外卖"), expense(obj="示例外卖App")))


class AutoLinkTest(unittest.TestCase):
    def test_unique_candidate_links_and_inherits_category(self):
        expense_id = bk.insert_entry({"transaction_time": "2026-05-10 10:00:00", "type": "支出", "category": "居家/快递费", "amount": 30, "account": "零钱", "transaction_object": "示例驿站"})["id"]
        refund_id = bk.insert_entry({"transaction_time": "2026-05-10 12:00:00", "type": "退款", "amount": 28, "account": "零钱", "transaction_object": "示例驿站"})["id"]
        report = bk.auto_link_refunds(bk.connect())
        self.assertIn(refund_id, [item["refund_id"] for item in report["auto"]])
        row = self._row(refund_id)
        self.assertEqual(row["refund_pair_id"], expense_id)
        self.assertEqual(row["category"], "居家/快递费")

    def test_two_identical_expenses_stay_manual(self):
        bk.insert_entry({"transaction_time": "2026-05-11 10:00:00", "type": "支出", "category": "居家/快递费", "amount": 30, "account": "零钱", "transaction_object": "示例驿站甲"})
        bk.insert_entry({"transaction_time": "2026-05-11 10:30:00", "type": "支出", "category": "居家/快递费", "amount": 30, "account": "零钱", "transaction_object": "示例驿站甲"})
        refund_id = bk.insert_entry({"transaction_time": "2026-05-11 12:00:00", "type": "退款", "amount": 30, "account": "零钱", "transaction_object": "示例驿站甲"})["id"]
        report = bk.auto_link_refunds(bk.connect())
        self.assertTrue([item for item in report["manual"] if item["refund_id"] == refund_id])

    def test_refund_without_expense_is_not_linked(self):
        refund_id = bk.insert_entry({"transaction_time": "2026-05-12 12:00:00", "type": "退款", "amount": 19, "account": "零钱", "transaction_object": "示例商城"})["id"]
        report = bk.auto_link_refunds(bk.connect())
        self.assertTrue([item for item in report["no_candidate"] if item["refund_id"] == refund_id])
        self.assertIsNone(self._row(refund_id)["refund_pair_id"])

    def test_pending_refund_is_confirmed_after_link(self):
        bk.insert_entry({"transaction_time": "2026-05-13 10:00:00", "type": "支出", "category": "交通/电动车充电", "amount": 30, "account": "零钱", "transaction_object": "示例充电宝"})
        refund_id = bk.insert_entry({"transaction_time": "2026-05-13 11:00:00", "type": "退款", "amount": 30, "account": "零钱", "transaction_object": "示例充电宝"})["id"]
        self.assertEqual(self._row(refund_id)["status"], "pending")
        report = bk.auto_link_refunds(bk.connect())
        self.assertTrue([item for item in report["auto"] if item["refund_id"] == refund_id])
        row = self._row(refund_id)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["category"], "交通/电动车充电")
        self.assertIsNotNone(row["refund_pair_id"])

    def test_fully_refunded_expense_takes_no_more(self):
        bk.insert_entry({"transaction_time": "2026-05-14 10:00:00", "type": "支出", "category": "居家/快递费", "amount": 30, "account": "零钱", "transaction_object": "示例驿站乙"})
        first = bk.insert_entry({"transaction_time": "2026-05-14 11:00:00", "type": "退款", "amount": 30, "account": "零钱", "transaction_object": "示例驿站乙"})["id"]
        second = bk.insert_entry({"transaction_time": "2026-05-14 12:00:00", "type": "退款", "amount": 30, "account": "零钱", "transaction_object": "示例驿站乙"})["id"]
        report = bk.auto_link_refunds(bk.connect())
        linked = [item["refund_id"] for item in report["auto"]]
        self.assertIn(first, linked)
        self.assertNotIn(second, linked)

    def _row(self, entry_id):
        conn = bk.connect()
        try:
            return dict(conn.execute("select * from entries where id = ?", (entry_id,)).fetchone())
        finally:
            conn.close()


class CandidateTest(unittest.TestCase):
    def test_only_earlier_expenses_and_best_match_first(self):
        bk.insert_entry({"transaction_time": "2026-05-20 21:00:00", "type": "支出", "category": "网球/场地费", "amount": 40, "account": "现金", "transaction_object": "", "status": "confirmed"})
        bk.insert_entry({"transaction_time": "2026-05-22 12:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 25, "account": "零钱", "transaction_object": "示例快餐", "status": "confirmed"})
        bk.insert_entry({"transaction_time": "2026-05-19 12:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 25, "account": "零钱", "transaction_object": "示例快餐", "status": "confirmed"})
        bk.insert_entry({"transaction_time": "2026-05-21 21:00:00", "type": "退款", "category": "网球/场地费", "amount": 40, "account": "现金", "transaction_object": "", "status": "confirmed"})

        data = bk.refund_candidates(merchant="", ttime="2026-05-21 21:00:00", amount=4000, account="现金")
        shown = data["matches"] + data["recent"]
        ids = [item["id"] for item in shown]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(data["matches"])
        top = data["matches"][0]
        self.assertEqual(top["amount_cents"], 4000)
        self.assertIn("金额相同", top["tags"])
        self.assertIn("同账户", top["tags"])
        self.assertTrue(all(item["transaction_time"] < "2026-05-21 21:00:00" for item in shown))
        self.assertTrue(any(item["amount_cents"] == 2500 and item["transaction_time"] == "2026-05-19 12:00:00" for item in data["recent"]))

    def test_triple_match_ranks_first(self):
        bk.insert_entry({"transaction_time": "2026-05-23 12:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 25, "account": "零钱", "transaction_object": "示例快餐甲", "status": "confirmed"})
        data = bk.refund_candidates(merchant="示例快餐甲", ttime="2026-05-23 13:00:00", amount=2500, account="零钱")
        top = data["matches"][0]
        self.assertEqual(top["tags"], ["金额相同", "同商户", "同账户"])
        self.assertTrue(all(item["score"] <= top["score"] for item in data["matches"][1:]))


if __name__ == "__main__":
    unittest.main()
