"""判定（仕様「判定テストケース」を含む）。"""
from datetime import datetime

import pytest

from src.judge import (
    ALL_ROUTES, CHEAP, INSTANT, JST, Deal, Rules, judge, peaks_in_travel, recommend_major,
)

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=JST)


@pytest.fixture(scope="module")
def rules():
    return Rules.load()


def deal(rules, airline, dest, price, **kw):
    name, info = rules.airline_info(airline)
    return Deal(airline=name, airline_type=(info or {}).get("type", kw.pop("type", "不明")),
                origin="OKA", destination=dest, price=price, **kw)


# --- 仕様の判定テストケース（9〜10月に実在したセール） ---


def test_spec_case1_tigerair_taipei_8500_with_gw_obon(rules):
    d = judge(deal(rules, "タイガーエア・台湾", "TPE", 8500, tax="税別",
                   travel_start="2027-03-28", travel_end="2027-10-30"), rules, NOW)
    assert d.rank == INSTANT
    assert "GW" in d.reason and "お盆" in d.reason


def test_spec_case2_jetstar_kaohsiung_6990(rules):
    d = judge(deal(rules, "ジェットスター・ジャパン", "KHH", 6990, tax="税別"), rules, NOW)
    assert d.rank == INSTANT


def test_spec_case3_airasia_bangkok_12990(rules):
    d = judge(deal(rules, "エアアジア", "BKK", 12990), rules, NOW)
    assert d.rank == INSTANT


def test_spec_case4_ana_ishigaki_5720_is_standard(rules):
    # 実際の ANA タイムセールは搭乗期間に年末年始を含むが、定番は繁忙期でも上げない
    d = judge(deal(rules, "ANA", "ISG", 5720,
                   travel_start="2026-10-25", travel_end="2027-02-28"), rules, NOW)
    assert d.rank is None


def test_spec_case5_trinity_seoul_23370_not_notified(rules):
    d = judge(deal(rules, "トリニティ航空", "ICN", 23370, tax="税込",
                   travel_start="2026-10-01", travel_end="2027-03-27"), rules, NOW)
    assert d.rank is None


# --- しきい値と補正 ---


@pytest.mark.parametrize("price,rank", [(4000, INSTANT), (4001, CHEAP), (5000, CHEAP), (5001, None)])
def test_domestic_lcc_thresholds(rules, price, rank):
    assert judge(deal(rules, "ピーチ", "KIX", price, tax="税込"), rules, NOW).rank == rank


@pytest.mark.parametrize("price,rank", [(4000, INSTANT), (4999, CHEAP), (5000, None)])
def test_island_is_strictly_under_5000(rules, price, rank):
    assert judge(deal(rules, "ANA", "ISG", price, tax="税込"), rules, NOW).rank == rank


def test_major_domestic_instant_only_with_peak(rules):
    plain = judge(deal(rules, "JAL", "HND", 9800, tax="税込",
                       travel_start="2026-10-10", travel_end="2026-11-30"), rules, NOW)
    peak = judge(deal(rules, "JAL", "HND", 9800, tax="税込",
                      travel_start="2026-12-01", travel_end="2027-01-10"), rules, NOW)
    assert plain.rank == CHEAP
    assert peak.rank == INSTANT and "年末年始" in peak.reason


def test_peak_excluded_period_is_not_counted(rules):
    d = deal(rules, "JAL", "HND", 9800, tax="税込", travel_start="2026-12-01",
             travel_end="2027-01-31",
             excluded_periods=[{"start": "2026-12-26", "end": "2027-01-05"}])
    assert peaks_in_travel(d, rules) == []


def test_tax_excluded_adds_estimate(rules):
    # 台湾 10,000円以下が「安い」。税別 9,000 + 1,500 = 10,500 で基準外
    assert judge(deal(rules, "タイガーエア台湾", "TPE", 9000, tax="税別"), rules, NOW).rank is None
    # 税込なら同じ金額で安い
    assert judge(deal(rules, "タイガーエア台湾", "TPE", 9000, tax="税込"), rules, NOW).rank == CHEAP


def test_unknown_tax_is_treated_as_included(rules):
    """逃さない優先: 税込か不明なら足さない。"""
    assert judge(deal(rules, "タイガーエア台湾", "TPE", 9000, tax="不明"), rules, NOW).rank == CHEAP


def test_round_trip_is_halved(rules):
    d = judge(deal(rules, "ピーチ", "FUK", 9000, trip="往復", tax="税込"), rules, NOW)
    assert d.effective_price == 4500 and d.rank == CHEAP
    assert "往復9,000円" in d.reason


def test_narita_is_separate_from_haneda(rules):
    assert rules.dest_name("NRT") == "成田"
    assert judge(deal(rules, "ジェットスター", "NRT", 4500, tax="税込"), rules, NOW).category == "国内LCC"


def test_unknown_destination_is_not_notified(rules):
    d = judge(deal(rules, "ピーチ", "SDJ", 1000, tax="税込"), rules, NOW)
    assert d.rank is None and "対象外の行き先" in d.reason


def test_expired_sale_is_dropped(rules):
    d = judge(deal(rules, "ピーチ", "KIX", 3000, tax="税込",
                   booking_end="2026-10-01T23:59"), rules, NOW)
    assert d.rank is None and "終了" in d.reason


def test_all_routes_sale_is_always_notified(rules):
    d = Deal(airline="ジェットスター", airline_type="LCC", origin="OKA", destination="全路線",
             price=777, basis="from", from_prices=[{"area": "国内", "price": 777}])
    assert judge(d, rules, NOW).rank == ALL_ROUTES


def test_skymark_uses_lcc_criteria(rules):
    # スカイマークの普段の最安（羽田 9,100円）は通知しない
    assert judge(deal(rules, "スカイマーク", "HND", 9100, tax="税込"), rules, NOW).rank is None


def test_airline_aliases(rules):
    assert rules.airline_info("Peach")[0] == "ピーチ"
    assert rules.airline_info("ジェットスター・ジャパン")[0] == "ジェットスター"
    assert rules.airline_info("タイガーエア・台湾")[0] == "タイガーエア台湾"
    assert rules.airline_info("ティーウェイ航空")[0] == "トリニティ航空"
    assert rules.airline_info("知らない航空")[1] is None


def test_major_recommendation(rules):
    lcc = judge(deal(rules, "ピーチ", "FUK", 4800, tax="税込"), rules, NOW)
    major = judge(deal(rules, "ANA", "FUK", 6500, tax="税込"), rules, NOW)
    recommend_major([lcc, major], rules)
    assert major.note and "荷物込み" in major.note


def test_only_naha_departures_are_notified(rules):
    """仕様は那覇発。石垣→台北などは履歴にだけ残す（2026-10-02 の実走で混ざった）。"""
    d = judge(Deal(airline="タイガーエア台湾", airline_type="LCC", origin="ISG", destination="TPE",
                   price=8500, tax="税込"), rules, NOW)
    assert d.rank is None and "那覇発ではない" in d.reason


def test_package_tours_are_v2(rules):
    """Peach TRAVEL（航空券＋ホテル）を運賃として判定していた実例。"""
    d = judge(deal(rules, "ピーチ", "ICN", 22800, trip="往復", product="パッケージ"), rules, NOW)
    assert d.rank is None and "パッケージ" in d.reason
