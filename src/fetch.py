"""取得（RSS・一覧ページ・スカイマーク運賃CSV）と本文抽出。

巡回マナー（UA・同一ホスト2秒間隔・タイムアウト・robots.txt）はここだけで守る。
他のモジュールは requests を直接呼ばない。
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import urllib.robotparser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests

log = logging.getLogger(__name__)

# 長い記事を削るときに優先して残す行の目印（那覇路線を落とさないため）
OKINAWA_WORDS = ("那覇", "沖縄", "OKA", "石垣", "宮古", "下地島", "久米島",
                 "全路線", "全線", "国内線全", "アジア全", "国際線全")


@dataclass
class Article:
    """取得した記事1件。抽出・判定・通知を流れる単位。"""

    source: str
    title: str
    url: str
    published: str | None = None
    official: bool = False
    airline_hint: str | None = None   # 公式発表なら発表元の航空会社
    body: str = ""
    source_kind: str = "rss"

    def to_dict(self) -> dict:
        return asdict(self)


class Fetcher:
    """巡回マナーを守る HTTP クライアント。"""

    def __init__(self, settings: dict):
        http = settings["http"]
        self.timeout = http["timeout_sec"]
        self.retries = http["retries"]
        self.host_interval = http["host_interval_sec"]
        self.respect_robots = http.get("respect_robots", True)
        self.user_agent = http["user_agent"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.host_interval:
                time.sleep(self.host_interval - gap)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                self._wait(base)
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None   # robots.txt が無ければ制限なし
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def get(self, url: str) -> requests.Response | None:
        """1回リトライ付きで GET。robots.txt で禁止・失敗なら None。"""
        if not self.allowed(url):
            log.warning("robots.txt で禁止されているため取得しない: %s", url)
            return None
        err = None
        for attempt in range(self.retries + 1):
            try:
                self._wait(url)
                r = self.session.get(url, timeout=self.timeout)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                err = e
                if attempt < self.retries:
                    time.sleep(1.0)
        log.warning("取得失敗 %s: %s", url, err)
        return None


# --- URL ---


def normalize_url(url: str) -> str:
    """utm_* などの追跡用パラメータと #以降を外し、同じ記事を同じURLにそろえる。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    path = p.path or "/"
    return urlunsplit((p.scheme, p.netloc.lower(), path, urlencode(query), ""))


# --- RSS ---


def _paged(url: str, page: int) -> str:
    if page == 1:
        return url
    return url + ("&" if "?" in url else "?") + f"paged={page}"


def _entry_date(e) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return None


def parse_feed(content: bytes, source: dict) -> list[Article]:
    """フィードのバイト列を Article のリストにする（ネット不要・テスト可能）。"""
    out = []
    for e in feedparser.parse(content).entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue   # 元記事URLの無いものは捨てる（仕様）
        out.append(Article(
            source=source["name"],
            title=(e.get("title") or "").strip(),
            url=normalize_url(link),
            published=_entry_date(e),
            official=bool(source.get("official")),
            airline_hint=source.get("airline"),
        ))
    return out


def read_feed(fetcher: Fetcher, source: dict, is_seen=lambda url: False) -> tuple[list[Article], bool]:
    """RSS を読む。既読の記事に当たるまで最大 pages ページ遡る。(記事, 成功) を返す。

    1ページに収まらないほど更新の多いサイト（TRAICY・sky-budget）で、
    起動が遅れた日に記事がこぼれないようにするため。
    """
    articles: list[Article] = []
    for page in range(1, int(source.get("pages", 1)) + 1):
        r = fetcher.get(_paged(source["url"], page))
        if r is None:
            return articles, page > 1   # 2ページ目以降の失敗は「遡れなかった」だけ
        items = parse_feed(r.content, source)
        if not items:
            return articles, page > 1
        articles.extend(items)
        if any(is_seen(a.url) for a in items):
            break   # ここから先は前回までに読んだ範囲
    return articles, True


# --- 一覧ページ（RSSから溢れた・長く続いているセール記事の回収用） ---

_URL_DATE = re.compile(r"_(20\d{2})(\d{2})(\d{2})/?$")


def parse_page_links(html: str, page_url: str, source: dict) -> list[Article]:
    """一覧ページの本文中から、個別記事へのリンクを Article にする（ネット不要・テスト可能）。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("article") or soup.find("main") or soup
    pattern = re.compile(source.get("link_pattern") or ".")
    out, seen = [], set()
    for a in main.find_all("a", href=True):
        url = normalize_url(urljoin(page_url, a["href"]))
        if url in seen or url.rstrip("/") == normalize_url(page_url).rstrip("/"):
            continue
        if not pattern.search(url):
            continue
        seen.add(url)
        m = _URL_DATE.search(urlparse(url).path)
        published = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
        out.append(Article(source=source["name"], title=a.get_text(" ", strip=True)[:120],
                           url=url, published=published, official=bool(source.get("official")),
                           airline_hint=source.get("airline"),
                           # セール一覧のリンクはセール記事そのもの。お知らせ一覧は混ざり物なので区別する
                           source_kind="sale_list" if source.get("sale_list") else "page_links"))
    return out


def read_page_links(fetcher: "Fetcher", source: dict) -> tuple[list[Article], bool]:
    r = fetcher.get(source["url"])
    if r is None:
        return [], False
    r.encoding = r.apparent_encoding or r.encoding
    items = parse_page_links(r.text, source["url"], source)
    return items, bool(items)


# --- 本文 ---


def extract_text(html: str, url: str = "") -> str:
    """HTML から本文テキストを取り出す。trafilatura → BeautifulSoup の順。"""
    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(html, url=url or None, include_tables=True) or ""
    except Exception:
        text = ""
    if len(text.strip()) < 200:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            t.decompose()
        main = soup.find("main") or soup.find("article") or soup.find("body") or soup
        text = main.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def focus_text(text: str, max_chars: int) -> str:
    """長い記事を max_chars に収める。冒頭＋沖縄関連の行（前後1行つき）を優先して残す。

    セール記事は冒頭に概要、下の方に路線別の運賃表があることが多い。
    単純に先頭で切ると、表の後半にある那覇路線の価格を落としてしまう。
    """
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    head_budget = max_chars // 2
    head, used = [], 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > head_budget:
            break
        head.append(ln)
        used += len(ln) + 1
    start = len(head)
    keep: list[int] = []
    for i in range(start, len(lines)):
        if any(w in lines[i] for w in OKINAWA_WORDS):
            keep.extend(j for j in (i - 1, i, i + 1) if start <= j < len(lines))
    picked, budget = [], max_chars - used - 20
    for j in sorted(set(keep)):
        if budget - len(lines[j]) - 1 < 0:
            break
        picked.append(lines[j])
        budget -= len(lines[j]) + 1
    return "\n".join(head + ["（…中略…）"] + picked)


def fetch_body(fetcher: Fetcher, article: Article, max_chars: int) -> bool:
    r = fetcher.get(article.url)
    if r is None:
        return False
    r.encoding = r.apparent_encoding or r.encoding
    article.body = focus_text(extract_text(r.text, article.url), max_chars)
    return bool(article.body)


# --- スカイマーク公式の運賃CSV ---

_CSV_HEADER = ("origin", "via", "destination", "imatoku", "tasutoku", "note")


def parse_skymark_csv(text: str) -> list[dict]:
    """CSV を路線ごとの最安運賃にする（ネット不要・テスト可能）。

    列: 出発, 経由, 到着, いま得の最安, たす得の最安, 備考（空港は小文字の3レター）。
    """
    rows = []
    for raw in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(raw) < 5 or not raw[0].strip():
            continue
        rec = dict(zip(_CSV_HEADER, [c.strip() for c in raw] + [""] * 6))

        def yen(s: str) -> int | None:
            s = s.replace(",", "").replace("円", "").strip()
            return int(s) if s.isdigit() else None

        rows.append({
            "origin": rec["origin"].upper(),
            "via": rec["via"].upper() or None,
            "destination": rec["destination"].upper(),
            "imatoku": yen(rec["imatoku"]),
            "tasutoku": yen(rec["tasutoku"]),
            "note": rec["note"] or None,
        })
    return rows


def read_skymark_fares(fetcher: Fetcher, sale_page_url: str) -> tuple[list[dict], str | None]:
    """セールページ → 運賃スクリプト → CSV の順にたどる。(運賃, CSVのURL) を返す。

    CSV のファイル名（farelist_YYYYMM.csv）は更新のたびに変わるので決め打ちしない。
    """
    page = fetcher.get(sale_page_url)
    if page is None:
        return [], None
    m = re.search(r'src="([^"]*script_farelist[^"]*\.js)', page.text)
    if not m:
        log.warning("スカイマーク: 運賃スクリプトが見つからない（ページ構造が変わった可能性）")
        return [], None
    js = fetcher.get(urljoin(sale_page_url, m.group(1)))
    if js is None:
        return [], None
    m = re.search(r"""fetch\(\s*['"]([^'"]+\.csv)""", js.text)
    if not m:
        log.warning("スカイマーク: CSV の場所が見つからない（スクリプトが変わった可能性）")
        return [], None
    csv_url = urljoin(sale_page_url, m.group(1))
    r = fetcher.get(csv_url)
    if r is None:
        return [], csv_url
    r.encoding = "utf-8-sig"
    return parse_skymark_csv(r.text), csv_url
