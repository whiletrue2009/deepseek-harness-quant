# -*- coding: utf-8 -*-
"""macOS 免 Tushare 盘后主管线。

链路：股票基础信息 -> Baostock 日线 -> 腾讯估值快照 -> 同花顺质量表 ->
本地历史市值 -> 宏观/EPU -> 沪深300 基准 -> 择时红绿灯 ->
机会池/Pitch/三层池/今日信号/日报。
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR

CACHE = CACHE_DIR


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def run(name: str, args: list[str], timeout: int = 7200) -> int:
    log(f"开始: {name}")
    result = subprocess.run(
        args,
        cwd=BASE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout or None,
    )
    status = "完成" if result.returncode == 0 else f"失败({result.returncode})"
    log(f"{status}: {name}")
    return result.returncode


def stock_basic_exists() -> bool:
    path = CACHE / "stock_basic.db"
    if not path.exists():
        return False
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] > 0
    finally:
        con.close()


def hist_start(init_mode: bool) -> str:
    if init_mode:
        return "2020-01"
    return datetime.now().strftime("%Y-%m")


def hist_end() -> str:
    return datetime.now().strftime("%Y-%m")


def run_status() -> int:
    """汇总免费真实数据链路的关键缓存，不做网络请求。"""
    basic_db = CACHE / "stock_basic.db"
    if basic_db.exists():
        con = sqlite3.connect(basic_db, timeout=10)
        try:
            row = con.execute(
                "SELECT COUNT(*), SUM(status='1'), SUM(industry!='') FROM stock_basic"
            ).fetchone()
            print(f"股票基础信息: {row[0]} 只 / 在市 {row[1]} 只 / 含行业 {row[2]} 只")
        finally:
            con.close()
    else:
        print("股票基础信息: 缺失")

    sections = [
        ("日线", [PY, "-X", "utf8", str(BASE / "data/free_daily_loader.py"), "--status"]),
        ("退市股", [PY, "-X", "utf8", str(BASE / "data/backfill_delisted.py"), "--status"]),
        ("财务质量", [PY, "-X", "utf8", str(BASE / "data/fetch_quality_ths.py"), "--status"]),
        ("估值", [PY, "-X", "utf8", str(BASE / "data/fetch_valuation_tencent.py"), "--status"]),
        ("宏观", [PY, "-X", "utf8", str(BASE / "data/fetch_macro_cache.py"), "--status"]),
        ("EPU", [PY, "-X", "utf8", str(BASE / "factors/policy/epu_fetcher.py"), "--check"]),
    ]
    failed = []
    for name, args in sections:
        log(f"开始: {name}状态")
        result = subprocess.run(
            args, cwd=BASE, text=True, encoding="utf-8", errors="replace",
            timeout=300, capture_output=True,
        )
        output = (result.stdout or "").strip()
        if output:
            print(output)
        if result.returncode:
            failed.append(name)

    hist_db = CACHE / "hist_mv.db"
    if hist_db.exists():
        con = sqlite3.connect(hist_db, timeout=10)
        try:
            row = con.execute(
                "SELECT COUNT(DISTINCT month), COUNT(*), MIN(month), MAX(month) FROM hist_mv"
            ).fetchone()
            print(f"历史流通市值: {row[0]} 个月 / {row[1]} 行 / {row[2]} ~ {row[3]}")
        finally:
            con.close()
    else:
        print("历史流通市值: 缺失")

    finance_db = CACHE / "finance.db"
    if finance_db.exists():
        con = sqlite3.connect(finance_db, timeout=10)
        try:
            has = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='finance_report'"
            ).fetchone()
            if has:
                row = con.execute(
                    "SELECT COUNT(DISTINCT code), COUNT(*), MIN(period), MAX(period) FROM finance_report"
                ).fetchone()
                print(f"财务摘要: {row[0]} 只 / {row[1]} 期 / {row[2]} ~ {row[3]}（非 PIT）")
            else:
                print("财务摘要: 缺失")
        finally:
            con.close()
    else:
        print("财务摘要: 缺失")

    minute_db = CACHE / "minute.db"
    if minute_db.exists():
        con = sqlite3.connect(minute_db, timeout=10)
        try:
            tables = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "minute_1m" in tables:
                row = con.execute(
                    "SELECT COUNT(DISTINCT code), COUNT(*), MIN(trade_time), MAX(trade_time) FROM minute_1m"
                ).fetchone()
                print(f"分钟数据: {row[0]} 只 / {row[1]} 行 / {row[2]} ~ {row[3]}")
            else:
                print("分钟数据: 库存在但无 minute_1m 表")
        finally:
            con.close()
    else:
        print("分钟数据: 缺失（盘中/竞价链路降级）")

    reports = sorted((BASE / "report").glob("daily_report_*.md"), key=lambda p: p.stat().st_mtime)
    if reports:
        print(f"最新日报: {reports[-1].name}")
    else:
        print("最新日报: 缺失")
    if failed:
        log("状态检查失败: " + "、".join(failed))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="免 Tushare 盘后主管线")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="汇总关键数据缓存覆盖")
    mode.add_argument("--init", action="store_true", help="按股票基础库初始化全市场日线")
    mode.add_argument("--sample", type=int, help="只处理当前 bars 中前 N 只，用于体验验证")
    parser.add_argument("--refresh-basic", action="store_true", help="强制刷新股票基础信息")
    parser.add_argument("--workers", type=int, default=2, help="日线初始化并发进程数；Baostock 建议 <=2")
    parser.add_argument("--start-date", default="2019-01-01")
    parser.add_argument("--skip-quality", action="store_true")
    parser.add_argument("--skip-valuation", action="store_true")
    parser.add_argument("--skip-hist-mv", action="store_true")
    parser.add_argument(
        "--skip-delisted",
        action="store_true",
        help="初始化时跳过 2019 年后退市股补拉（日常增量默认不拉退市股）",
    )
    parser.add_argument("--skip-refresh", action="store_true", help="只更新数据，不重跑因子输出")
    args = parser.parse_args()

    if args.status:
        return run_status()
    failed = []
    # --init 代表全市场初始化；旧库可能是小样本，必须先刷新股票清单。
    refresh_basic = args.init or args.refresh_basic or not stock_basic_exists()
    if refresh_basic:
        if run("股票基础信息", [PY, "-X", "utf8", str(BASE / "data/fetch_stock_basic.py")], 600):
            failed.append("股票基础信息")

    baostock_args = [
        PY, "-X", "utf8", str(BASE / "data/free_daily_loader.py"),
        "--source", "baostock", "--rate", "0.04",
        "--workers", str(args.workers),
    ]
    init_mode = args.init or args.sample is not None
    daily_failed = False
    if args.sample is not None:
        baostock_args += ["--init", "--from-bars", "--limit", str(args.sample)]
        daily_failed = bool(run("Baostock 日线", baostock_args, 0 if args.init else 7200))
    elif args.init:
        baostock_args += ["--init", "--start-date", args.start_date]
        daily_failed = bool(run("Baostock 日线", baostock_args, 0))
    else:
        # 盘后日常增量优先使用腾讯批量快照；失败时退回 Baostock 逐股增量。
        if run("腾讯收盘日线", [PY, "-X", "utf8", str(BASE / "data/fetch_daily_tencent.py")], 600):
            baostock_args.append("--incremental")
            daily_failed = bool(run("Baostock 日线增量", baostock_args, 7200))
    if daily_failed:
        failed.append("Baostock 日线")

    if args.init and not args.skip_delisted and not daily_failed:
        if run("退市股补拉", [PY, "-X", "utf8", str(BASE / "data/backfill_delisted.py")], 0):
            failed.append("退市股补拉")

    if not args.skip_valuation and run(
        "腾讯估值快照",
        [PY, "-X", "utf8", str(BASE / "data/fetch_valuation_tencent.py")],
        600,
    ):
        failed.append("腾讯估值快照")

    if not args.skip_quality:
        quality_args = [
            PY, "-X", "utf8", str(BASE / "data/fetch_quality_ths.py"),
            "--run", "--quarters", "10", "--rate", "0.10",
            "--workers", str(args.workers),
        ]
        if args.sample is not None:
            quality_args += ["--limit", str(args.sample)]
        if run("同花顺财务质量", quality_args, 0):
            failed.append("同花顺财务质量")

    if not args.skip_hist_mv:
        hist_args = [
            PY, "-X", "utf8", str(BASE / "data/build_hist_mv.py"),
            "--start", hist_start(init_mode), "--end", hist_end(),
        ]
        if args.init:
            # 旧样本库的月份断点会导致全市场月份被跳过，初始化必须覆盖补齐。
            hist_args.append("--force")
        if run("历史流通市值", hist_args, 1800):
            failed.append("历史流通市值")

    if run("沪深300基准", [PY, "-X", "utf8", str(BASE / "data/fetch_index_daily.py"), "--days", "30"], 300):
        failed.append("沪深300基准")
    if run("宏观与EPU", [PY, "-X", "utf8", str(BASE / "data/fetch_macro_cache.py")], 600):
        failed.append("宏观与EPU")
    if run("EPU指数", [PY, "-X", "utf8", str(BASE / "factors/policy/epu_fetcher.py")], 300):
        failed.append("EPU指数")
    if run("择时红绿灯", [PY, "-X", "utf8", str(BASE / "data/traffic_light.py")], 300):
        failed.append("择时红绿灯")
    if not args.skip_refresh and run(
        "盘后因子刷新", [PY, "-X", "utf8", str(BASE / "data/refresh_after_data.py")], 7200
    ):
        failed.append("盘后因子刷新")

    if failed:
        log("管线失败: " + "、".join(failed))
        return 1
    log("管线完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
