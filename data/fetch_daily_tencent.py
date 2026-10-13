# -*- coding: utf-8 -*-
"""腾讯收盘快照日线落地器。

历史日线仍以 Baostock 初始化为准；本模块只服务盘后当日增量，用批量快照
替代 5000+ 次单股请求。写入保留 source='tencent'，便于后续审计混合来源。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR, DailyCache
from data.fetcher_free import fetch_tencent_quotes

STOCK_DB = CACHE_DIR / "stock_basic.db"


def load_universe() -> list[str]:
    """默认只写沪深 A 股；北交所历史口径另有专门链路。"""
    con = sqlite3.connect(STOCK_DB, timeout=10)
    try:
        return [
            r[0]
            for r in con.execute(
                """
                SELECT code FROM stock_basic
                WHERE status='1' AND (code LIKE '%.SH' OR code LIKE '%.SZ')
                ORDER BY code
                """
            ).fetchall()
        ]
    finally:
        con.close()


def run(force: bool) -> int:
    codes = load_universe()
    quotes = fetch_tencent_quotes(codes)
    if quotes.empty:
        raise RuntimeError("腾讯收盘快照为空")

    dates = quotes["source_time"].dropna().astype(str).str[:10]
    trade_date, date_count = Counter(dates).most_common(1)[0]
    closed = quotes["source_time"].dropna().astype(str).str[11:] >= "15:00:00"
    if not force and (date_count < max(100, int(len(codes) * 0.8)) or not bool(closed.any())):
        raise RuntimeError(
            f"快照未达到收盘写入条件: date={trade_date}, n={date_count}, "
            f"closed_snapshots={int(closed.sum())}"
        )

    quotes = quotes[
        quotes["source_time"].astype(str).str.startswith(trade_date)
        & quotes["price"].notna()
        & (quotes["price"] > 0)
    ].copy()
    if quotes.empty:
        raise RuntimeError("腾讯收盘快照没有有效价格")

    out = pd.DataFrame(
        {
            "code": quotes["code"],
            "date": trade_date,
            "open": quotes["open"],
            "high": quotes["high"],
            "low": quotes["low"],
            "close": quotes["price"],
            "preclose": quotes["preclose"],
            "volume": quotes.get("volume"),
            "amount": quotes["amount_yi"] * 1e8,
            "turn": quotes["turn"],
            "pct_chg": quotes["pct_chg"],
            "is_st": quotes["name"].astype(str).str.upper().str.contains("ST").astype(int),
        }
    ).dropna(subset=["open", "high", "low", "close"])

    cache = DailyCache()
    n = cache.put_daily_batch(out, adjust="qfq", source="tencent")
    print(
        f"腾讯收盘日线完成: {trade_date}，写入 {n} 行 / {out['code'].nunique()} 只；"
        f"快照时间示例 {quotes['source_time'].iloc[0]}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="腾讯收盘快照写入 daily_bar")
    parser.add_argument("--force", action="store_true", help="样本不足时仍写入，仅用于诊断")
    args = parser.parse_args()
    try:
        return run(args.force)
    except Exception as exc:
        print(f"腾讯收盘日线失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
