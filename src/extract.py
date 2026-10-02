"""Gemini で記事から運賃情報を構造化して取り出す（仕様「抽出」）。

価格の良し悪しはここでは判断させない（判定はコード側のルール・仕様どおり）。
最優先は「大事なセールを逃さない」。そのため:
  - 沖縄発着の運賃は表の行まで全部出させる
  - 全路線セールで那覇の個別価格が無ければ「〜円から」を別枠で出させる
  - 抽出に失敗した記事も捨てず、ok=False のまま呼び出し元へ返す（後段で「要確認」通知）

Gemini 呼び出しは caller 関数に切り出してあり、テストでは差し替えられる。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from .fetch import Article

log = logging.getLogger(__name__)

OKINAWA_AIRPORTS = {"OKA": "那覇", "ISG": "石垣", "MMY": "宮古", "SHI": "下地島", "UEO": "久米島"}

# 表記ゆれ → IATA。プロンプトに渡し、検証にも使う
AIRPORTS = {
    # 沖縄
    "那覇": "OKA", "沖縄": "OKA", "石垣": "ISG", "宮古": "MMY", "下地島": "SHI", "久米島": "UEO",
    # 国内
    "成田": "NRT", "羽田": "HND", "関西": "KIX", "伊丹": "ITM", "神戸": "UKB", "中部": "NGO",
    "新千歳": "CTS", "福岡": "FUK", "北九州": "KKJ", "仙台": "SDJ", "茨城": "IBR", "小松": "KMQ",
    "広島": "HIJ", "高松": "TAK", "松山": "MYJ", "長崎": "NGS", "熊本": "KMJ", "鹿児島": "KOJ",
    "宮崎": "KMI", "奄美": "ASJ", "静岡": "FSZ", "新潟": "KIJ", "富山": "TOY", "岡山": "OKJ",
    # 台湾・韓国・香港・東南アジア
    "台北(桃園)": "TPE", "台北(松山)": "TSA", "台中": "RMQ", "高雄": "KHH",
    "ソウル(仁川)": "ICN", "ソウル(金浦)": "GMP", "釜山": "PUS", "大邱": "TAE", "清州": "CJJ",
    "香港": "HKG", "マカオ": "MFM", "上海(浦東)": "PVG",
    "バンコク(スワンナプーム)": "BKK", "バンコク(ドンムアン)": "DMK", "シンガポール": "SIN",
    "クアラルンプール": "KUL", "マニラ": "MNL", "セブ": "CEB", "クラーク": "CRK",
    "ホーチミン": "SGN", "ハノイ": "HAN", "ダナン": "DAD",
}
KNOWN_IATA = set(AIRPORTS.values())

SYSTEM_PROMPT = """あなたは航空券セール記事から運賃情報を取り出す抽出器。
価格が安いかどうかの判断はしない。記事に書かれている事実だけを返す。推測で埋めず、分からない項目は null。
JSONオブジェクトを1つだけ返す。説明文・マークダウン禁止。

【最優先】沖縄県の空港（那覇OKA・石垣ISG・宮古MMY・下地島SHI・久米島UEO）を発着する運賃を1つも取りこぼさない。
- 沖縄の路線の運賃が載っていれば、表の中の行も含めてすべて fares に入れる。
- 「那覇－台北」のように方向が書かれていない路線は、origin に沖縄側の空港を入れる。
- 沖縄を発着しない路線の運賃は fares に入れない。
- basis: その路線自身の価格が書かれていれば必ず "route"（「片道3,900円～」のように「～」「から」が付いていても route）。
  沖縄の路線名だけ書かれていて個別の価格が無い場合（例:「那覇＝高雄線もセール対象」）に限り、その路線を
  fares に入れ、price に該当エリアの「〜円から」を入れ、basis を "from" にする。
- 全路線・国内全路線などが対象で、沖縄の路線名も個別価格も書かれていない場合は fares を空にし、
  記事の「〜円から」を from_prices に入れる。
- 価格が片道か往復かを trip に必ず入れる。記事が往復料金なら往復のまま書く（勝手に半額にしない）。

空港は IATA コードにする。対応表: {airports}
日時は "YYYY-MM-DDTHH:MM"、日付は "YYYY-MM-DD"。年が省略されていれば記事の公開日（{published}）から補う。

出力形式（キーはすべて必須）:
{{
 "is_sale": true,                  // 運賃のセール・割引運賃・キャンペーン運賃の告知なら true。就航・増便・制服などは false
 "product": "航空券|パッケージ|その他", // 航空券＋ホテルなどのセット（パッケージツアー）は "パッケージ"
 "airline": "航空会社名",
 "airline_type": "LCC|大手|中堅|不明",
 "sale_name": "セール名",
 "okinawa_included": "yes|no|unknown", // yes=沖縄路線が対象と明記 / no=対象外が明らか / unknown=全路線対象などで明記なし
 "fares": [{{"origin":"OKA","destination":"TPE","price":8500,"currency":"JPY","tax":"税込|税別|不明","trip":"片道|往復","basis":"route|from","excluded_periods":[],"note":null}}],
 "from_prices": [{{"area":"国内|国際|全体","price":3790,"currency":"JPY","tax":"税込|税別|不明","trip":"片道|往復"}}],
 "booking_start": "YYYY-MM-DDTHH:MM",
 "booking_end": "YYYY-MM-DDTHH:MM",
 "travel_start": "YYYY-MM-DD",
 "travel_end": "YYYY-MM-DD",
 "excluded_periods": [{{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","label":"年末年始"}}], // 全路線共通の除外期間だけ。
                                   // 一部の路線だけの除外は、その路線の fares[].excluded_periods に入れる（沖縄以外の路線の除外は書かない）
 "peak": {{"gw": true, "obon": true, "nenmatsu": false}}, // 搭乗期間に含まれ、除外もされていなければ true。不明は null
 "member_only": false,             // 会員限定（先行販売のみ会員限定なら false にして presale を true）
 "presale": false,
 "conditions": ["返金不可", "預け荷物は別料金"],
 "official_url": "公式の予約・セールページURL（記事に書かれていれば）"
}}"""

RETRY_NOTE = "前回の出力はJSONとして不正だった。指定の形式のJSONオブジェクトだけを返せ。"

DEFAULTS = {
    "is_sale": False, "product": "航空券", "airline": None, "airline_type": "不明", "sale_name": None,
    "okinawa_included": "unknown", "fares": [], "from_prices": [],
    "booking_start": None, "booking_end": None, "travel_start": None, "travel_end": None,
    "excluded_periods": [], "peak": {"gw": None, "obon": None, "nenmatsu": None},
    "member_only": None, "presale": None, "conditions": [], "official_url": None,
}
REQUIRED = ("is_sale", "fares")

PRICE_MIN, PRICE_MAX = 100, 1_000_000   # 円。これを外れる値は誤抽出として捨てる（仕様「誤抽出」）


@dataclass
class Extraction:
    article: Article
    ok: bool
    data: dict | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def build_prompt(article: Article) -> tuple[str, str]:
    airports = "、".join(f"{k}={v}" for k, v in AIRPORTS.items())
    system = SYSTEM_PROMPT.format(airports=airports, published=(article.published or "不明")[:10])
    user = f"記事タイトル: {article.title}\n配信元: {article.source}\n\n本文:\n{article.body}"
    return system, user


def parse_json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSONオブジェクトが見つからない")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("オブジェクトではない")
    return obj


def _int_price(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = re.sub(r"[,，円\s]", "", v)
        return int(s) if s.isdigit() else None
    return None


def _valid_dt(v) -> str | None:
    if not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            datetime.strptime(v[:16] if "T" in v else v[:10], fmt)
            return v[:16] if "T" in v else v[:10]
        except ValueError:
            continue
    return None


def _periods(v) -> list[dict]:
    return [{"start": _valid_dt(p.get("start")), "end": _valid_dt(p.get("end")), "label": p.get("label")}
            for p in (v or []) if isinstance(p, dict)]


def normalize(raw: dict) -> tuple[dict, list[str]]:
    """スキーマ検証と正規化。誤抽出は捨てて warnings に残す（黙って消さない）。"""
    for key in REQUIRED:
        if key not in raw:
            raise ValueError(f"必須キー欠落: {key}")
    d = {**DEFAULTS, **raw}
    warnings: list[str] = []

    fares = []
    for f in d.get("fares") or []:
        if not isinstance(f, dict):
            continue
        o = str(f.get("origin") or "").upper().strip()
        dst = str(f.get("destination") or "").upper().strip()
        price = _int_price(f.get("price"))
        if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
            warnings.append(f"価格が不正なため除外: {o}-{dst} {f.get('price')!r}")
            continue
        if o not in OKINAWA_AIRPORTS and dst not in OKINAWA_AIRPORTS:
            warnings.append(f"沖縄発着でないため除外: {o}-{dst}")
            continue
        if dst in OKINAWA_AIRPORTS and o not in OKINAWA_AIRPORTS:
            o, dst = dst, o   # 「那覇発」に揃える
        for code in (o, dst):
            if code not in KNOWN_IATA:
                warnings.append(f"未知の空港コード: {code}（そのまま残す）")
        fares.append({
            "origin": o, "destination": dst, "price": price,
            "currency": (f.get("currency") or "JPY").upper(),
            "tax": f.get("tax") if f.get("tax") in ("税込", "税別") else "不明",
            "trip": f.get("trip") if f.get("trip") in ("片道", "往復") else "不明",
            "basis": "from" if f.get("basis") == "from" else "route",
            "excluded_periods": _periods(f.get("excluded_periods")),
            "note": f.get("note"),
        })
    d["fares"] = fares

    froms = []
    for f in d.get("from_prices") or []:
        price = _int_price(f.get("price")) if isinstance(f, dict) else None
        if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
            continue
        froms.append({"area": f.get("area") or "全体", "price": price,
                      "currency": (f.get("currency") or "JPY").upper(),
                      "tax": f.get("tax") if f.get("tax") in ("税込", "税別") else "不明",
                      "trip": f.get("trip") if f.get("trip") in ("片道", "往復") else "不明"})
    d["from_prices"] = froms

    for key in ("booking_start", "booking_end", "travel_start", "travel_end"):
        d[key] = _valid_dt(d.get(key))
    d["excluded_periods"] = _periods(d.get("excluded_periods"))
    peak = d.get("peak") if isinstance(d.get("peak"), dict) else {}
    d["peak"] = {k: peak.get(k) for k in ("gw", "obon", "nenmatsu")}
    if d.get("okinawa_included") not in ("yes", "no", "unknown"):
        d["okinawa_included"] = "unknown"
    d["is_sale"] = bool(d.get("is_sale"))
    if d.get("product") not in ("航空券", "パッケージ", "その他"):
        d["product"] = "航空券"
    d["conditions"] = [str(c) for c in (d.get("conditions") or [])]
    return d, warnings


def make_gemini_caller(api_key: str, timeout_sec: int = 60):
    """call(model, system, user) -> 応答テキスト。モデルは呼ぶたびに選べる（予備モデルへの切替用）。

    タイムアウト必須。無しだと応答の返らない1回の呼び出しで実行全体が止まる
    （2026-10-02 に実際に22分止まった。Actions なら30分で強制終了＝その日のメールが出ない）。
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=timeout_sec * 1000))

    def call(model: str, system: str, user: str) -> str:
        resp = client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        return resp.text or ""

    return call


class AllModelsUnavailable(Exception):
    """どのモデルも使えない（全モデルが1日の上限・混雑・廃止）。残りの記事は次回へ持ち越す。"""


def classify_error(e: Exception) -> str:
    """Gemini のエラーを対処法ごとに分ける。

    daily_quota … そのモデルは今日はもう使えない → すぐ次のモデルへ
    gone        … モデルが廃止・存在しない → 次のモデルへ
    busy        … 混雑（503）や1分あたりの上限 → 少し待って同じモデルで再試行
    fatal       … リクエスト自体の誤り → この記事は諦める（記事は「要確認」で残る）

    実測: gemini-flash-latest（3.8-flash）の無料枠は1日20回（2026-10-01）。
    超えると 429 で quotaId に PerDay が入る。混雑時は 503 UNAVAILABLE。
    """
    s = f"{type(e).__name__} {e}"
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return "daily_quota" if "PerDay" in s else "busy"
    if "404" in s or "NOT_FOUND" in s:
        return "gone"
    if "503" in s or "UNAVAILABLE" in s or "imeout" in s or "500" in s:
        return "busy"
    return "fatal"


class Extractor:
    """モデルを順に試す抽出器。

    無料枠の上限はモデルごと（1日20回など）なので、1つが上限・混雑でも
    次のモデルで続けられる。入札監視と同じ鍵を使うため、先頭は入札監視が
    使う gemini-flash-latest 以外にして、互いの枠を食い合わないようにしている。
    """

    def __init__(self, settings: dict, caller, models: list[str] | None = None,
                 sleeper=time.sleep):
        g = settings["gemini"]
        self.models = list(models or g["models"])
        self.min_interval = g["min_interval_sec"]
        self.max_requests = g["max_requests_per_run"]
        self.backoff = g["backoff_sec"]
        self.max_retries = g["max_retries"]
        self.caller = caller
        self.sleep = sleeper
        self.requests = 0
        self.used: dict[str, int] = {}          # モデル別の使用回数（サマリ用）
        self.unavailable: dict[str, str] = {}   # 今回の実行で使えなくなったモデルと理由
        self._last: float | None = None

    def _throttle(self) -> None:
        if self._last is not None:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                self.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def _call(self, system: str, user: str) -> str:
        for model in self.models:
            if model in self.unavailable:
                continue
            for attempt in range(self.max_retries):
                if self.requests >= self.max_requests:
                    raise AllModelsUnavailable("1回の実行の Gemini 上限に達した")
                self._throttle()
                self.requests += 1
                self.used[model] = self.used.get(model, 0) + 1
                try:
                    return self.caller(model, system, user)
                except Exception as e:
                    kind = classify_error(e)
                    if kind == "fatal":
                        raise
                    if kind in ("daily_quota", "gone"):
                        self.unavailable[model] = kind
                        log.warning("Gemini %s が使えないため次のモデルへ: %s", model, kind)
                        break
                    if attempt < self.max_retries - 1:
                        self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            else:
                # 混雑で再試行を使い切った。今回はこのモデルを諦めて次へ
                self.unavailable[model] = "busy"
                log.warning("Gemini %s が混雑のため次のモデルへ", model)
        raise AllModelsUnavailable(f"使えるモデルが無い: {self.unavailable}")

    def extract(self, article: Article) -> Extraction:
        """1記事を抽出。失敗しても例外にせず ok=False で返す（記事は捨てない）。"""
        system, user = build_prompt(article)
        for retry in (False, True):
            try:
                raw = self._call(system, user + (f"\n\n{RETRY_NOTE}" if retry else ""))
            except AllModelsUnavailable:
                raise
            except Exception as e:
                return Extraction(article, ok=False, error=f"呼び出し失敗: {e}")
            try:
                data, warnings = normalize(parse_json_object(raw))
                return Extraction(article, ok=True, data=data, warnings=warnings)
            except (ValueError, json.JSONDecodeError) as e:
                log.warning("抽出の検証失敗(retry=%s) %s: %s", retry, article.url, e)
                last = str(e)
        return Extraction(article, ok=False, error=f"JSON不正: {last}")
