# -*- coding: utf-8 -*-
"""总览 KPI：待确认与退款互换位置；待确认始终可跳确认页（含 0 笔）。"""
import os
import re

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookkeeping_app", "static")
html = open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read()
js = open(os.path.join(STATIC_DIR, "dashboard.js"), encoding="utf-8").read()

# 1. 退款在前、待确认在后（DOM 顺序即视觉顺序）
refund_pos = html.find('id="refundKpi"')
pending_pos = html.find('id="pendingKpi"')
assert refund_pos != -1, "退款卡应有 id=refundKpi"
assert pending_pos != -1, "待确认卡应有 id=pendingKpi"
assert refund_pos < pending_pos, "退款应在待确认左侧"

# 2. 待确认始终是可点击 KPI（不因 count=0 去掉 kpi-action）
pending_block = re.search(r'<div class="kpi[^"]*" id="pendingKpi"[^>]*>', html)
assert pending_block, "找不到 pendingKpi 开标签"
assert "kpi-action" in pending_block.group(0), "待确认卡应带 kpi-action，0 笔也可点"

# 3. 点击跳待确认页，不依赖 unconfirmed_count
assert "$('pendingKpi').onclick" in js, "应绑定 pendingKpi 点击"
assert re.search(r"\$\('pendingKpi'\)\.onclick\s*=\s*\(\)\s*=>\s*switchView\('confirm'\)", js), (
    "pendingKpi 应无条件 switchView('confirm')"
)
assert "unconfirmed_count" not in js[js.find("$('pendingKpi').onclick"): js.find("$('pendingKpi').onclick") + 200]

# 4. V1 关闭理财 Web 入口：nav 无可见「理财」按钮
invest_nav = re.search(r'<button[^>]*data-view="invest"[^>]*>.*?</button>', html, re.S)
if invest_nav:
    assert "hidden" in invest_nav.group(0), "nav 理财按钮应 hidden，用户进不去"
assert not re.search(
    r'<button(?![^>]*\bhidden\b)[^>]*data-view="invest"',
    html,
), "nav 不得出现可见的理财入口"

# 5. 总览「理财市值」KPI 跳资产页投资组，不再进理财页
assert "jumpToAssetGroup('invest')" in js, "investKpi 应走 jumpToAssetGroup('invest')"
assert re.search(
    r"\$\('investKpi'\)\.onclick\s*=\s*\(\)\s*=>\s*jumpToAssetGroup\('invest'\)",
    js,
), "investKpi 应绑定 jumpToAssetGroup('invest')"
assert not re.search(
    r"\$\('investKpi'\)\.onclick\s*=\s*\(\)\s*=>\s*switchView\('invest'\)",
    js,
), "investKpi 不得再 switchView('invest')"
assert "点击查看资产页投资组" in html, "investKpi title 应说明跳资产页投资组"
assert "点击进入理财页" not in html, "investKpi 静态 title 不得再写进入理财页"

print("test_overview_kpi: 5 组断言全部通过")
