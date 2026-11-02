# -*- coding: utf-8 -*-
"""日常改账走命令行：改删流水、退款关联、活动、账户。"""
import io
import json
import unittest
from contextlib import redirect_stdout

from conftest import use_empty_ledger
import bookkeeping as bk


def setUpModule():
    use_empty_ledger()
    bk.set_account("现金", 0)


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        bk.main(argv)
    text = buf.getvalue().strip()
    return json.loads(text) if text.startswith("{") or text.startswith("[") else text


class CliControlTest(unittest.TestCase):
    def test_edit_refund_activity_and_account(self):
        added = run(["add", "--type", "支出", "--amount", "30", "--account", "现金",
                     "--category", "交通/电动车充电", "--transaction-object", "命令充电",
                     "--time", "2026-09-10 10:00:00"])
        entry_id = added["id"]
        run(["entry-update", str(entry_id), "--note", "含押金"])
        self.assertEqual(self._value("select note from entries where id = ?", entry_id), "含押金")

        refund_id = run(["add", "--type", "退款", "--amount", "20", "--account", "现金",
                         "--transaction-object", "命令充电", "--time", "2026-09-10 12:00:00"])["id"]
        linked = run(["refund-link", "--refund-id", str(refund_id), "--expense-id", str(entry_id)])
        self.assertTrue(linked["ok"])
        self.assertEqual(linked["expense_id"], entry_id)
        self.assertEqual(self._value("select refund_pair_id from entries where id = ?", refund_id), entry_id)

        candidates = run(["refund-candidates", "--merchant", "命令充电"])
        self.assertIsInstance(candidates, (dict, list))
        self.assertTrue(run(["refund-unlink", "--refund-id", str(refund_id)])["ok"])
        self.assertIsNone(self._value("select refund_pair_id from entries where id = ?", refund_id))
        run(["refund-link", "--refund-id", str(refund_id), "--expense-id", str(entry_id)])

        activity_id = run(["activity-add", "--name", "命令测试活动"])["id"]
        hung = run(["entry-activity", "--entry-id", str(entry_id), "--activity-id", str(activity_id)])
        self.assertTrue(hung["ok"])
        self.assertEqual(hung["count"], 1)
        listed = run(["activity-list"])
        rows = listed if isinstance(listed, list) else listed.get("activities") or listed.get("items")
        row = [item for item in rows if item["id"] == activity_id][0]
        self.assertEqual(row["net_cents"], 1000)

        run(["activity-archive", "--id", str(activity_id)])
        with self.assertRaises(ValueError) as raised:
            run(["entry-activity", "--entry-id", str(entry_id), "--activity-id", str(activity_id)])
        self.assertIn("归档", str(raised.exception))
        run(["activity-archive", "--id", str(activity_id), "--restore"])
        run(["entry-activity", "--entry-id", str(entry_id)])
        self.assertIsNone(self._value("select activity_id from entries where id = ?", entry_id))

        run(["account-add", "--name", "命令储蓄卡"])
        run(["account-set", "命令储蓄卡", "--balance", "100", "--asset-type", "bank"])
        renamed = run(["account-rename", "--old", "命令储蓄卡", "--new", "命令工资卡"])
        self.assertTrue(renamed.get("ok"))
        conn = bk.connect()
        try:
            row = conn.execute("select asset_type from accounts where name = '命令工资卡'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["asset_type"], "bank")

        self.assertTrue(run(["entry-delete", str(entry_id)]).get("ok"))
        self.assertIsNone(self._value("select id from entries where id = ?", entry_id))

    def _value(self, sql, entry_id):
        conn = bk.connect()
        try:
            row = conn.execute(sql, (entry_id,)).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
