"""実行スロット判定（GitHub schedule の遅延・欠落対策）。

GitHub の schedule は「頼んだ回数」も「頼んだ時刻」も守らない。実測では
20分おき（1日72回）を要求しても **1日2回程度**しか起動せず、時刻もバラバラ
だった（08/02 16:41・21:16、08/04 01:42・05:46 JST）。

そこで「来た起動は絶対に捨てない」方針にする:

  スロット境界 … morning = JST 07:00（仕様）/ evening = JST 18:00（逃さないための追加）
  起動が来たら … 過ぎている境界のうち「まだ実行していない最新のもの」を実行

深夜01:42に起動が来ても、前日の evening が未実行ならそれを消化する。
遅れて届くことはあっても、届かないことは無くなる。

実行済みは data/last_batch.json に {"morning": "YYYY-MM-DD", ...} で記録。
消化時は「過ぎている境界」をまとめて既済にする（パイプラインは新着を
すべて拾うので、1回走れば溜まっていた分は全部片付くため）。

※ workflow の gate ステップから `python3 -m src.slots` として呼ばれる。
   pip install 前に動く必要があるため標準ライブラリのみ使う。
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
SLOT_TIMES = {"morning": time(7, 0), "evening": time(18, 0)}
LOOKBACK_DAYS = 1   # 何日前の取りこぼしまで拾うか

LAST_BATCH_PATH = Path(__file__).resolve().parent.parent / "data" / "last_batch.json"


def passed_boundaries(now: datetime) -> list[tuple[str, str]]:
    """今より前に過ぎたスロット境界を新しい順に返す [(slot, "YYYY-MM-DD"), ...]。"""
    out = []
    for back in range(LOOKBACK_DAYS + 1):
        day = (now - timedelta(days=back)).date()
        for slot, t in SLOT_TIMES.items():
            boundary = datetime.combine(day, t, tzinfo=JST)
            if boundary <= now:
                out.append((boundary, slot, day.isoformat()))
    out.sort(key=lambda x: x[0], reverse=True)
    return [(slot, d) for _, slot, d in out]


def due_slot(now: datetime, last_batch: dict) -> tuple[str, str] | None:
    """実行すべき (slot, 日付)。すべて消化済みなら None。"""
    for slot, day in passed_boundaries(now):
        # 記録が同日以降なら消化済み。== ではなく >= で見ないと、
        # 前日の同じスロットが毎回未消化に見えて無限に再実行される
        if last_batch.get(slot, "") < day:
            return slot, day
    return None


def mark_done(now: datetime, last_batch: dict) -> dict:
    """過ぎている境界をまとめて既済にする（1回走れば全部片付くため）。"""
    for slot, day in passed_boundaries(now):
        if last_batch.get(slot, "") < day:
            last_batch[slot] = day
    return last_batch


def load_last_batch(path: Path | None = None) -> dict:
    p = path or LAST_BATCH_PATH
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    due = due_slot(datetime.now(JST), load_last_batch())
    print(f"due={due[0] + ':' + due[1] if due else 'none'}")


if __name__ == "__main__":
    main()


def current_slot(now: datetime) -> str | None:
    """今の時刻が属するスロット（月曜の稼働確認メールを朝の回だけ送るため）。"""
    t = now.astimezone(JST).time()
    if t >= SLOT_TIMES["evening"]:
        return "evening"
    if t >= SLOT_TIMES["morning"]:
        return "morning"
    return None
