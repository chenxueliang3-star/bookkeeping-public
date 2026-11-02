# -*- coding: utf-8 -*-
"""按日净额、活动花费、活动归档。"""
import unittest

from conftest import use_empty_ledger
import bookkeeping as bk

# 本文件的流水全部落在 2026-04，查询按这个月过滤。
MONTH = "2026-04"


def setUpModule():
    use_empty_ledger()
    bk.set_account("现金", 0)


class DailyNetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 04-04：餐饮 300 + 旅行 200，同日退餐饮 100；04-05 退旅行 200（跨日归位）
        # 04-30：交通 500，05-02 退 500（跨月仍归 04-30）；04-06 未关联退款 80 不冲减
        food = bk.insert_entry({"transaction_time": f"{MONTH}-04 10:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 300, "account": "现金", "transaction_object": "净额面馆"})
        trip = bk.insert_entry({"transaction_time": f"{MONTH}-04 12:00:00", "type": "支出", "category": "旅行", "amount": 200, "account": "现金", "transaction_object": "净额出行"})
        ride = bk.insert_entry({"transaction_time": f"{MONTH}-30 20:00:00", "type": "支出", "category": "交通/打车", "amount": 500, "account": "现金", "transaction_object": "净额出租"})
        r1 = bk.insert_entry({"transaction_time": f"{MONTH}-04 18:00:00", "type": "退款", "category": "餐饮/早午晚餐", "amount": 100, "account": "现金", "transaction_object": "净额面馆"})
        r2 = bk.insert_entry({"transaction_time": f"{MONTH}-05 09:00:00", "type": "退款", "category": "旅行", "amount": 200, "account": "现金", "transaction_object": "净额出行"})
        r3 = bk.insert_entry({"transaction_time": "2026-05-02 09:00:00", "type": "退款", "category": "交通/打车", "amount": 500, "account": "现金", "transaction_object": "净额出租"})
        bk.insert_entry({"transaction_time": f"{MONTH}-06 09:00:00", "type": "退款", "category": "旅行", "amount": 80, "account": "现金", "transaction_object": "净额未关联"})
        bk.link_refund(r1["id"], food["id"])
        bk.link_refund(r2["id"], trip["id"])
        bk.link_refund(r3["id"], ride["id"])

    def test_refund_offsets_original_day(self):
        by_day, refund_by_category, _, repay = bk.net_expense_stats(bk.connect(), MONTH)
        self.assertEqual(repay, {})
        # 只核对自己造的两天。同月可能还有别的测试插入的流水，月合计不作死数。
        self.assertEqual(by_day[f"{MONTH}-04"], {"gross": 50000, "offset": 30000, "net": 20000})
        self.assertEqual(by_day[f"{MONTH}-30"], {"gross": 50000, "offset": 50000, "net": 0})
        self.assertNotIn(f"{MONTH}-05", by_day)
        self.assertGreaterEqual(refund_by_category["餐饮/早午晚餐"], 10000)
        self.assertGreaterEqual(refund_by_category["旅行"], 20000)
        self.assertGreaterEqual(refund_by_category["交通/打车"], 50000)

    def test_next_month_has_no_expense(self):
        by_day, _, _, _ = bk.net_expense_stats(bk.connect(), "2026-05")
        # 退款发生在 05-02，但原支出在 04 月，所以 05-02 本身没有支出。
        self.assertNotIn("2026-05-02", by_day)


class ActivityTest(unittest.TestCase):
    def test_spend_follows_refund_and_survives_delete(self):
        train = bk.insert_entry({"transaction_time": f"{MONTH}-10 10:00:00", "type": "支出", "category": "交通/火车高铁", "amount": 40, "account": "现金", "transaction_object": "活动高铁"})
        hotel = bk.insert_entry({"transaction_time": f"{MONTH}-10 12:00:00", "type": "支出", "category": "居家/住宿房租", "amount": 100, "account": "现金", "transaction_object": "活动酒店"})
        lunch = bk.insert_entry({"transaction_time": f"{MONTH}-11 09:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 25, "account": "现金", "transaction_object": "活动午餐"})
        refund = bk.insert_entry({"transaction_time": f"{MONTH}-12 09:00:00", "type": "退款", "category": "交通/火车高铁", "amount": 40, "account": "现金", "transaction_object": "活动高铁"})
        bk.link_refund(refund["id"], train["id"])

        act = bk.create_activity("示例出游", f"{MONTH}-10", f"{MONTH}-11")
        with self.assertRaises(ValueError):
            bk.create_activity("示例出游")

        bk.set_entries_activity([train["id"], hotel["id"], lunch["id"]], act["id"])
        row = self._row(act["id"])
        self.assertEqual(row["expense_count"], 3)
        self.assertEqual((row["gross_cents"], row["refund_cents"], row["net_cents"]), (16500, 4000, 12500))

        detail = bk.activity_entries(act["id"])
        self.assertEqual(len(detail["expenses"]), 3)
        self.assertEqual([item["id"] for item in detail["refunds"]], [refund["id"]])

        bk.set_entries_activity([lunch["id"]], None)
        row = self._row(act["id"])
        self.assertEqual((row["gross_cents"], row["net_cents"]), (14000, 10000))

        bk.update_activity(act["id"], {"name": "示例出游改名"})
        self.assertEqual(self._row(act["id"])["name"], "示例出游改名")

        bk.delete_activity(act["id"])
        conn = bk.connect()
        try:
            left = conn.execute(
                "select count(*) c from entries where id in (?,?,?) and activity_id is null",
                (train["id"], hotel["id"], lunch["id"]),
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(left, 3)
        self.assertFalse([item for item in bk.list_activities() if item["id"] == act["id"]])

        other = bk.create_activity("示例约会")["id"]
        bk.update_entry(train["id"], {"activity_id": str(other)})
        self.assertEqual(self._activity_of(train["id"]), other)
        bk.update_entry(train["id"], {"activity_id": ""})
        self.assertIsNone(self._activity_of(train["id"]))

        with self.assertRaises(ValueError):
            bk.set_entries_activity([train["id"]], 99999)

    def test_archive_blocks_new_entries(self):
        entry = bk.insert_entry({"transaction_time": f"{MONTH}-15 10:00:00", "type": "支出", "category": "餐饮/早午晚餐", "amount": 50, "account": "现金", "transaction_object": "归档面馆"})
        act = bk.create_activity("示例归档活动")
        self.assertEqual(self._row(act["id"])["archived"], 0)

        self.assertTrue(bk.set_activity_archived(act["id"], True)["archived"])
        with self.assertRaises(ValueError) as raised:
            bk.set_entries_activity([entry["id"]], act["id"])
        self.assertIn("归档", str(raised.exception))

        bk.set_activity_archived(act["id"], False)
        bk.set_entries_activity([entry["id"]], act["id"])
        bk.set_activity_archived(act["id"], True)
        self.assertEqual(len(bk.activity_entries(act["id"])["expenses"]), 1)
        self.assertEqual(self._activity_of(entry["id"]), act["id"])

        bk.set_activity_archived(act["id"], False)
        bk.set_entries_activity([entry["id"]], act["id"])

        with self.assertRaises(ValueError):
            bk.set_activity_archived(99999, True)

    def _row(self, activity_id):
        return [item for item in bk.list_activities() if item["id"] == activity_id][0]

    def _activity_of(self, entry_id):
        conn = bk.connect()
        try:
            return conn.execute("select activity_id from entries where id = ?", (entry_id,)).fetchone()["activity_id"]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
