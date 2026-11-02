# -*- coding: utf-8 -*-
"""债权负债、负债三型、资产分组和净资产。"""
import unittest

from conftest import account_row, balances, use_empty_ledger
import bookkeeping as bk
import debt_accounts as da
import investments


def setUpModule():
    use_empty_ledger()
    bk.set_account("现金", 0)


class DebtArchiveTest(unittest.TestCase):
    def test_kind_balance_archive_and_correction(self):
        self.assertIn("archived", self._columns())
        bk.set_account("花呗", -500, kind="liability", note="示例额度")
        bk.set_account("借款-甲", 0, kind="receivable")
        row = account_row("花呗")
        self.assertEqual(row["kind"], "liability")
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["current_balance_cents"], -50000)
        self.assertEqual(account_row("借款-甲")["kind"], "receivable")

        bk.set_account("花呗", -500)
        self.assertEqual(account_row("花呗")["kind"], "liability")

        bk.insert_entry({"transaction_time": "2026-07-01 10:00:00", "type": "支出", "category": "购物/日用品", "amount": 100, "account": "花呗", "transaction_object": "负债日用"})
        self.assertEqual(balances()["花呗"], -50000 - 10000)
        bk.insert_entry({"transaction_time": "2026-07-01 11:00:00", "type": "转账", "category": "转账", "amount": 200, "account": "现金", "target_account": "花呗", "transaction_object": "负债还款"})
        self.assertEqual(balances()["花呗"], -50000 - 10000 + 20000)

        self.assertTrue(bk.set_account_archived("花呗", True)["archived"])
        self.assertEqual(account_row("花呗")["archived"], 1)
        bk.set_account_archived("花呗", False)
        self.assertEqual(account_row("花呗")["archived"], 0)

        with self.assertRaises(ValueError) as raised:
            bk.set_account_archived("现金", True)
        self.assertTrue("债权" in str(raised.exception) or "负债" in str(raised.exception))
        with self.assertRaises(ValueError):
            bk.set_account_archived("不存在的账户", True)

        bk.set_account_archived("花呗", True)
        bk.init_db()
        self.assertEqual(account_row("花呗")["archived"], 1)
        bk.set_account_archived("花呗", False)

        before = balances()["花呗"]
        out = bk.correct_balance("花呗", -450, note="示例余额修正")
        self.assertTrue(out["entry_id"])
        self.assertEqual(account_row("花呗")["current_balance_cents"], -45000)
        conn = bk.connect()
        try:
            corr = conn.execute("select type from entries where id = ?", (out["entry_id"],)).fetchone()
            count = conn.execute("select count(*) c from entries where account = '花呗' and type = '余额修正'").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(corr["type"], "余额修正")
        self.assertEqual(count, 1)
        self.assertNotEqual(balances()["花呗"], before)

        bk.insert_entry({"transaction_time": "2026-07-02 10:00:00", "type": "转账", "category": "转账", "amount": 50, "account": "现金", "target_account": "花呗", "transaction_object": "负债再还"})
        self.assertEqual(balances()["花呗"], -45000 + 5000)

    def test_archive_derives_principal_from_borrow_transfers(self):
        """借款型本金未手填时，归档快照从缓冲账户转出（借入）流水推导本金。"""
        bk.set_account("借款-己", 0, kind="liability")
        bk.insert_entry({"transaction_time": "2026-08-01 10:00:00", "type": "转账", "category": "转账", "amount": 6500, "account": "借款-己", "target_account": "现金", "transaction_object": "借入未填本金"})
        bk.insert_entry({"transaction_time": "2026-08-02 10:00:00", "type": "转账", "category": "转账", "amount": 3000, "account": "现金", "target_account": "借款-己", "transaction_object": "还款一"})
        bk.insert_entry({"transaction_time": "2026-08-03 10:00:00", "type": "转账", "category": "转账", "amount": 3500, "account": "现金", "target_account": "借款-己", "transaction_object": "还款二"})
        out = bk.set_account_archived("借款-己", True)
        self.assertEqual(out["principal_cents"], 650000)
        self.assertEqual(out["repay_cents"], 650000)
        self.assertEqual(out["interest_cents"], 0)
        row = account_row("借款-己")
        self.assertEqual(row["archived_principal_cents"], 650000)
        self.assertEqual(row["archived_interest_cents"], 0)
        bk.set_account_archived("借款-己", False)

        # 没有借入流水时不瞎猜：本金保持 NULL
        bk.set_account("借款-庚", 0, kind="liability")
        out2 = bk.set_account_archived("借款-庚", True)
        self.assertIsNone(out2["principal_cents"])
        self.assertIsNone(out2["interest_cents"])
        bk.set_account_archived("借款-庚", False)

    def _columns(self):
        conn = bk.connect()
        try:
            return [row[1] for row in conn.execute("pragma table_info(accounts)")]
        finally:
            conn.close()


class DebtModeTest(unittest.TestCase):
    """信用卡消费计入支出，贷款月供计入支出，同一笔不能计两次。"""

    MONTH = "2026-08"

    def net(self):
        return bk.net_expense_stats(bk.connect(), self.MONTH)

    def test_credit_spend_counts_repay_does_not(self):
        card = account_row("花呗")
        self.assertEqual(card["expense_mode"], "consume")
        self.assertEqual(card["expense_mode_locked"], 1)
        before = self.net()[2]["net"]
        bk.insert_entry({"transaction_time": f"{self.MONTH}-01 10:00:00", "type": "支出", "category": "购物/日用品", "amount": 100, "account": "花呗", "transaction_object": "口径日用"})
        bk.insert_entry({"transaction_time": f"{self.MONTH}-02 10:00:00", "type": "转账", "category": "信用卡还款", "amount": 100, "account": "现金", "target_account": "花呗", "transaction_object": "口径还款"})
        _, _, totals, repay = self.net()
        # 消费记支出，还款转账不再记一笔。增量只比自己插入前多 100 元。
        self.assertEqual(totals["net"] - before, 10000)
        self.assertNotIn("信用卡还款", repay)

    def test_mortgage_installment_counts_once(self):
        bk.set_account("房贷", -100000, kind="liability")
        row = account_row("房贷")
        self.assertEqual(row["expense_mode"], "repay")
        self.assertEqual(row["expense_mode_locked"], 0)
        before = self.net()[2]["net"]
        before_day = self.net()[0].get(f"{self.MONTH}-05", {}).get("net", 0)
        before_repay = self.net()[3].get("房贷", 0)
        bk.insert_entry({"transaction_time": f"{self.MONTH}-05 10:00:00", "type": "转账", "category": "房贷", "amount": 1000, "account": "现金", "target_account": "房贷", "transaction_object": "口径月供"})
        _, _, totals, repay = self.net()
        self.assertEqual(totals["net"] - before, 100000)
        self.assertEqual(repay["房贷"] - before_repay, 100000)
        self.assertEqual(self.net()[0][f"{self.MONTH}-05"]["net"] - before_day, 100000)

        plain = bk.insert_entry({"transaction_time": f"{self.MONTH}-06 10:00:00", "type": "转账", "category": "转账", "amount": 300, "account": "现金", "target_account": "房贷", "transaction_object": "口径普通转账"})
        self.assertTrue(plain.get("warning"))
        self.assertEqual(self.net()[2]["net"] - before, 100000)

        with self.assertRaises(ValueError) as raised:
            bk.insert_entry({"transaction_time": f"{self.MONTH}-07 10:00:00", "type": "支出", "category": "居家/住宿房租", "amount": 1000, "account": "房贷", "transaction_object": "口径误记"})
        self.assertIn("还款口径", str(raised.exception))
        bk.insert_entry({"transaction_time": f"{self.MONTH}-07 10:00:00", "type": "转账", "category": "房贷", "amount": 1000, "account": "现金", "target_account": "房贷", "transaction_object": "口径月供二"})
        self.assertEqual(self.net()[2]["net"] - before, 200000)
        self._check_interest_and_archive_snapshot()
        self._check_locked_mode_and_switch()
        self._check_migration_and_principal()
        self._check_debt_type_labels()

    def _check_interest_and_archive_snapshot(self):
        bk.set_account("借款-乙", 0, kind="liability", expense_mode="consume", principal="1000")
        row = account_row("借款-乙")
        self.assertEqual(row["expense_mode"], "consume")
        self.assertEqual(row["expense_mode_locked"], 0)
        self.assertEqual(row["principal_cents"], 100000)
        for index in range(3):
            bk.insert_entry({
                "transaction_time": f"2026-0{index + 1}-15 10:00:00",
                "type": "转账", "category": "转账", "amount": 400,
                "account": "现金", "target_account": "借款-乙", "transaction_object": "口径分期",
            })
        rows = {item["name"]: item for item in bk.api_summary()["account_balances"]}
        self.assertEqual(rows["借款-乙"]["repaid_cents"], 120000)
        self.assertEqual(rows["借款-乙"]["interest_cents"], 20000)
        self.assertEqual(rows["借款-乙"]["debt_type"], "借款")

        out = bk.set_account_archived("借款-乙", True)
        self.assertEqual(out["principal_cents"], 100000)
        self.assertEqual(out["repay_cents"], 120000)
        self.assertEqual(out["interest_cents"], 20000)
        bk.insert_entry({"transaction_time": "2026-09-20 10:00:00", "type": "转账", "category": "转账", "amount": 100, "account": "现金", "target_account": "借款-乙", "transaction_object": "口径追加"})
        self.assertEqual(account_row("借款-乙")["archived_repay_cents"], 120000)
        self.assertEqual(account_row("借款-乙")["archived_interest_cents"], 20000)
        bk.set_account_archived("借款-乙", False)
        cleared = account_row("借款-乙")
        self.assertIsNone(cleared["archived_principal_cents"])
        self.assertIsNone(cleared["archived_repay_cents"])
        bk.set_account_archived("借款-乙", True)
        self.assertEqual(account_row("借款-乙")["archived_repay_cents"], 130000)
        bk.set_account_archived("借款-乙", False)

    def _check_locked_mode_and_switch(self):
        with self.assertRaises(ValueError) as raised:
            bk.set_account("花呗", expense_mode="repay")
        self.assertIn("锁定", str(raised.exception))
        self.assertEqual(account_row("花呗")["expense_mode"], "consume")
        with self.assertRaises(ValueError):
            bk.set_account("花呗", principal="3000")
        self.assertIsNone(account_row("花呗")["principal_cents"])
        out = bk.set_account_archived("花呗", True)
        self.assertIsNone(out["principal_cents"])
        self.assertIsNone(out["repay_cents"])
        bk.set_account_archived("花呗", False)

        bk.set_account("借款-丙", 0, kind="liability")
        before = self.net()[2]["net"]
        before_repay = self.net()[3].get("房贷", 0)
        bk.insert_entry({"transaction_time": f"{self.MONTH}-08 10:00:00", "type": "转账", "category": "房贷", "amount": 800, "account": "现金", "target_account": "借款-丙", "transaction_object": "口径切换"})
        self.assertEqual(self.net()[2]["net"], before)
        bk.set_account("借款-丙", expense_mode="repay")
        _, _, totals, repay = self.net()
        self.assertEqual(totals["net"], before + 80000)
        self.assertEqual(repay["房贷"] - before_repay, 80000)

    def _check_migration_and_principal(self):
        watched = ("花呗", "房贷", "借款-乙", "借款-丙")
        before = {name: self._mode(name) for name in watched}
        conn = bk.connect()
        entries_before = dict(conn.execute("select count(*) c, coalesce(sum(amount_cents),0) s from entries").fetchone())
        conn.close()
        bk.init_db()
        bk.init_db()
        after = {name: self._mode(name) for name in watched}
        self.assertEqual(before, after)
        conn = bk.connect()
        entries_after = dict(conn.execute("select count(*) c, coalesce(sum(amount_cents),0) s from entries").fetchone())
        conn.close()
        self.assertEqual(entries_before, entries_after)

        bk.set_account("借款-丁", 0, kind="liability")
        rows = {item["name"]: item for item in bk.api_summary()["account_balances"]}
        self.assertIsNone(rows["借款-丁"]["principal_cents"])
        self.assertIsNone(rows["借款-丁"]["interest_cents"])

        bk.set_account("车贷", -8000, kind="liability", expense_mode="repay", principal="8000")
        self.assertEqual(account_row("车贷")["expense_mode"], "repay")
        self.assertEqual(account_row("车贷")["principal_cents"], 800000)
        self.assertIn("房贷", bk.valid_categories())
        self.assertIn("车贷", bk.valid_categories())

        for bad in (0, -100, "-50"):
            with self.assertRaises(ValueError):
                bk.set_account("借款-丁", principal=bad)
        self.assertIsNone(account_row("借款-丁")["principal_cents"])
        bk.set_account("借款-丁", principal="1200")
        self.assertEqual(account_row("借款-丁")["principal_cents"], 120000)
        bk.set_account("借款-丁", principal="")
        self.assertEqual(account_row("借款-丁")["principal_cents"], 120000)

    def _check_debt_type_labels(self):
        self.assertEqual(da.derive_debt_type("consume", 1), "信用卡")
        self.assertEqual(da.derive_debt_type("consume", 0), "借款")
        self.assertEqual(da.derive_debt_type("repay", 0), "贷款")
        self.assertEqual(da.derive_debt_type("repay", 1), "贷款")
        self.assertIsNone(da.derive_debt_type("", 0))
        self.assertEqual(da.guess_expense_mode("花呗"), ("consume", 1))
        self.assertEqual(da.guess_expense_mode("房贷"), ("repay", 0))
        self.assertEqual(da.guess_expense_mode("车贷"), ("repay", 0))
        self.assertEqual(da.guess_expense_mode("借款-甲"), ("consume", 0))
        rows = {item["name"]: item for item in bk.api_summary()["account_balances"]}
        self.assertEqual(rows["花呗"]["debt_type"], "信用卡")
        self.assertEqual(rows["房贷"]["debt_type"], "贷款")
        self.assertEqual(rows["借款-丙"]["debt_type"], "贷款")

    def _mode(self, name):
        row = account_row(name)
        return {key: row[key] for key in ("expense_mode", "expense_mode_locked", "principal_cents")}


class AssetManagerTest(unittest.TestCase):
    def test_groups_and_net_worth(self):
        bk.set_account("工资卡", 1000, kind="asset")
        bk.set_account("借款-戊", -300, kind="liability")
        bk.set_account("应收-甲", 500, kind="receivable")
        bk.set_account("示例钱包", 0, kind="asset")
        bk.set_account("零钱", 0, kind="asset")
        bk.set_account("定期存款", 0, kind="asset")

        bk.init_db()
        bk.init_db()
        types = self._asset_types()
        self.assertEqual(types["工资卡"], "bank")
        self.assertEqual(types["现金"], "cash")
        self.assertEqual(types["零钱"], "virtual")
        self.assertEqual(types["定期存款"], "custom")
        self.assertEqual(da.guess_asset_type("工资卡"), "bank")
        self.assertEqual(da.guess_asset_type("示例钱包"), "cash")
        self.assertEqual(da.guess_asset_type("零钱"), "virtual")
        self.assertEqual(da.guess_asset_type("定期存款"), "custom")

        groups = self._groups()
        self.assertIn("花呗", [row["name"] for row in groups["credit"]])
        self.assertIn("借款-戊", [row["name"] for row in groups["liability"]])
        self.assertIn("应收-甲", [row["name"] for row in groups["receivable"]])
        self.assertIn("工资卡", [row["name"] for row in groups["bank"]])

        nw = bk.api_summary()["net_worth"]
        asset_total = sum(row["computed_balance_cents"] for row in bk.api_summary()["account_balances"] if (row.get("kind") or "asset") == "asset")
        asset_total += sum(row["computed_balance_cents"] for row in bk.api_summary()["account_balances"] if row.get("kind") == "receivable")
        liability_total = abs(sum(row["computed_balance_cents"] for row in bk.api_summary()["account_balances"] if row.get("kind") == "liability"))
        self.assertEqual(nw["asset_total_cents"], asset_total)
        self.assertEqual(nw["liability_total_cents"], liability_total)
        self.assertEqual(nw["net_worth_cents"], asset_total - liability_total)

        conn = bk.connect()
        investments.add_category(conn, "示例大类")
        conn.commit()
        category_id = conn.execute("select id from invest_categories where name = '示例大类'").fetchone()[0]
        investments.add_account(conn, category_id, "示例理财户")
        conn.commit()
        account_id = conn.execute("select id from invest_accounts where name = '示例理财户'").fetchone()[0]
        investments.add_asset(conn, account_id, "示例标的", quantity="0", current_price_cents=100000)
        conn.commit()
        asset_id = conn.execute("select id from invest_assets where name = '示例标的'").fetchone()[0]
        # 市值按买入流水算持仓，不看建标的时填的数量。
        conn.execute(
            "insert into invest_trades(asset_id, trade_type, quantity, price_cents, amount_cents, trade_time, cash_account, created_at)"
            " values (?, '买入', '1', 100000, 100000, ?, '', ?)",
            (asset_id, bk.now_iso(), bk.now_iso()),
        )
        conn.commit()
        conn.close()
        grown = bk.api_summary()["net_worth"]
        self.assertEqual(grown["asset_total_cents"], nw["asset_total_cents"] + 100000)
        self.assertEqual(grown["net_worth_cents"], nw["net_worth_cents"] + 100000)
        self._check_typed_add_and_validation()
        self._check_archived_liability_stays_in_net_worth()

    def _check_typed_add_and_validation(self):
        bk.set_account("车贷乙", -5000, kind="liability", expense_mode="repay", principal="5000")
        row = account_row("车贷乙")
        self.assertEqual(row["expense_mode"], "repay")
        self.assertEqual(row["principal_cents"], 500000)
        bk.set_account("京东白条", -100, kind="liability", expense_mode="consume")
        row = account_row("京东白条")
        self.assertEqual(row["expense_mode"], "consume")
        self.assertEqual(row["expense_mode_locked"], 1)

        with self.assertRaises(ValueError) as raised:
            bk.set_account("示例投资", 0, kind="asset", asset_type="invest")
        self.assertIn("理财页", str(raised.exception))
        for bad in ("gold", "stock"):
            with self.assertRaises(ValueError) as raised:
                bk.set_account("工资卡", asset_type=bad)
            self.assertIn("asset_type", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            bk.set_account("花呗", asset_type="cash")
        self.assertIn("资产账户", str(raised.exception))

    def _check_archived_liability_stays_in_net_worth(self):
        before = bk.api_summary()["net_worth"]["liability_total_cents"]
        bk.set_account_archived("借款-戊", True)
        groups = self._groups()
        self.assertNotIn("借款-戊", [row["name"] for row in groups["liability"]])
        self.assertEqual(bk.api_summary()["net_worth"]["liability_total_cents"], before)
        bk.set_account_archived("借款-戊", False)

    def _asset_types(self):
        conn = bk.connect()
        try:
            return {row["name"]: row["asset_type"] for row in conn.execute("select name, asset_type from accounts where kind = 'asset'")}
        finally:
            conn.close()

    def _groups(self):
        groups = {"credit": [], "liability": [], "receivable": [], "bank": []}
        for row in bk.api_summary()["account_balances"]:
            if int(row.get("archived") or 0) == 1:
                continue
            kind = row.get("kind") or "asset"
            if kind == "liability":
                key = "credit" if da.derive_debt_type(row.get("expense_mode"), row.get("expense_mode_locked")) == "信用卡" else "liability"
                groups[key].append(row)
            elif kind == "receivable":
                groups["receivable"].append(row)
            elif (row.get("asset_type") or "") == "bank":
                groups["bank"].append(row)
        return groups


# 负债口径的用例会建「房贷」等账户，资产分组的用例要读到它们。
# 默认按类名排序会让资产分组先跑，所以这里按依赖顺序装载。
def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    for case in (DebtArchiveTest, DebtModeTest, AssetManagerTest):
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


if __name__ == "__main__":
    unittest.main()
