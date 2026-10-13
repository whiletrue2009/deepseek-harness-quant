# -*- coding: utf-8 -*-
"""data/refresh_after_data.py — 数据落地后全系统刷新链。

默认免 Tushare 链路：因子池评分补跑 → 机会池 → Pitch → 科技池 → 远期池 →
突破监控 → 三层池/信号 → 市场知识库 → 自动日报。
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
POOL_DIR = BASE / "data" / "factorpool"
POOL_PY = POOL_DIR / "core" / "scheduler.py"

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def run(name, args, cwd=None, timeout=3600):
    log(f"▶ {name}")
    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", cwd=cwd or str(BASE))
        tail = (r.stdout or "").strip().splitlines()
        log(f"  结束 {time.time()-t0:.0f}s | {tail[-1][:80] if tail else '无输出'}" + (f" | 错误: {str(r.stderr)[-120:]}" if r.returncode else ""))
        return r.returncode
    except subprocess.TimeoutExpired:
        log(f"  ⚠ 超时 {timeout}s")
        return -1
    except Exception as e:
        log(f"  ✗ 异常: {str(e)[:100]}")
        return -2


def latest_pitch_file():
    files = sorted((BASE / "logs").glob("pitch_v2_*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main():
    failed = []
    log("=== 数据落地刷新链启动 ===")
    # 1) 因子池评分补跑（C5 连续验证第 3 天：bars 已到 08-10 → scheduler 幂等产出）
    if POOL_PY.exists():
        rc = run("因子池评分补跑（scheduler daily）",
                 [PY, "-X", "utf8", str(POOL_PY), "daily"], cwd=str(POOL_DIR), timeout=1800)
        if rc:
            failed.append("因子池评分补跑")
    else:
        log("跳过外包因子池（data/factorpool 不存在）")
    # 2) 机会池全类型扫描（08-10）
    rc = run("机会池扫描（全 7 类 + Pitch 门槛）",
             [PY, "-X", "utf8", str(BASE / "factors/opportunities/scan.py"), "--pitch"], timeout=3600)
    if rc:
        failed.append("机会池扫描")
    # 3) Pitch v2（审批清单刷新）
    rc = run("Pitch v2",
             [PY, "-X", "utf8", str(BASE / "factors/opportunities/pitch_v2.py")], timeout=3600)
    if rc:
        failed.append("Pitch v2")
    pitch_file = latest_pitch_file()
    if pitch_file:
        rc = run("远期池入池",
                 [PY, "-X", "utf8", str(BASE / "factors/opportunities/pitch_track.py"),
                  "--append", str(pitch_file)], timeout=600)
        if rc:
            failed.append("远期池入池")
    else:
        log("未找到 Pitch v2 输出，跳过远期池入池")
        failed.append("远期池入池")
    # 4) 科技池
    rc = run("科技池",
             [PY, "-X", "utf8", str(BASE / "factors/opportunities/tech_pitch_v3.py")], timeout=3600)
    if rc:
        failed.append("科技池")
    # 5) 远期池（T+1 填充 08-10）
    rc = run("远期池 T+1 填充",
             [PY, "-X", "utf8", str(BASE / "factors/opportunities/pitch_track.py")], timeout=1800)
    if rc:
        failed.append("远期池 T+1 填充")
    # 6) 突破监控
    rc = run("突破监控",
             [PY, "-X", "utf8", str(BASE / "factors/opportunities/breakout_monitor.py")], timeout=1800)
    if rc:
        failed.append("突破监控")
    # 7) 择时 + 三层池 + 今日信号（观察池/决策池）
    rc = run("新择时系统",
             [PY, "-X", "utf8", str(BASE / "factors/policy/timing_system.py")], timeout=300)
    if rc:
        failed.append("新择时系统")
    cash = _regime_cash_from_timing()
    rc = run("三层池",
             [PY, "-X", "utf8", str(BASE / "strategy/pool_layers.py"), "--n", "100", "--capital", "200000",
              "--regime-cash", str(cash)],
             timeout=1800)
    if rc:
        failed.append("三层池")
    rc = run("今日信号",
             [PY, "-X", "utf8", str(BASE / "report/daily_signal.py")], timeout=1800)
    if rc:
        failed.append("今日信号")
    rc = run("市场知识库",
             [PY, "-X", "utf8", str(BASE / "data/build_market_kb.py"), "--dump"],
             timeout=300)
    if rc:
        failed.append("市场知识库")
    rc = run("自动日报",
             [PY, "-X", "utf8", str(BASE / "data/daily_report_auto.py")], timeout=300)
    if rc:
        failed.append("自动日报")
    if failed:
        log(f"=== 刷新链失败：{('、'.join(failed))} ===")
        return 1
    log("=== 刷新链完成 ===")
    return 0


def _regime_cash_from_timing() -> float:
    """从最新择时结果推导防守现金比例，避免刷新链写死仓位。"""
    import json
    files = sorted((BASE / "output").glob("timing_system_*.json"),
                   key=lambda p: p.stat().st_mtime)
    try:
        level = str(json.loads(files[-1].read_text(encoding="utf-8")).get("level", ""))
    except Exception:
        return 0.3
    if "不适合" in level:
        return 0.5
    if "谨慎" in level:
        return 0.3
    return 0.0

if __name__ == "__main__":
    sys.exit(main())
