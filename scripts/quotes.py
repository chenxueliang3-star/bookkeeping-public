"""理财行情引擎：K 线拉取与降级、现价历史缓存。

数据源（全部免费公开接口、只读、无需密钥）：
- 数字货币：Gate.io（芝麻开门）公共行情 —— 国内可直连（实测 OKX/币安/HTX/MyToken/非小号 均被墙）
- A股/场内基金：东方财富公开 K 线接口（按 6 位代码）
- 其他/自定义标的：回退为 invest_price_history 里的手动价格记录

行情数据只用于展示（K 线/现价参考），持仓成本与盈亏始终以用户记录的交易为准。
"""

import datetime as _dt
import json
import re
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 8
FETCH_LIMIT = 365
FX_MICROS = 1000000

CRYPTO_DEFAULT_MAP = {
    "BTC": "BTC-USDT",
    "ETH": "ETH-USDT",
    # USDT 不入映射：Gate 无 USDT_CNY 法币对（请求会 400），
    # USDT≈¥7.1 属准稳定值，走手动价格 + 合成 K 线即可。
    "SOL": "SOL-USDT",
    "BNB": "BNB-USDT",
    "XRP": "XRP-USDT",
    "DOGE": "DOGE-USDT",
    "ADA": "ADA-USDT",
    "TRX": "TRX-USDT",
    "AVAX": "AVAX-USDT",
    "LINK": "LINK-USDT",
    "DOT": "DOT-USDT",
}


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 bookkeeping-dashboard"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _cents(value):
    return int(round(float(value) * 100))


def _fetch_usd_cny_micros():
    data = _http_json("https://open.er-api.com/v6/latest/USD")
    rate = ((data.get("rates") or {}).get("CNY"))
    if rate is None or float(rate) <= 0:
        raise ValueError("USD/CNY unavailable")
    return int(round(float(rate) * FX_MICROS))


# ---------------------------------------------------------------- 数据源

def _fetch_gate(symbol, interval="1d", limit=FETCH_LIMIT):
    """Gate.io 现货日 K。symbol 形如 BTC_USDT。

    返回数组按时间升序，每行：
    [unix秒, 计价币成交额, open, high, low, close, 基础币成交量, 趋势]。
    最后一根可能是当日未收盘 K 线，与其他数据源行为一致。
    """
    url = ("https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=" + urllib.parse.quote(symbol)
           + f"&interval={interval}&limit={min(limit, 999)}")
    data = _http_json(url)
    out = []
    for row in data:
        ts = _dt.datetime.fromtimestamp(int(row[0])).strftime("%Y-%m-%d")
        out.append({"date": ts, "open": _cents(row[2]), "high": _cents(row[3]),
                    "low": _cents(row[4]), "close": _cents(row[5])})
    return out


def _fetch_eastmoney(code, klt=101, limit=FETCH_LIMIT):
    code = str(code).strip()
    if re.fullmatch(r"[4-8]\d{5}", code):
        secid = f"1.{code}" if code[0] in ("5", "6") else f"0.{code}"
    elif re.fullmatch(r"\d{6}", code):
        secid = f"1.{code}" if code[0] in ("5", "6", "9") else f"0.{code}"
    else:
        raise ValueError("unsupported code")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + urllib.parse.quote(secid)
           + f"&klt={klt}&fqt=1&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55&end=20500101&lmt={limit}")
    data = _http_json(url)
    klines = (data.get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        parts = line.split(",")
        # date, open, close, high, low
        out.append({"date": parts[0], "open": _cents(parts[1]), "close": _cents(parts[2]),
                    "high": _cents(parts[3]), "low": _cents(parts[4])})
    return out


def _fetch_eastmoney_spot(code):
    """Return the latest A-share quote in cents (Eastmoney f43 is fen)."""
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("unsupported code")
    secid = f"1.{code}" if code[0] in ("5", "6", "9") else f"0.{code}"
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid="
           + urllib.parse.quote(secid) + "&fields=f43,f57,f58")
    data = (_http_json(url).get("data") or {})
    value = data.get("f43")
    if value in (None, "", "-") or float(value) <= 0:
        raise ValueError("quote unavailable")
    return int(round(float(value)))


def _fetch_gate_spot(symbol):
    url = ("https://api.gateio.ws/api/v4/spot/tickers?currency_pair="
           + urllib.parse.quote(symbol))
    data = _http_json(url)
    if not data or not data[0].get("last"):
        raise ValueError("quote unavailable")
    return _cents(data[0]["last"])


def resolve_crypto_symbol(code, name=""):
    text = f"{code} {name}".upper().replace("/", "-").strip()
    if not text:
        return None
    if "-USDT" in text:
        return text.split()[0].split(",")[0]
    token = re.findall(r"[A-Z0-9]{2,10}", text)
    for candidate in token:
        if candidate in CRYPTO_DEFAULT_MAP:
            return CRYPTO_DEFAULT_MAP[candidate]
    return None


def fetch_daily_kline(code, name=""):
    """按标的代码/名称探测数据源，返回 (rows, source)。失败抛异常由调用方降级。"""
    symbol = resolve_crypto_symbol(code, name)
    if symbol:
        rows = _fetch_gate(symbol.replace("-", "_"))
        return rows, f"gate:{symbol}"
    if re.fullmatch(r"\d{6}", str(code or "").strip()):
        rows = _fetch_eastmoney(code)
        return rows, f"eastmoney:{code}"
    raise ValueError("no quote source for asset")


def fetch_spot_price(code, name=""):
    """Fetch the newest available quote, falling back to the latest daily close."""
    symbol = resolve_crypto_symbol(code, name)
    if symbol:
        return _fetch_gate_spot(symbol.replace("-", "_")), "USD", _fetch_usd_cny_micros(), f"gate:{symbol}"
    if re.fullmatch(r"\d{6}", str(code or "").strip()):
        return _fetch_eastmoney_spot(code), "CNY", FX_MICROS, f"eastmoney:{code}"
    rows, source = fetch_daily_kline(code, name)
    if rows:
        return rows[-1]["close"], "CNY", FX_MICROS, source
    raise ValueError("no quote source for asset")


# ---------------------------------------------------------------- 聚合

def aggregate(rows, interval):
    """把日 K 聚合为周/月 K。rows 需按日期升序。"""
    if interval not in ("week", "month"):
        return rows
    buckets = {}
    order = []
    for row in rows:
        date = row["date"]
        key = date[:7] if interval == "month" else _week_key(date)
        if key not in buckets:
            buckets[key] = {"date": row["date"], "open": row["open"], "high": row["high"],
                            "low": row["low"], "close": row["close"]}
            order.append(key)
        else:
            b = buckets[key]
            b["high"] = max(b["high"], row["high"])
            b["low"] = min(b["low"], row["low"])
            b["close"] = row["close"]
            b["date"] = row["date"]
    return [buckets[k] for k in order]


def _week_key(date_str):
    d = _dt.date.fromisoformat(date_str)
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def kline_for_asset(conn, asset, interval="day"):
    """优先取真实行情（带缓存），失败则用手动价格历史合成伪 K 线。"""
    from investments import _now_iso

    interval = interval if interval in ("day", "week", "month") else "day"
    rows, source = [], "manual"
    try:
        rows, source = fetch_daily_kline(asset["code"], asset["name"])
    except Exception:
        rows = []
    if rows:
        conn.executemany(
            "insert or replace into invest_price_history(asset_id, price_date, open_cents, high_cents, low_cents,"
            " close_cents, source, created_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
            [(asset["id"], r["date"], r["open"], r["high"], r["low"], r["close"], source, _now_iso()) for r in rows],
        )
        # 顺手把最新收盘价写为现价参考
        latest = rows[-1]["close"]
        if asset["current_price_cents"] is None:
            conn.execute("update invest_assets set current_price_cents = ? where id = ?", (latest, asset["id"]))
    else:
        rows = _synthesize_from_history(conn, asset["id"])
    return {
        "ok": bool(rows), "source": source, "interval": interval,
        "currency": asset.get("quote_currency") or "CNY",
        "fx_cny_micros": int(asset.get("current_fx_cny_micros") or FX_MICROS),
        "klines": aggregate(rows, interval),
    }


def _synthesize_from_history(conn, asset_id):
    rows = conn.execute(
        "select price_date, close_cents from invest_price_history where asset_id = ? and close_cents is not null"
        " order by price_date",
        (asset_id,),
    ).fetchall()
    points = [(r["price_date"], int(r["close_cents"])) for r in rows]
    # 加入手动现价（若有）保证最新点存在
    asset = conn.execute("select current_price_cents from invest_assets where id = ?", (asset_id,)).fetchone()
    today = _dt.date.today().isoformat()
    if asset and asset["current_price_cents"] and (not points or points[-1][0] != today):
        points.append((today, int(asset["current_price_cents"])))
    if not points:
        return []
    if len(points) == 1:
        d, c = points[0]
        return [{"date": d, "open": c, "high": c, "low": c, "close": c}]
    out = []
    for i, (d, c) in enumerate(points):
        prev = points[i - 1][1] if i > 0 else c
        high = max(prev, c)
        low = min(prev, c)
        out.append({"date": d, "open": prev, "high": high, "low": low, "close": c})
    return out


def auto_update_price(conn, asset):
    from investments import _now_iso
    """尝试拉最新收盘价更新现价；失败静默返回 None。"""
    try:
        latest_price, quote_currency, fx_cny_micros, source = fetch_spot_price(asset["code"], asset["name"])
        if not latest_price:
            return None
        # Keep the daily history for charts, while the asset uses the newest spot quote.
        try:
            rows, _ = fetch_daily_kline(asset["code"], asset["name"])
        except Exception:
            rows = []
        conn.executemany(
            "insert or replace into invest_price_history(asset_id, price_date, open_cents, high_cents, low_cents,"
            " close_cents, source, created_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
            [(asset["id"], r["date"], r["open"], r["high"], r["low"], r["close"], source, _now_iso()) for r in rows[-30:]],
        )
        conn.execute("update invest_assets set current_price_cents = ?, quote_currency = ?, current_fx_cny_micros = ? where id = ?", (latest_price, quote_currency, fx_cny_micros, asset["id"]))
        return latest_price
    except Exception:
        return None
