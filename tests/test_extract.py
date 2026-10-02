"""抽出（Gemini 呼び出しは差し替え。実APIは一度も叩かない）。"""
import json

import pytest

from src import config
from src.extract import (
    AllModelsUnavailable,
    Extractor,
    classify_error,
    normalize,
)
from src.fetch import Article, focus_text, parse_skymark_csv


@pytest.fixture
def settings():
    s = config.load_settings()
    s["gemini"]["min_interval_sec"] = 0
    return s


def art():
    return Article(source="LCCjp", title="テスト", url="https://x.jp/1",
                   published="2026-10-01", body="本文")


def ok_json(**over):
    d = {"is_sale": True, "airline": "タイガーエア台湾", "okinawa_included": "yes",
         "fares": [{"origin": "OKA", "destination": "TPE", "price": 8500,
                    "currency": "JPY", "tax": "税別", "trip": "片道"}]}
    d.update(over)
    return json.dumps(d, ensure_ascii=False)


# --- 正規化（誤抽出は捨てるが、黙って消さず warnings に残す） ---


def test_keeps_okinawa_fares_and_drops_others():
    d, w = normalize({"is_sale": True, "fares": [
        {"origin": "OKA", "destination": "TPE", "price": 8500},
        {"origin": "KIX", "destination": "TPE", "price": 9500},
    ]})
    assert [(f["origin"], f["destination"]) for f in d["fares"]] == [("OKA", "TPE")]
    assert any("沖縄発着でない" in x for x in w)


def test_reverse_direction_is_flipped_to_naha_departure():
    d, _ = normalize({"is_sale": True, "fares": [
        {"origin": "ICN", "destination": "OKA", "price": 23370}]})
    assert (d["fares"][0]["origin"], d["fares"][0]["destination"]) == ("OKA", "ICN")


@pytest.mark.parametrize("bad", [0, 50, "不明", None, 99_999_999, True])
def test_bad_price_is_rejected(bad):
    d, w = normalize({"is_sale": True, "fares": [
        {"origin": "OKA", "destination": "TPE", "price": bad}]})
    assert d["fares"] == []
    assert w


def test_price_strings_are_parsed():
    d, _ = normalize({"is_sale": True, "fares": [
        {"origin": "OKA", "destination": "ISG", "price": "5,720円"}]})
    assert d["fares"][0]["price"] == 5720


def test_trip_and_tax_are_validated():
    d, _ = normalize({"is_sale": True, "fares": [
        {"origin": "OKA", "destination": "KIX", "price": 10000, "trip": "往復", "tax": "税別"},
        {"origin": "OKA", "destination": "FUK", "price": 9000, "trip": "?", "tax": "?"}]})
    assert d["fares"][0]["trip"] == "往復" and d["fares"][0]["tax"] == "税別"
    assert d["fares"][1]["trip"] == "不明" and d["fares"][1]["tax"] == "不明"


def test_missing_required_key_raises():
    with pytest.raises(ValueError):
        normalize({"airline": "x"})


# --- エラーの分類（実際に返ってきたメッセージで確認） ---


@pytest.mark.parametrize("msg,kind", [
    ("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier", "daily_quota"),
    ("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerMinutePerProjectPerModel", "busy"),
    ("503 UNAVAILABLE This model is currently experiencing high demand", "busy"),
    ("404 NOT_FOUND This model models/gemini-2.5-flash is no longer available", "gone"),
    ("400 INVALID_ARGUMENT", "fatal"),
])
def test_classify_error(msg, kind):
    assert classify_error(RuntimeError(msg)) == kind


# --- モデルの切り替え（大事なセールを逃さないための要） ---


def test_daily_quota_switches_to_next_model_immediately(settings):
    calls = []

    def caller(model, system, user):
        calls.append(model)
        if model == "m1":
            raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
        return ok_json()

    ex = Extractor(settings, caller, models=["m1", "m2"], sleeper=lambda s: None)
    r = ex.extract(art())
    assert r.ok
    assert calls == ["m1", "m2"], "1日の上限なら待たずに次のモデルへ"
    assert ex.unavailable == {"m1": "daily_quota"}


def test_busy_model_is_retried_then_abandoned(settings):
    calls, slept = [], []

    def caller(model, system, user):
        calls.append(model)
        if model == "m1":
            raise RuntimeError("503 UNAVAILABLE")
        return ok_json()

    ex = Extractor(settings, caller, models=["m1", "m2"], sleeper=slept.append)
    assert ex.extract(art()).ok
    assert calls == ["m1", "m1", "m1", "m2"]
    assert slept[:2] == [8, 16]


def test_unavailable_model_is_skipped_for_rest_of_run(settings):
    calls = []

    def caller(model, system, user):
        calls.append(model)
        if model == "m1":
            raise RuntimeError("404 NOT_FOUND")
        return ok_json()

    ex = Extractor(settings, caller, models=["m1", "m2"], sleeper=lambda s: None)
    ex.extract(art())
    ex.extract(art())
    assert calls == ["m1", "m2", "m2"], "2記事目は廃止モデルを試さない"


def test_all_models_unavailable_is_raised_not_swallowed(settings):
    """全モデル不可は記事の失敗ではない。呼び出し元が残りを次回へ持ち越せるよう上に投げる。"""
    def caller(model, system, user):
        raise RuntimeError("429 RESOURCE_EXHAUSTED PerDay")

    ex = Extractor(settings, caller, models=["m1", "m2"], sleeper=lambda s: None)
    with pytest.raises(AllModelsUnavailable):
        ex.extract(art())


def test_broken_json_is_resent_once_then_kept_as_failure(settings):
    """2回とも壊れていても記事は捨てない（ok=False で返り、後段で「要確認」通知）。"""
    calls = []

    def caller(model, system, user):
        calls.append(user)
        return "すみません"

    ex = Extractor(settings, caller, models=["m1"], sleeper=lambda s: None)
    r = ex.extract(art())
    assert not r.ok and r.article.url == "https://x.jp/1"
    assert len(calls) == 2 and "JSONとして不正" in calls[1]


# --- 長い記事の削り方（表の下の那覇路線を落とさない） ---


def test_focus_text_keeps_okinawa_lines_beyond_the_head():
    lines = [f"関係ない行{i} " + "あ" * 40 for i in range(300)]
    lines[250] = "那覇－台北 片道8,500円"
    text = "\n".join(lines)
    out = focus_text(text, 3000)
    assert len(out) <= 3000
    assert "那覇－台北 片道8,500円" in out


def test_focus_text_leaves_short_text_alone():
    assert focus_text("短い本文", 3000) == "短い本文"


# --- スカイマーク運賃CSV（2026-10-01 に取得した実データの形） ---


def test_parse_skymark_csv():
    text = "﻿cts,,ibr,\"8,000\",\"9,900\",増便\noka,,hnd,\"9,100\",\"12,100\",\noka,,shi,\"4,100\",\"6,000\",\n"
    rows = parse_skymark_csv(text)
    assert rows[1] == {"origin": "OKA", "via": None, "destination": "HND",
                       "imatoku": 9100, "tasutoku": 12100, "note": None}
    assert rows[2]["imatoku"] == 4100


def test_timeout_counts_as_busy():
    """応答の返らない呼び出しは「混雑」扱い＝次のモデルへ（2026-10-02 に22分止まった実例）。"""
    class ReadTimeout(Exception):
        pass

    class ConnectTimeout(Exception):
        pass

    assert classify_error(ReadTimeout("timed out")) == "busy"
    assert classify_error(ConnectTimeout("")) == "busy"
