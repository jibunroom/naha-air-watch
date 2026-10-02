"""data/*.json の読み書き（仕様「データ保存と重複排除」）。

GitHub Actions は実行ごとに状態が消えるので、入札監視と同じくリポジトリにコミットして残す。
保存するのは事実（価格・路線・期間・URL）だけで、記事の本文は保存しない（公開リポジトリのため）。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config

JST = timezone(timedelta(hours=9))


def now_jst() -> datetime:
    return datetime.now(JST)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _read(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class State:
    FILES = {
        "seen": {},          # 既読記事 {URLのハッシュ: 取得日時}
        "sales": [],         # 通知済みセール（同じセールを二度通知しない）
        "history": [],       # 価格履歴（v2で中央値ベースのしきい値に使う）
        "pending": [],       # AI抽出が全モデル不可で持ち越した記事
        "undelivered": [],   # メール送信に失敗した通知（送れるまで消さない）
        "health": {},        # 取得元ごとの連続失敗回数・日別の取得件数
        "last_batch": {},    # 実行済みスロット（GitHub の起動遅延対策の門番用）
    }

    def __init__(self, data_dir: Path | None = None, dry_run: bool = False):
        self.dir = Path(data_dir or config.DATA_DIR)
        self.dry_run = dry_run
        for name, default in self.FILES.items():
            setattr(self, name, _read(self.dir / f"{name}.json", json.loads(json.dumps(default))))

    @property
    def first_run(self) -> bool:
        return not self.seen

    def is_seen(self, url: str) -> bool:
        return url_hash(url) in self.seen

    def mark_seen(self, url: str) -> None:
        self.seen[url_hash(url)] = now_jst().strftime("%Y-%m-%d %H:%M")

    def trim(self, keep_days: int = 180) -> None:
        """古い既読・履歴を捨てる（リポジトリの肥大化防止）。"""
        cutoff = (now_jst() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        self.seen = {k: v for k, v in self.seen.items() if v[:10] >= cutoff}
        self.sales = [s for s in self.sales if (s.get("notified_at") or "")[:10] >= cutoff]

    def save(self) -> None:
        if self.dry_run:
            return
        self.trim()
        for name in self.FILES:
            _write(self.dir / f"{name}.json", getattr(self, name))
