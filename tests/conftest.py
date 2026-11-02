# -*- coding: utf-8 -*-
"""全套测试共用一个空库。

建库、建户都会写 config.yaml，所以这里把配置复制到临时目录再改 CONFIG_PATH，
真实账本和真实配置不会被打开。各测试用自己的商户名，查询时按商户过滤，
避免互相看见别人插入的流水。
"""
import os
import shutil
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SCRIPTS, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import bookkeeping as bk  # noqa: E402

_READY = False


def use_empty_ledger():
    """第一次调用时建库、建表、放好虚构账户。之后的调用直接返回。"""
    global _READY
    if _READY:
        return
    folder = tempfile.mkdtemp(prefix="bookkeeping-tests-")
    db_path = os.path.join(folder, "ledger.db")
    cfg_path = os.path.join(folder, "config.yaml")
    shutil.copy(os.path.join(SCRIPTS, "config.yaml"), cfg_path)
    os.environ["BOOKKEEPING_DB_PATH"] = db_path
    bk.set_database_path(db_path)
    bk.CONFIG_PATH = bk.Path(cfg_path)
    bk.init_db()
    _READY = True


def account_row(name):
    conn = bk.connect()
    try:
        row = conn.execute("select * from accounts where name = ?", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def balances():
    return {row["name"]: row["computed_balance_cents"] for row in bk.account_computed_balances(bk.connect())}
