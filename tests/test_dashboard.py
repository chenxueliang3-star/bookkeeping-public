# -*- coding: utf-8 -*-
"""总览和资产页的入口、文案。只读静态文件，不连数据库。"""
import os
import re
import unittest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "bookkeeping_app", "static")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


class OverviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read("index.html")
        cls.js = read("dashboard.js")

    def test_refund_card_is_left_of_pending(self):
        refund_pos = self.html.find('id="refundKpi"')
        pending_pos = self.html.find('id="pendingKpi"')
        self.assertNotEqual(refund_pos, -1)
        self.assertNotEqual(pending_pos, -1)
        self.assertLess(refund_pos, pending_pos)

    def test_pending_card_always_opens_confirm(self):
        block = re.search(r'<div class="kpi[^"]*" id="pendingKpi"[^>]*>', self.html)
        self.assertIsNotNone(block)
        self.assertIn("kpi-action", block.group(0))
        match = re.search(r"\$\('pendingKpi'\)\.onclick\s*=\s*\(\)\s*=>\s*switchView\('confirm'\)", self.js)
        self.assertIsNotNone(match)
        start = self.js.find("$('pendingKpi').onclick")
        self.assertNotIn("unconfirmed_count", self.js[start:start + 200])

    def test_invest_page_stays_closed(self):
        nav = re.search(r'<button[^>]*data-view="invest"[^>]*>.*?</button>', self.html, re.S)
        self.assertIsNotNone(nav)
        self.assertIn("hidden", nav.group(0))
        self.assertIsNone(re.search(r'<button(?![^>]*\bhidden\b)[^>]*data-view="invest"', self.html))
        self.assertIsNotNone(re.search(r"\$\('investKpi'\)\.onclick\s*=\s*\(\)\s*=>\s*jumpToAssetGroup\('invest'\)", self.js))
        self.assertIsNone(re.search(r"\$\('investKpi'\)\.onclick\s*=\s*\(\)\s*=>\s*switchView\('invest'\)", self.js))
        self.assertIn("点击查看资产页投资组", self.html)
        self.assertNotIn("点击进入理财页", self.html)


class AssetPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read("index.html")
        cls.js = read("dashboard.js")
        cls.css = read("dashboard.css")

    def test_invest_rows_do_not_open_invest_page(self):
        self.assertIn('class="invest-row"', self.js)
        self.assertEqual(self.js.find("querySelectorAll('tr.invest-row')"), -1)
        self.assertNotIn("点击跳转理财页", self.js)
        start = self.js.find('class="invest-row"')
        end = self.js.find("function bindAddAccountPanel", start)
        self.assertNotIn("switchView('invest')", self.js[start:end])
        self.assertIn("投资账户请在理财页添加", self.js)
        bind_start = self.js.find("function bindAddAccountPanel")
        bind_end = self.js.find("\n    function ", bind_start + 1)
        branch = self.js[bind_start:bind_end]
        branch = branch[branch.find("type === 'invest'"):branch.find("addingAccountType")]
        self.assertNotIn("switchView('invest')", branch)

    def test_hidden_class_and_nav_labels(self):
        self.assertEqual(self.css.count(".hidden { display: none !important; }"), 1)
        self.assertIn("addTypeGrid", self.js)
        self.assertIn("activity-archived-area ${showArchivedDebts ? '' : 'hidden'}", self.js)
        self.assertIn('<button data-view="assets">资产</button>', self.html)
        self.assertIn("assets: '资产管家'", self.js)
        self.assertIsNotNone(re.search(r"/static/dashboard\.js\?v=\d{8}-", self.html))


if __name__ == "__main__":
    unittest.main()
