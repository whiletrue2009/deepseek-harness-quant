# -*- coding: utf-8 -*-
"""
validation/check_survivorship.py — 幸存者偏差检查（第06课落地，待办队列 ★项）

背景：M2 股票列表来自 baostock query_stock_basic（当前在市股票），若不含退市股，
回测只用"活下来的股票"→ 收益虚高 50%+（第06课原文警示）。本脚本在 M2 完成后验收用，
也可随时运行（只读，不干扰后台下载）。

检查内容：
1. 退市股清单：本地 stock_basic.db（由 Baostock 股票基础库刷新）
2. 全市场列表（stock_basic.db 当前在市股票）是否含退市股
3. 本地缓存 bars.db 已入库代码是否含退市股
4. 结论：幸存者偏差风险评级 + M2 补拉建议

用法：
  python validation/check_survivorship.py
"""
import os
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR


def norm(code):
    """归一为 6 位数字代码（容忍 sh.600519 / 600519.SH / 600519 三种格式）"""
    s = str(code).strip().upper()
    if "." in s:
        left, right = s.split(".", 1)
        # baostock: sh.600519 / tushare: 600519.SH
        return left.zfill(6) if right in ("SH", "SZ", "BJ") else right.zfill(6)
    return s.zfill(6)


def get_delisted():
    """读本地股票基础库中的沪深退市股代码。"""
    path = Path(CACHE_DIR) / "stock_basic.db"
    if not path.exists():
        return set(), {"error": 1}, None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT code FROM stock_basic WHERE status='0' "
            "AND (code LIKE '%.SH' OR code LIKE '%.SZ')"
        ).fetchall()
    finally:
        con.close()
    return {norm(r[0].split(".")[0]) for r in rows}, {"local": len(rows)}, None


def get_recent_delisted():
    """2019 年后退市股是当前回测边界的必修补拉目标。"""
    path = Path(CACHE_DIR) / "stock_basic.db"
    if not path.exists():
        return set()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT code FROM stock_basic WHERE status='0' AND out_date>='2019-01-01' "
            "AND out_date!='' AND (code LIKE '%.SH' OR code LIKE '%.SZ')"
        ).fetchall()
    finally:
        con.close()
    return {norm(r[0].split(".")[0]) for r in rows}


def get_listed():
    """读本地股票基础库当前在市列表。"""
    path = Path(CACHE_DIR) / "stock_basic.db"
    if not path.exists():
        return None, f"股票基础库不存在: {path}"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT code FROM stock_basic WHERE status='1' "
            "AND (code LIKE '%.SH' OR code LIKE '%.SZ')"
        ).fetchall()
    finally:
        con.close()
    codes = {norm(r[0].split(".")[0]) for r in rows}
    return (codes, None) if codes else (None, "本地在市清单为空")


def get_cached_codes():
    """本地缓存已入库代码（只读）"""
    db = os.path.join(CACHE_DIR, "bars.db")
    if not os.path.exists(db):
        return None, f"缓存不存在: {db}"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT DISTINCT code FROM daily_bar").fetchall()
        return {norm(r[0].split(".")[0]) for r in rows}, None
    finally:
        con.close()


def main():
    print("=" * 62)
    print("幸存者偏差检查（第06课：无退市股 → 回测收益虚高 50%+）")
    print("=" * 62)

    # 1. 退市股清单（本地基础库，不访问网络）
    delisted, src, detail = get_delisted()
    recent_delisted = get_recent_delisted()
    print(
        f"\n[1] 本地退市股清单: {src.get('local', 0)} 只，"
        f"其中 2019 年后退市 {len(recent_delisted)} 只"
    )

    # 2. 本地当前在市列表
    listed, err = get_listed()
    if listed is None:
        print(f"[WARN] 本地在市清单读取失败: {err}（跳过 [2]）")
    else:
        print(f"[2] 本地基础库当前在市: {len(listed)} 只")
        in_listed = delisted & listed
        print(f"    其中含退市股: {len(in_listed)} 只 → {'❌ 列表污染（需清洗）' if in_listed else '✅ 列表干净（无退市股混入）'}")
        missing = delisted - listed
        print(f"    未覆盖退市股: {len(missing)} 只（如 000004 国华退 2026-07 终止上市）")

    # 3. 本地缓存
    cached, cerr = get_cached_codes()
    if cached is None:
        print(f"[3] 本地缓存: {cerr}")
        return
    print(f"[3] 本地缓存已入库: {len(cached)} 只")
    cached_delisted = delisted & cached
    cached_recent_delisted = recent_delisted & cached
    print(f"    其中退市股: {len(cached_delisted)} 只 → "
          f"{'✅ 已有退市股（M2 列表或含退市）' if cached_delisted else '⚠️ 完全不含退市股（幸存者偏差确认）'}")
    if cached_delisted:
        print(f"    示例: {sorted(cached_delisted)[:10]}")
    if recent_delisted:
        print(f"    2019 年后退市覆盖: {len(cached_recent_delisted)} / {len(recent_delisted)} 只")

    # 4. 结论
    print("\n" + "=" * 62)
    if cached and not cached_delisted:
        print("结论: ⚠️ 幸存者偏差风险【已确认】")
        print("  - 当前缓存 100% 为在市股票，退市股覆盖 0 只")
        print("  - 影响: 回测只用幸存者 → 收益虚高（第06课: 可虚高 50%+）")
        print("  - 处置: 运行 data/backfill_delisted.py 补拉 2019 年后退市股历史")
        print("    全市场初始化入口已串联该步骤：data/free_daily_pipeline.py --init")
    elif recent_delisted and len(cached_recent_delisted) < len(recent_delisted):
        print("结论: ⚠️ 幸存者偏差风险【部分缓解，未完成】")
        print(f"  - 2019 年后退市股覆盖 {len(cached_recent_delisted)} / {len(recent_delisted)} 只")
        print("  - 处置: 继续运行 data/backfill_delisted.py，完成后再做回测结论")
    elif cached_delisted:
        print("结论: ✅ 缓存含退市股，幸存者偏差风险低")
    else:
        print("结论: ⚠️ 数据不足，无法判定")
    print("=" * 62)


if __name__ == "__main__":
    main()
