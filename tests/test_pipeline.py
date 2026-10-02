"""一次フィルタ・抽出結果→判定候補・重複排除・件名（ネットにもAIにも触れない）。"""
import dataclasses
from datetime import datetime

import pytest

from src.extract import Extraction
from src.fetch import Article, normalize_url, parse_page_links
from src.judge import ALL_ROUTES, INSTANT, JST, Deal, Rules, judge
from src.main import (already_notified, deals_from_extraction, deals_from_skymark,
                      merge_duplicates, record_notified)
from src.notify import build_body, build_subject
from src.prefilter import body_pass, title_pass
from src.state import State

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=JST)


@pytest.fixture(scope="module")
def rules():
    return Rules.load()


def art(title, body="", **kw):
    return Article(source=kw.pop("source", "LCCjp"), title=title, url=kw.pop("url", "https://x.jp/a"),
                   body=body, **kw)


# --- 一次フィルタ ---


def test_title_filter_needs_sale_words():
    assert title_pass(art("タイガーエア台湾、夏ダイヤ販売開始セール 片道8,500円から"))
    assert not title_pass(art("JAL、ピンクリボン月間の啓発キャンペーンを実施"))


def test_list_page_links_pass_title_filter():
    assert title_pass(art("創業15周年記念セール", source_kind="page_links"))
    assert title_pass(art("タイムセール", source_kind="page_links"))


def test_body_with_okinawa_passes(rules):
    assert body_pass(art("セール", "那覇－台北 片道8,500円"), rules)[0]


def test_all_routes_sale_without_naha_passes(rules):
    """実例: ジェットスター「最強開運日セール」の記事には那覇・沖縄が1回も出てこない。"""
    a = art("「最強開運日セール」国内全路線片道777円から", "対象路線 国内18路線、アジア7路線",
            source="ジェットスター（PR TIMES）", airline_hint="ジェットスター")
    ok, why = body_pass(a, rules)
    assert ok and "全路線" in why


def test_unrelated_sale_is_dropped(rules):
    a = art("エアロK航空セール 北九州−清州が15％割引", "北九州－清州線が対象")
    assert not body_pass(a, rules)[0]


# --- URL ---


def test_tracking_params_are_removed():
    u = "https://monomoney-living.com/tigerair-timesale_20261001/?utm_source=rss&utm_medium=rss#top"
    assert normalize_url(u) == "https://monomoney-living.com/tigerair-timesale_20261001/"


def test_list_page_links_get_dates_from_url():
    html = """<article><a href="/jal-timesale_20260908/">国内線航空券タイムセール</a>
              <a href="/privacypolicy/">プライバシー</a></article>"""
    src = {"name": "モノリビ 国内線セール一覧",
           "link_pattern": r"monomoney-living\.com/[^/]+_20\d{6}/?$"}
    items = parse_page_links(html, "https://monomoney-living.com/aircraft-ticket_timesale/", src)
    assert [(a.url, a.published) for a in items] == [
        ("https://monomoney-living.com/jal-timesale_20260908/", "2026-09-08")]


# --- 抽出結果 → 判定候補 ---


def ext(data, **kw):
    return Extraction(article=art("t", url=kw.get("url", "https://x.jp/a")), ok=True, data=data)


BASE = {"is_sale": True, "okinawa_included": "yes", "fares": [], "from_prices": [],
        "booking_start": None, "booking_end": None, "travel_start": None, "travel_end": None,
        "excluded_periods": [], "peak": {}, "member_only": None, "presale": None,
        "conditions": [], "official_url": None, "sale_name": None, "airline_type": "LCC"}


def test_route_fares_become_deals(rules):
    d = {**BASE, "airline": "タイガーエア・台湾", "fares": [
        {"origin": "OKA", "destination": "TPE", "price": 8500, "currency": "JPY",
         "tax": "税別", "trip": "片道", "basis": "route", "note": None}]}
    deals = deals_from_extraction(ext(d), rules)
    assert [(x.airline, x.destination, x.price) for x in deals] == [("タイガーエア台湾", "TPE", 8500)]


def test_all_routes_sale_becomes_one_all_routes_deal(rules):
    d = {**BASE, "airline": "ジェットスター・ジャパン", "okinawa_included": "unknown",
         "from_prices": [{"area": "国内", "price": 777}, {"area": "国際", "price": 7777}]}
    deals = deals_from_extraction(ext(d), rules)
    assert len(deals) == 1 and deals[0].destination == "全路線"
    assert judge(deals[0], rules, NOW).rank == ALL_ROUTES


def test_not_okinawa_or_not_sale_gives_nothing(rules):
    assert deals_from_extraction(ext({**BASE, "airline": "x", "okinawa_included": "no"}), rules) == []
    assert deals_from_extraction(ext({**BASE, "airline": "x", "is_sale": False}), rules) == []


def test_skymark_only_naha_direct_routes(rules):
    rows = [{"origin": "OKA", "via": None, "destination": "SHI", "imatoku": 4100, "tasutoku": 6000, "note": None},
            {"origin": "OKA", "via": "UKB", "destination": "SDJ", "imatoku": 12400, "tasutoku": 14400, "note": None},
            {"origin": "HND", "via": None, "destination": "OKA", "imatoku": 9100, "tasutoku": 12100, "note": None}]
    deals = deals_from_skymark(rows, rules, "https://www.skymark.co.jp/")
    assert [(d.destination, d.price) for d in deals] == [("SHI", 4100)]


# --- 重複排除 ---


def tiger(url, booking_start, presale=False):
    return judge(Deal(airline="タイガーエア台湾", airline_type="LCC", origin="OKA", destination="TPE",
                      price=8500, tax="税別", booking_start=booking_start,
                      booking_end="2026-10-03T23:59", presale=presale, urls=[url],
                      travel_start="2027-03-28", travel_end="2027-10-30",   # 実際のセール（GW・お盆を含む）
                      sources=[url.split("/")[2]]), Rules.load(), NOW)


def test_same_sale_from_two_sites_is_merged():
    """実例: 同じセールを LCCjp は予約開始10/2、モノリビは会員先行込みで10/1 と書いていた。"""
    a = tiger("https://dsk.ne.jp/news/x.html", "2026-10-02T11:00")
    b = tiger("https://monomoney-living.com/y/", "2026-10-01T11:00", presale=True)
    merged = merge_duplicates([a, b])
    assert len(merged) == 1
    assert merged[0].urls == ["https://dsk.ne.jp/news/x.html", "https://monomoney-living.com/y/"]
    assert merged[0].presale


def test_already_notified_is_not_sent_again(tmp_path):
    st = State(data_dir=tmp_path)
    a = tiger("https://dsk.ne.jp/news/x.html", "2026-10-02T11:00")
    record_notified([a], st, NOW)
    assert already_notified(tiger("https://other/", "2026-10-01T11:00"), st, NOW)


def test_same_price_next_season_is_a_new_sale(tmp_path):
    st = State(data_dir=tmp_path)
    record_notified([tiger("https://a/", "2026-10-02T11:00")], st, NOW)
    later = tiger("https://b/", "2027-03-01T11:00")
    later.booking_end = "2027-03-03T23:59"
    assert not already_notified(later, st, NOW)


def test_undelivered_deal_round_trips():
    d = tiger("https://a/", "2026-10-02T11:00")
    assert Deal(**dataclasses.asdict(d)) == d


# --- 件名・本文 ---


def test_subject_counts_and_urgent(rules):
    d = tiger("https://a/", "2026-10-02T11:00")          # 締切 10/3 23:59 ＝ 39時間後
    assert build_subject([d], NOW, rules) == "[那覇セール] 即買い1件・安い0件（10/2）"
    late = datetime(2026, 10, 3, 9, 0, tzinfo=JST)        # 締切まで15時間
    assert build_subject([d], late, rules).startswith("【急ぎ】")


def test_body_has_required_items(rules):
    d = tiger("https://a/", "2026-10-02T11:00")
    text, html_body = build_body([d], ["サマリ"], rules, NOW)
    assert "■【即買い】那覇→台北（桃園）" in text
    assert "8,500円（税別・片道）" in text and "最安値・日付で変動" in text
    assert "【締切 10/3(土)23:59" in text            # テキストでは【】で強調
    assert "<b>締切 10/3(土)23:59" in html_body      # HTMLでは太字
    assert "預け荷物は別料金" in text
    assert d.rank == INSTANT
