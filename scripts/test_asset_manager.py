# -*- coding: utf-8 -*-
"""资产管家改版 TDD：分组派生 / 净资产卡 / 类型化添加 / 迁移回填。
对应 docs/design-asset-manager.md §11 测试计划 10 条（前端交互项以源码断言 + E2E 兜底）。"""
import os
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), "tmp_asset_manager_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["BOOKKEEPING_DB_PATH"] = TMP
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.join(_SCRIPTS, "bookkeeping_app"))

import bookkeeping as bk  # noqa: E402
import debt_accounts as da  # noqa: E402
import investments  # noqa: E402

# 分类配置也用临时副本：测试建户不污染真实 config.yaml 的 accounts 列表
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402
_REAL_CFG = os.path.join(_SCRIPTS, "config.yaml")
_TMP_CFG = os.path.join(tempfile.gettempdir(), "tmp_asset_manager_config.yaml")
shutil.copy(_REAL_CFG, _TMP_CFG)
bk.CONFIG_PATH = Path(_TMP_CFG)

STATIC_DIR = os.path.join(_SCRIPTS, "bookkeeping_app", "static")

bk.init_db()

# ---- 1. 分组正确性（后端 summary 数据 → 前端 assetGroupRows 同规则镜像）：----
# 花呗（liability+locked）落「信用卡」组、借款-张三落「负债」组、爸爸借落「债权」组、招行储蓄卡落「储蓄卡」组
bk.set_account("招行储蓄卡", 8938, kind="asset")
bk.set_account("花呗", -9000, kind="liability")
bk.set_account("借款-张三", -3000, kind="liability")
bk.set_account("爸爸借", 19000, kind="receivable")
bk.set_account("现金", 0, kind="asset")
bk.set_account("某某资产", 6000, kind="asset")


def mirror_groups():
    """与 dashboard.js assetGroupRows 同规则的 Python 镜像（呈现层派生断言用）。"""
    summary = bk.api_summary()
    groups = {"cash": [], "virtual": [], "bank": [], "credit": [], "liability": [], "receivable": [], "custom": [], "invest": []}
    for row in summary["account_balances"]:
        if Number_archived(row):
            continue
        kind = row.get("kind") or "asset"
        if kind == "invest":
            groups["invest"].append(row)
        elif kind == "liability":
            key = "credit" if da.derive_debt_type(row.get("expense_mode"), row.get("expense_mode_locked")) == "信用卡" else "liability"
            groups[key].append(row)
        elif kind == "receivable":
            groups["receivable"].append(row)
        else:
            groups[row.get("asset_type") or "custom"].append(row)
    return groups


def Number_archived(row):
    return int(row.get("archived") or 0) == 1


def subtotal(rows):
    return sum(int(r["computed_balance_cents"]) for r in rows)


groups = mirror_groups()
assert [r["name"] for r in groups["credit"]] == ["花呗"], groups["credit"]
assert [r["name"] for r in groups["liability"]] == ["借款-张三"], groups["liability"]
assert [r["name"] for r in groups["receivable"]] == ["爸爸借"], groups["receivable"]
assert [r["name"] for r in groups["bank"]] == ["招行储蓄卡"], groups["bank"]
assert subtotal(groups["credit"]) == -900000, subtotal(groups["credit"])
assert subtotal(groups["liability"]) == -300000, subtotal(groups["liability"])
assert subtotal(groups["receivable"]) == 1900000, subtotal(groups["receivable"])
assert subtotal(groups["bank"]) == 893800, subtotal(groups["bank"])

# ---- 2. 存量名称回填：bank/cash/virtual/custom + 幂等（重复 init_db 不变）----
bk.set_account("工商银行储蓄卡", 0, kind="asset")
bk.set_account("零钱", 0, kind="asset")
bk.set_account("某某", 0, kind="asset")


def asset_types():
    conn = bk.connect()
    rows = {r["name"]: r["asset_type"] for r in conn.execute("select name, asset_type from accounts where kind='asset'").fetchall()}
    conn.close()
    return rows


bk.init_db()
before = asset_types()
bk.init_db()
after = asset_types()
assert before == after, (before, after)
assert after["工商银行储蓄卡"] == "bank", after
assert after["现金"] == "cash", after
assert after["零钱"] == "virtual", after
assert after["某某"] == "custom", after  # 宁缺勿错
# guess_asset_type 单元断言（顺序敏感：bank→cash→virtual）
assert da.guess_asset_type("招行储蓄卡(5072)") == "bank"
assert da.guess_asset_type("钱包") == "cash"
assert da.guess_asset_type("支付宝小荷包") == "virtual"
assert da.guess_asset_type("定期存款(农行)") == "custom"

# ---- 3. 净资产公式（鲨鱼口径：债权计资产侧、负债取绝对值）----
summary = bk.api_summary()
nw = summary["net_worth"]
# 自定义资产（某某资产 6000）+ 储蓄卡 8938 + 现金 0 + 某某 0 + 工商 0 + 零钱 0 + 债权 19000 = 33938
assert nw["asset_total_cents"] == 3393800, nw
assert nw["liability_total_cents"] == 1200000, nw  # 信用卡 9000 + 负债 3000
assert nw["net_worth_cents"] == 2193800, nw

# ---- 4. 净资产含投资市值（市值口径）----
conn = bk.connect()
investments.add_category(conn, "测试大类")
conn.commit()
cat_id = conn.execute("select id from invest_categories where name='测试大类'").fetchone()[0]
investments.add_account(conn, cat_id, "测试理财户")
conn.commit()
acc_id = conn.execute("select id from invest_accounts where name='测试理财户'").fetchone()[0]
investments.add_asset(conn, acc_id, "测试标的", quantity="1", current_price_cents=1000000)  # 现价 10000 元
conn.commit()
asset_id = conn.execute("select id from invest_assets where name='测试标的'").fetchone()[0]
# 建仓流水（测试夹具直插：走 add_trade 会连带生成现金转账流水，干扰净资产断言）
conn.execute(
    "insert into invest_trades(asset_id, trade_type, quantity, price_cents, amount_cents, trade_time, cash_account, created_at)"
    " values (?, '买入', '1', 1000000, 1000000, ?, '', ?)",
    (asset_id, bk.now_iso(), bk.now_iso()),
)
conn.commit()
conn.close()
nw2 = bk.api_summary()["net_worth"]
assert nw2["asset_total_cents"] == nw["asset_total_cents"] + 1000000, (nw, nw2)  # +10000 元市值
assert nw2["net_worth_cents"] == 2193800 + 1000000, nw2

# ---- 5. 类型化添加（后端口径）：负债→贷款型 → repay + principal 落库；信用卡名称校验拒绝 ----
bk.set_account("房贷X", -500000, kind="liability", expense_mode="repay", principal="500000")
conn = bk.connect()
row = dict(conn.execute("select * from accounts where name='房贷X'").fetchone())
conn.close()
assert row["expense_mode"] == "repay" and row["principal_cents"] == 50000000, row  # 500000 元
# 信用卡型：名称命中 patterns → 后端强制 consume+锁定（前端对不命中名称先行拦截，见 §8/§9 源码断言与 E2E）
bk.set_account("E2E京东白条", -100, kind="liability", expense_mode="consume")
conn = bk.connect()
row = dict(conn.execute("select * from accounts where name='E2E京东白条'").fetchone())
conn.close()
assert row["expense_mode"] == "consume" and row["expense_mode_locked"] == 1, row
# asset_type invest → 400「投资账户请在理财页添加」
try:
    bk.set_account("假投资", 0, kind="asset", asset_type="invest")
    raise AssertionError("asset_type=invest 应被拒绝")
except ValueError as err:
    assert "理财页" in str(err), err

# ---- 6. 投资组（前端行为，源码断言：投资行不再进理财页，资产页无理财编辑入口）----
js = open(os.path.join(STATIC_DIR, "dashboard.js"), encoding="utf-8").read()
assert 'class="invest-row"' in js, "投资账户行应带 invest-row 标记"
# 投资行已在资产页投资组内，V1 不再绑定点击跳理财页
row_bind_start = js.find("querySelectorAll('tr.invest-row')")
assert row_bind_start == -1, "投资行不应再绑定 tr.invest-row 点击"
assert "点击跳转理财页" not in js, "投资行 title 不得再写点击跳转理财页"
# 宫格「投资」仍可能提示去理财页；行点击不得再 switchView('invest')
row_template_start = js.find('class="invest-row"')
row_template_end = js.find("function bindAddAccountPanel", row_template_start)
row_slice = js[row_template_start:row_template_end if row_template_end != -1 else row_template_start + 800]
assert "switchView('invest')" not in row_slice, "投资行不应再 switchView('invest')"
assert "投资账户请在理财页添加" in js, "投资宫格应提示去理财页添加"
bind_start = js.find("function bindAddAccountPanel")
assert bind_start != -1, "应存在 bindAddAccountPanel"
bind_end = js.find("\n    function ", bind_start + 1)
bind_fn = js[bind_start:bind_end if bind_end != -1 else bind_start + 2500]
invest_branch = bind_fn[bind_fn.find("type === 'invest'"):bind_fn.find("addingAccountType")]
assert "switchView('invest')" not in invest_branch, "添加宫格 invest 分支不得 switchView('invest')"

# ---- 7. 归档账户不进主列表：归档负债只出现在折叠区；净资产仍计入（归档≠销账，口径固化）----
bk.set_account_archived("借款-张三", True)
groups = mirror_groups()
liability_names = [r["name"] for r in groups["liability"]]
assert "借款-张三" not in liability_names, liability_names  # 归档负债不进主列表
nw3 = bk.api_summary()["net_worth"]
assert nw3["liability_total_cents"] == 51210000, nw3  # 归档负债仍计入负债侧（归档≠销账）：白条 100 + 花呗 9000 + 张三 3000 + 房贷X 500000
bk.set_account_archived("借款-张三", False)

# ---- 8. .hidden 回归（CSS 源码断言）：工具类保持 display:none!important 且未被组样式覆盖 ----
css = open(os.path.join(STATIC_DIR, "dashboard.css"), encoding="utf-8").read()
assert ".hidden { display: none !important; }" in css, ".hidden 工具类被改动"
# 裸 .hidden 工具类只允许一条定义（.X.hidden 组合选择器不算覆盖，历史上正是组样式覆盖了它）
assert css.count(".hidden { display: none !important; }") == 1, "出现多个裸 .hidden 定义，存在覆盖风险"
# 折叠区/类型宫格用 .hidden 控制（源码存在）
assert "id=\"addTypeGrid\"" in js.replace("'", '"') or "addTypeGrid" in js
assert "activity-archived-area ${showArchivedDebts ? '' : 'hidden'}" in js

# ---- 9. nav 文案：nav「资产」、pageTitle「资产管家」，data-view 不变 ----
html = open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read()
assert '<button data-view="assets">资产</button>' in html, html
assert 'data-view="assets"' in html
assert "assets: '资产管家'" in js, "pageTitle 映射应改为 资产管家"
assert "?v=20261022-oss1" in html, "缓存版本应 bump"

# ---- 10. asset_type 校验已在 5 覆盖（invest → 400）；枚举外值同样 400 ----
for bad in ("gold", "stock"):
    try:
        bk.set_account("招行储蓄卡", asset_type=bad)
        raise AssertionError(f"asset_type={bad} 应被拒绝")
    except ValueError as err:
        assert "asset_type" in str(err), err
# asset_type 仅资产账户：负债账户传 asset_type → 400
try:
    bk.set_account("花呗", asset_type="cash")
    raise AssertionError("负债账户传 asset_type 应被拒绝")
except ValueError as err:
    assert "资产账户" in str(err), err

print("test_asset_manager: 10 组断言全部通过")
