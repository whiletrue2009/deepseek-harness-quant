# -*- coding: utf-8 -*-
"""今日信号聚合器。

只消费已经生成的择时、三层池、机会池与 Pitch 文件，不创建第二套选股逻辑。
买入清单始终来自 pool_layers.decision（L2/L3 决策层），信号生成后按 T+1 口径执行。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR

OUTPUT = BASE / "output"
LOGS = BASE / "logs"


def _latest(pattern: str, root: Path = OUTPUT):
    files = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _read(path):
    if not path:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cash_from_timing(timing: dict, override=None) -> float:
    if override is not None:
        return float(override)
    level = str(timing.get("level", ""))
    score = timing.get("score")
    if "不适合" in level or (isinstance(score, (int, float)) and score < 45):
        return 0.5
    if "谨慎" in level or not isinstance(score, (int, float)):
        return 0.3
    return 0.0


def _quality_is_missing() -> bool:
    path = CACHE_DIR / "finance_quality.db"
    return not path.exists() or path.stat().st_size == 0


def _rel(path):
    return str(path.relative_to(BASE)) if path else ""


def build(override_cash=None) -> dict:
    timing_path = _latest("timing_system_*.json")
    pool_path = _latest("pool_layers_*.json")
    opp_path = _latest("opp_pool_*.json", LOGS)
    pitch_path = _latest("pitch_v2_*.json", LOGS)

    timing = _read(timing_path)
    pool = _read(pool_path)
    opp = _read(opp_path)
    pitch = _read(pitch_path)
    cash = _cash_from_timing(timing, override_cash)

    date = pool.get("date") or opp.get("date") or timing.get("date") or ""
    decision = pool.get("decision") or []
    if cash >= 0.5:
        decision = []
    codes = [x.get("code") for x in decision if x.get("code")]

    warnings = []
    if not timing:
        warnings.append("缺少 timing_system，先运行 factors/policy/timing_system.py")
    if not pool:
        warnings.append("缺少 pool_layers，先运行 strategy/pool_layers.py")
    if not opp:
        warnings.append("缺少 opp_pool，先运行 factors/opportunities/scan.py --pitch")
    if not pitch:
        warnings.append("缺少 pitch_v2，先运行 factors/opportunities/pitch_v2.py")
    if _quality_is_missing():
        warnings.append("finance_quality.db 为空或缺失，质量风险显示 NO_DATA")

    return {
        "date": date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regime_label": timing.get("level", "NO_DATA"),
        "regime_level": "exit" if cash >= 0.5 else ("half" if cash >= 0.3 else "full"),
        "regime_cash_ratio": cash,
        "n_passed": len(pool.get("watch") or []),
        "n_passed_scope": "L1观察池（当前样本模式非全市场硬过滤）",
        "codes": codes,
        "hold_plan": decision,
        "advice": "T+1 开盘按 L2 决策卡执行；无决策卡时保留现金，不随机补票。"
                  if decision else "今日无 L2 决策卡，保留现金/维持既有持仓等待。",
        "gate": {
            "ok": bool(timing and pool),
            "reason": "" if timing and pool else "核心上游产物缺失",
        },
        "timing": {
            "score": timing.get("score"),
            "level": timing.get("level"),
            "reason": timing.get("reason", ""),
            "file": timing_path.name if timing_path else "",
        },
        "counts": {
            "opportunities": len(opp.get("opportunities") or []),
            "pitch": len(pitch.get("pitch") or []),
            "watch": len(pool.get("watch") or []),
            "candidate": len(pool.get("candidate") or []),
            "decision": len(decision),
        },
        "sources": {
            "timing": _rel(timing_path),
            "pool_layers": _rel(pool_path),
            "opportunity_pool": _rel(opp_path),
            "pitch_v2": _rel(pitch_path),
        },
        "warnings": warnings,
        "execution": "收盘后生成，下一交易日开盘执行；一字板无法买入时不追单。",
    }


def main():
    ap = argparse.ArgumentParser(description="聚合今日择时与 L2 决策信号")
    ap.add_argument("--regime-cash", type=float, default=None,
                    help="手工指定防守现金比例；默认从 timing_system level 推导")
    args = ap.parse_args()
    data = build(args.regime_cash)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT / f"daily_signal_{ts}.json"
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    (OUTPUT / "daily_signal.json").write_text(payload, encoding="utf-8")
    print(f"今日信号已生成：{path}")
    print(f"数据日 {data['date']} | 择时 {data['regime_label']} | "
          f"现金 {data['regime_cash_ratio']:.0%} | 决策 {len(data['hold_plan'])} 只")
    for warning in data["warnings"]:
        print(f"警告：{warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
