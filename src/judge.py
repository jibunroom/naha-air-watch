"""判定（仕様「判定ルールと価格しきい値」）。価格の良し悪しはここだけで決める。

AI には判定させない。しきい値・行き先・航空会社は config/rules.yml に持つ。
ランク: 即買い ＞ 安い ＞ 全路線（那覇就航会社の全路線セール。逃さないため価格に関係なく通知）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config

JST = timezone(timedelta(hours=9))
INSTANT, CHEAP, ALL_ROUTES = "即買い", "安い", "全路線"
RANK_ORDER = {INSTANT: 0, CHEAP: 1, ALL_ROUTES: 2}


@dataclass
class Deal:
    """通知候補の運賃1件（記事から抽出したもの、またはスカイマークの公式運賃）。"""

    airline: str
    airline_type: str
    origin: str
    destination: str            # IATA。全路線セールは "全路線"
    price: int                  # 記事に書かれた金額（往復ならそのまま）
    currency: str = "JPY"
    tax: str = "不明"           # 税込 / 税別 / 不明
    trip: str = "片道"          # 片道 / 往復 / 不明
    basis: str = "route"        # route=路線別の価格 / from=路線共通の「〜円から」 / official=公式運賃表
    product: str = "航空券"     # 航空券 / パッケージ / その他
    sale_name: str | None = None
    booking_start: str | None = None
    booking_end: str | None = None
    travel_start: str | None = None
    travel_end: str | None = None
    excluded_periods: list = field(default_factory=list)
    peak_flags: dict = field(default_factory=dict)   # AI が返した繁忙期フラグ（日付が無いときの予備）
    member_only: bool | None = None
    presale: bool | None = None
    conditions: list = field(default_factory=list)
    from_prices: list = field(default_factory=list)  # 全路線セールの「〜円から」
    urls: list = field(default_factory=list)          # 元記事URL（複数サイトが報じたら全部）
    sources: list = field(default_factory=list)
    official_url: str | None = None
    note: str | None = None
    # 判定結果
    rank: str | None = None
    reason: str = ""
    effective_price: int | None = None
    category: str | None = None
    peaks_hit: list = field(default_factory=list)

    @property
    def oneway_price(self) -> int:
        return math.ceil(self.price / 2) if self.trip == "往復" else self.price


class Rules:
    def __init__(self, data: dict):
        self.categories = data["categories"]
        self.destinations = data["destinations"]
        self.domestic_category = data["domestic_category"]
        self.tax_estimate = data["tax_estimate"]
        self.peaks = data["peaks"]
        self.urgent_hours = data.get("urgent_hours", 24)
        self.major_recommend_diff = data.get("major_recommend_diff", 2000)
        self.airlines = data["airlines"]

    @classmethod
    def load(cls, path: Path | None = None) -> "Rules":
        import yaml
        with (path or config.CONFIG_DIR / "rules.yml").open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    # --- 航空会社 ---

    def airline_info(self, name: str | None) -> tuple[str, dict | None]:
        """表記ゆれを正式名に揃える。(正式名, 設定) を返す。知らない会社は (元の名前, None)。"""
        raw = (name or "").strip()
        if not raw:
            return "不明", None
        low = raw.lower()
        for canon, info in self.airlines.items():
            names = [canon] + list(info.get("aliases", []))
            if any(n.lower() == low for n in names):
                return canon, info
        for canon, info in self.airlines.items():
            names = [canon] + list(info.get("aliases", []))
            if any(n.lower() in low for n in names if len(n) >= 3):
                return canon, info
        return raw, None

    def is_naha_carrier(self, name: str | None) -> bool:
        _, info = self.airline_info(name)
        return bool(info and info.get("naha"))

    # --- 行き先 ---

    def category_for(self, destination: str, airline_type: str) -> str | None:
        info = self.destinations.get(destination)
        if not info:
            return None
        if info.get("category"):
            return info["category"]
        if info.get("region") == "国内":
            return self.domestic_category.get(airline_type) or self.domestic_category["不明"]
        return None

    def dest_name(self, code: str) -> str:
        """空港コード → 表示名。出発地（那覇など沖縄の空港）も引けるようにする。"""
        from .extract import OKINAWA_AIRPORTS
        return (self.destinations.get(code) or {}).get("name") or OKINAWA_AIRPORTS.get(code, code)

    def is_domestic(self, destination: str) -> bool:
        info = self.destinations.get(destination) or {}
        return info.get("region") == "国内" or info.get("category") == "県内離島"


def _d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def peaks_in_travel(deal: Deal, rules: Rules) -> list[str]:
    """搭乗期間に含まれる繁忙期。日付が分かれば日付で計算し、除外期間で消された日は数えない。

    日付が無いときだけ AI のフラグを使う。
    """
    ts, te = _d(deal.travel_start), _d(deal.travel_end)
    if not (ts and te) or te < ts:
        flags = deal.peak_flags or {}
        return [label for label, key in (("GW", "gw"), ("お盆", "obon"), ("年末年始", "nenmatsu"))
                if flags.get(key) is True]
    excluded = [(_d(p.get("start")), _d(p.get("end"))) for p in deal.excluded_periods or []]
    excluded = [(s, e) for s, e in excluded if s and e]

    def is_excluded(day: date) -> bool:
        return any(s <= day <= e for s, e in excluded)

    hits = []
    for label, (start_md, end_md) in rules.peaks.items():
        sm, sd = map(int, start_md.split("-"))
        em, ed = map(int, end_md.split("-"))
        for year in range(ts.year - 1, te.year + 1):
            ps = date(year, sm, sd)
            pe = date(year + (1 if (em, ed) < (sm, sd) else 0), em, ed)   # 年末年始は年をまたぐ
            lo, hi = max(ps, ts), min(pe, te)
            day = lo
            found = False
            while day <= hi:
                if not is_excluded(day):
                    found = True
                    break
                day += timedelta(days=1)
            if found:
                hits.append(label)
                break
    return hits


def is_expired(deal: Deal, now: datetime) -> bool:
    """予約締切が過ぎた、または搭乗期間が終わったセールは通知しない。分からなければ残す。"""
    if deal.booking_end:
        try:
            end = datetime.fromisoformat(deal.booking_end)
            if end.tzinfo is None:
                end = end.replace(tzinfo=JST)
            if len(deal.booking_end) <= 10:
                end = end.replace(hour=23, minute=59)
            if end < now:
                return True
        except ValueError:
            pass
    te = _d(deal.travel_end)
    return bool(te and te < now.astimezone(JST).date())


def judge(deal: Deal, rules: Rules, now: datetime) -> Deal:
    """deal.rank / reason / effective_price / category を埋める。通知しないなら rank=None。"""
    deal.rank, deal.reason = None, ""
    if is_expired(deal, now):
        deal.reason = "予約・搭乗期間が終了"
        return deal

    if deal.product != "航空券":
        deal.reason = f"{deal.product}（パッケージツアーは v2）"
        return deal
    if deal.origin != "OKA":
        deal.reason = f"那覇発ではない（{deal.origin}発）"   # 仕様は那覇発。石垣発などは履歴にだけ残す
        return deal

    if deal.destination == "全路線":
        deal.rank = ALL_ROUTES
        froms = "／".join(f"{f['area']}{f['price']:,}円〜" for f in deal.from_prices) or "価格は記事参照"
        deal.reason = (f"{deal.airline}のセール（{froms}）。那覇路線それぞれの価格は記事に記載なし"
                       "→ 公式サイトで要確認")
        return deal

    if deal.currency != "JPY":
        deal.reason = f"円以外の価格（{deal.currency}）のため判定できない"
        return deal

    category = rules.category_for(deal.destination, deal.airline_type)
    deal.category = category
    if not category:
        deal.reason = f"対象外の行き先（{rules.dest_name(deal.destination)}）"
        return deal

    notes = []
    price = deal.oneway_price
    if deal.trip == "往復":
        notes.append(f"往復{deal.price:,}円→片道{price:,}円で判定")
    if deal.tax == "税別":
        est = rules.tax_estimate["国内" if rules.is_domestic(deal.destination) else "国際"]
        price += est
        notes.append(f"税別のため+{est:,}円で判定")
    deal.effective_price = price

    th = rules.categories[category]
    label = category.replace("国内LCC", "国内LCC ").replace("国内大手", "国内大手 ")
    if th.get("instant") is not None and price <= th["instant"]:
        deal.rank = INSTANT
        deal.reason = f"{label}{th['instant']:,}円以下"
    elif price <= th["cheap"]:
        deal.rank = CHEAP
        limit = "5,000円未満" if th["cheap"] == 4999 else f"{th['cheap']:,}円以下"
        deal.reason = f"{label}{limit}"
    else:
        deal.reason = f"{label}の基準外（{price:,}円）"
        return deal

    deal.peaks_hit = peaks_in_travel(deal, rules)
    if deal.peaks_hit:
        deal.reason += "＋" + "・".join(deal.peaks_hit) + "対象"
        if deal.rank == CHEAP:
            deal.rank = INSTANT   # 繁忙期加点（仕様）
    if th.get("provisional"):
        deal.reason += "（基準は暫定）"
    if notes:
        deal.reason += "／" + "・".join(notes)
    return deal


def recommend_major(deals: list[Deal], rules: Rules) -> None:
    """同じ行き先で LCC と大手の差が小さければ、大手側に「荷物込みならこっち」と添える（仕様）。"""
    by_dest: dict[str, list[Deal]] = {}
    for d in deals:
        if d.rank in (INSTANT, CHEAP) and d.effective_price is not None:
            by_dest.setdefault(d.destination, []).append(d)
    for group in by_dest.values():
        lcc = [d for d in group if d.airline_type == "LCC"]
        major = [d for d in group if d.airline_type == "大手"]
        for m in major:
            if any(abs(m.effective_price - l.effective_price) <= rules.major_recommend_diff for l in lcc):
                m.note = ((m.note + "／") if m.note else "") + "荷物込みならこっち（LCCとの差2,000円以内）"


def hours_to_deadline(deal: Deal, now: datetime) -> float | None:
    if not deal.booking_end:
        return None
    try:
        end = datetime.fromisoformat(deal.booking_end)
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=JST)
    if len(deal.booking_end) <= 10:
        end = end.replace(hour=23, minute=59)
    return (end - now).total_seconds() / 3600
