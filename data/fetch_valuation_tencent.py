# -*- coding: utf-8 -*-
"""腾讯估值快照落地器。

网络层只负责把当日 PE/PB/市值写入 finance.db.valuation；因子与回测仍只读
本地缓存，不在扫描时直接请求腾讯。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR
from data.fetcher_free import fetch_tencent_quotes

FIN_DB = CACHE_DIR / "finance.db"
STOCK_DB = CACHE_DIR / "stock_basic.db"


def load_universe(limit: int | None = None) -> list[str]:
    con = sqlite3.connect(STOCK_DB, timeout=10)
    try:
        sql = (
            "SELECT code FROM stock_basic WHERE status='1' "
            "AND (code LIKE '%.SH' OR code LIKE '%.SZ' OR code LIKE '%.BJ') "
            "ORDER BY code"
        )
        if limit is not None:
            sql += " LIMIT ?"
            rows = con.execute(sql, (limit,)).fetchall()
        else:
            rows = con.execute(sql).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def ensure_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS valuation (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            pe REAL,
            pe_ttm REAL,
            pb REAL,
            total_mv REAL,
            circ_mv REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (code, trade_date)
        )
        """
    )
    con.commit()


def store(df: pd.DataFrame) -> tuple[int, str]:
    dates = df.get("source_time", pd.Series(dtype="object")).dropna().astype(str)
    trade_date = (
        max(x[:10].replace("/", "-") for x in dates if len(x) >= 10)
        if not dates.empty
        else datetime.now().strftime("%Y-%m-%d")
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in df.to_dict("records"):
        code = str(r.get("code") or "")
        if not code:
            continue
        # 项目旧估值表单位为万元；腾讯适配层输出为亿元。
        total_yi = r.get("total_mcap_yi")
        circ_yi = r.get("float_mcap_yi")
        rows.append(
            (
                code,
                trade_date,
                r.get("pe_ttm"),
                r.get("pe_ttm"),
                r.get("pb"),
                None if total_yi is None or pd.isna(total_yi) else float(total_yi) * 10000.0,
                None if circ_yi is None or pd.isna(circ_yi) else float(circ_yi) * 10000.0,
                "tencent",
                now,
            )
        )
    con = sqlite3.connect(FIN_DB, timeout=30)
    try:
        ensure_table(con)
        con.executemany(
            """
            INSERT OR REPLACE INTO valuation
            (code, trade_date, pe, pe_ttm, pb, total_mv, circ_mv, source, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        con.commit()
    finally:
        con.close()
    return len(rows), trade_date


def status() -> int:
    con = sqlite3.connect(FIN_DB, timeout=10)
    try:
        ensure_table(con)
        row = con.execute(
            """
            SELECT trade_date, COUNT(*), COUNT(DISTINCT code), SUM(pb IS NOT NULL),
                   SUM(total_mv IS NOT NULL)
            FROM valuation GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1
            """
        ).fetchone()
    finally:
        con.close()
    if not row:
        print("估值缓存: 空")
    else:
        print(
            f"估值缓存: {row[0]} | {row[1]} 行 / {row[2]} 只 | "
            f"PB {row[3]} | 总市值 {row[4]}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="腾讯估值快照写入本地 finance.db")
    parser.add_argument("--limit", type=int, default=None, help="只刷新前 N 只（链路测试）")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        return status()
    codes = load_universe(args.limit)
    print(f"腾讯估值快照: 待拉 {len(codes)} 只", flush=True)
    t0 = time.time()
    df = fetch_tencent_quotes(codes)
    if df.empty:
        print("腾讯估值快照失败: 无有效响应", flush=True)
        return 1
    n, trade_date = store(df)
    print(
        f"腾讯估值快照完成: {n} 只 | 数据日 {trade_date} | "
        f"耗时 {time.time() - t0:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
