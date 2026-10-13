# -*- coding: utf-8 -*-
"""免 Tushare 数据源适配层。

职责只做三件事：协议解析、代码归一、单位标准化。策略与回测仍然只读本地
SQLite 缓存，本模块不能被因子或回测代码直接调用。
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterable
from urllib.error import HTTPError

import pandas as pd
import requests


class FreeDataSourceError(RuntimeError):
    """免费源请求或解析失败。"""


_SESSION = requests.Session()
_SESSION.trust_env = False
_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
        ),
        "Referer": "https://basic.10jqka.com.cn/",
    }
)


def normalize_ticker(code: str) -> str:
    """把 `600519.SH` / `sh600519` / `600519` 统一成 6 位代码。"""
    s = str(code).strip().lower()
    if s.startswith(("sh", "sz", "bj")) and len(s) == 8:
        s = s[2:]
    if "." in s:
        s = s.split(".", 1)[0]
    if not re.fullmatch(r"\d{6}", s):
        raise ValueError(f"非法 A 股代码: {code}")
    return s


def tencent_symbol(code: str) -> str:
    """生成腾讯行情代码；显式交易所后缀优先，避免裸代码市场歧义。"""
    s = str(code).strip().lower()
    m = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", s)
    if m:
        return m.group(2) + m.group(1)
    c = normalize_ticker(code)
    if c.startswith(("6", "5", "9")) and not c.startswith("92"):
        return f"sh{c}"
    if c.startswith(("0", "3", "1")):
        return f"sz{c}"
    if c.startswith(("4", "8", "92")):
        return f"bj{c}"
    raise ValueError(f"暂不支持自动判断市场的代码: {code}")


def _get(url: str, referer: str | None = None, retries: int = 3) -> requests.Response:
    headers = {"Referer": referer} if referer else None
    last_error: Exception | None = None
    for i in range(retries):
        try:
            r = _SESSION.get(url, headers=headers, timeout=(3, 15))
            if r.status_code == 404:
                raise HTTPError(url, 404, "not found", None, None)
            r.raise_for_status()
            return r
        except HTTPError:
            raise
        except Exception as exc:  # 网络抖动统一重试，404 不重试。
            last_error = exc
            time.sleep(0.4 * (i + 1))
    raise FreeDataSourceError(f"请求失败 {url}: {last_error}") from last_error


def _parse_jsonp(text: str) -> dict:
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right <= left:
        raise FreeDataSourceError("同花顺 JSONP 响应缺少括号")
    try:
        obj = json.loads(text[left + 1 : right])
        if not isinstance(obj, dict):
            raise ValueError("顶层不是对象")
        return obj
    except Exception as exc:
        raise FreeDataSourceError(f"同花顺 JSONP 解析失败: {exc}") from exc


def fetch_ths_daily(
    code: str,
    start: str = "2019-01-01",
    end: str | None = None,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """拉取同花顺年度日线，输出项目标准单位。

    端点返回前复权 OHLC、volume=股、amount=元、turn=百分数。项目当前
    有效研究边界从 2019 年开始，也避开该源更早年份出现负复权价的边界。
    """
    if adjust != "qfq":
        raise ValueError("当前仅验证过同花顺前复权日线")
    c = normalize_ticker(code)
    start_s = str(start).replace("-", "")
    end_s = str(end or pd.Timestamp.today().strftime("%Y-%m-%d")).replace("-", "")
    first_year = max(2019, int(start_s[:4]))
    last_year = int(end_s[:4])
    frames = []
    for year in range(first_year, last_year + 1):
        url = f"https://d.10jqka.com.cn/v6/line/hs_{c}/01/{year}.js"
        try:
            text = _get(url, referer="https://d.10jqka.com.cn/").text
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        obj = _parse_jsonp(text)
        raw = obj.get("data")
        if not raw:
            continue
        rows = []
        for line in str(raw).split(";"):
            cols = line.split(",")
            if len(cols) < 8 or not cols[0]:
                continue
            try:
                date = f"{cols[0][:4]}-{cols[0][4:6]}-{cols[0][6:8]}"
                row = [date] + [float(x) for x in cols[1:8]]
            except ValueError:
                continue
            rows.append(row)
        if rows:
            frames.append(pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount", "turn"]))

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["date"] >= str(start)) & (df["date"] <= str(end or "9999-12-31"))]
    df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    # 复权序列中的负价/零价是源数据边界，宁缺毋伪。
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)].copy()
    df["preclose"] = df["close"].shift(1)
    df["pct_chg"] = (df["close"] / df["preclose"] - 1.0) * 100.0
    df["is_st"] = 0  # 同花顺日线不含历史 ST，入库前由 loader 保留旧审计值。
    return df[
        [
            "date", "open", "high", "low", "close", "preclose",
            "volume", "amount", "turn", "pct_chg", "is_st",
        ]
    ]


def fetch_ths_finance(code: str, statement: str) -> dict:
    """拉取同花顺财务三表，返回二次解析后的 flashData。"""
    if statement not in {"benefit", "debt", "cash"}:
        raise ValueError("statement 只支持 benefit/debt/cash")
    c = normalize_ticker(code)
    url = f"https://basic.10jqka.com.cn/api/stock/finance/{c}_{statement}.json"
    obj = _get(url).json()
    try:
        flash = json.loads(obj["flashData"])
        if not isinstance(flash, dict) or "report" not in flash:
            raise ValueError("缺少 report")
        return flash
    except Exception as exc:
        raise FreeDataSourceError(f"同花顺财务数据解析失败: {c} {statement}: {exc}") from exc


def fetch_tencent_quotes(codes: Iterable[str]) -> pd.DataFrame:
    """批量拉取腾讯实时快照，金额单位亿元、市值单位亿元。"""
    symbols = [tencent_symbol(c) for c in codes]
    rows = []
    for i in range(0, len(symbols), 60):
        chunk = symbols[i : i + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        text = _get(url, referer="https://gu.qq.com/").content.decode("gbk", errors="ignore")
        for sym, item in re.findall(r'v_(\w+)="([^"]*)"', text):
            f = item.split("~")
            if len(f) < 39:
                continue
            try:
                price = _num(f[3])
                preclose = _num(f[4])
                amount_yi = _num(f[37]) / 10000.0 if _num(f[37]) is not None else None
                source_time = None
                if len(f) > 30 and re.fullmatch(r"\d{14}", f[30] or ""):
                    source_time = (
                        f"{f[30][:4]}-{f[30][4:6]}-{f[30][6:8]} "
                        f"{f[30][8:10]}:{f[30][10:12]}:{f[30][12:14]}"
                    )
                suffix = {"sh": ".SH", "sz": ".SZ", "bj": ".BJ"}.get(sym[:2], "")
                rows.append(
                    {
                        "symbol": sym,
                        "code": sym[2:] + suffix,
                        "name": f[1],
                        "price": price,
                        "preclose": preclose,
                        "open": _num(f[5]),
                        "pct_chg": _num(f[32]),
                        "high": _num(f[33]),
                        "low": _num(f[34]),
                        # 腾讯第 37 位成交额单位为万元。
                        "amount_yi": amount_yi,
                        "turn": _num(f[38]),
                        "pe_ttm": _num(f[39]) if len(f) > 39 else None,
                        "float_mcap_yi": _num(f[44]) if len(f) > 44 else None,
                        "total_mcap_yi": _num(f[45]) if len(f) > 45 else None,
                        "pb": _num(f[46]) if len(f) > 46 else None,
                        "limit_up": _num(f[47]) if len(f) > 47 else None,
                        "limit_down": _num(f[48]) if len(f) > 48 else None,
                        "volume_ratio": _num(f[49]) if len(f) > 49 else None,
                        # 腾讯第 36 位为成交量，单位手；项目日线口径为股。
                        "volume": _num(f[36]) * 100.0 if _num(f[36]) is not None else None,
                        "source_time": source_time,
                        "is_stale": bool(
                            price is not None
                            and preclose is not None
                            and price > 0
                            and price == preclose
                            and amount_yi == 0
                        ),
                    }
                )
            except (ValueError, IndexError):
                continue
    return pd.DataFrame(rows)


def _num(v) -> float | None:
    try:
        if v in ("", "-", "--"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "FreeDataSourceError",
    "fetch_ths_daily",
    "fetch_ths_finance",
    "fetch_tencent_quotes",
    "normalize_ticker",
    "tencent_symbol",
]
