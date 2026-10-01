"""設定ファイル（config/*.yml）と .env の読み込み。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def _load_yaml(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings(path: Path | None = None) -> dict:
    return _load_yaml(path or CONFIG_DIR / "settings.yml")


def load_sources(path: Path | None = None) -> list[dict]:
    data = _load_yaml(path or CONFIG_DIR / "sources.yml")
    return data.get("sources", []) if isinstance(data, dict) else []


def load_env() -> None:
    """.env があれば読む（GitHub Actions では Secrets が環境変数で入る）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return v if v not in (None, "") else default
