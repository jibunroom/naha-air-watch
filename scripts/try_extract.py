"""ステップ3: 実記事10件で Gemini 抽出を試し、結果を表で出す。

使い方: .venv/bin/python -m scripts.try_extract [出力JSONのパス]
記事本文は著作物なのでリポジトリには保存しない（出力先は引数で指定）。
"""
from __future__ import annotations

import json
import logging
import sys

from src import config
from src.extract import Extractor, make_gemini_caller
from src.fetch import Article, Fetcher, fetch_body

ARTICLES = [
    # (配信元, タイトル, URL, 公開日, 公式か, ねらい)
    ("LCCjp", "タイガーエア台湾「2027年夏ダイヤ販売開始セール」",
     "https://dsk.ne.jp/news/tigerair_taiwan_sale_20261001.html", "2026-09-29", False, "那覇の路線別価格が表にある"),
    ("モノリビ", "タイガーエア台湾セール 8,500円〜｜2027年夏ダイヤ先行",
     "https://monomoney-living.com/tigerair-timesale_20261001/", "2026-09-29", False, "1と同じセールを別サイトで"),
    ("ジェットスター（PR TIMES）", "ジェットスター・ジャパン創業15周年記念セール開催",
     "https://prtimes.jp/main/html/rd/p/000000221.000012437.html", "2026-09-18", True, "全路線セール・那覇=高雄の言及あり"),
    ("ジェットスター（PR TIMES）", "「最強開運日セール」国内全路線片道777円から",
     "https://prtimes.jp/main/html/rd/p/000000216.000012437.html", "2026-02-26", True, "全路線セール・那覇の文字なし"),
    ("ピーチ（PR TIMES）", "創業15周年 全路線が対象の「感謝セール」",
     "https://prtimes.jp/main/html/rd/p/000000035.000081560.html", "2026-03-03", True, "全路線セール（ピーチは那覇が拠点）"),
    ("LCCjp", "ANA「国内線航空券 タイムセール」片道5,720円から",
     "https://dsk.ne.jp/news/ana_sale_20260925.html", "2026-09-24", False, "タイトルに那覇なし・大手"),
    ("LCCjp", "トリニティ航空「トリどきセール」韓国国際線 片道16,050円から",
     "https://dsk.ne.jp/news/trinity_sale_20261001.html", "2026-10-01", False, "那覇=ソウルが載っているか"),
    ("sky-budget", "イースター航空 ソウル・釜山まで片道100円から",
     "https://sky-budget.com/2026/10/01/eastar-jet-sale-oct2026/", "2026-10-01", False, "那覇発が含まれるか"),
    ("LCCjp", "Peach「弾丸往復運賃」冬ダイヤ 往復7,600円から",
     "https://dsk.ne.jp/news/peach_dangan_20260929.html", "2026-09-29", False, "往復運賃の扱い"),
    ("TRAICY", "タイガーエア・台湾「台湾デー開催記念セール」片道9,400円から",
     "https://www.traicy.com/posts/20260930383550/", "2026-09-30", False, "大阪中心・那覇は含まれないはず"),
]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    out_path = sys.argv[1] if len(sys.argv) > 1 else None
    config.load_env()
    settings = config.load_settings()
    fetcher = Fetcher(settings)
    caller = make_gemini_caller(config.env("GEMINI_API_KEY"))
    ex = Extractor(settings, caller)

    results = []
    for n, (src, title, url, pub, official, aim) in enumerate(ARTICLES, 1):
        a = Article(source=src, title=title, url=url, published=pub, official=official)
        if not fetch_body(fetcher, a, settings["extract"]["max_chars"]):
            print(f"[{n}] 本文取得失敗: {url}")
            results.append({"n": n, "aim": aim, "ok": False, "error": "本文取得失敗", "url": url})
            continue
        r = ex.extract(a)
        print(f"[{n}] {'OK' if r.ok else 'NG'} 本文{len(a.body)}字 {title[:40]}")
        results.append({"n": n, "aim": aim, "source": src, "title": title, "url": url,
                        "ok": r.ok, "error": r.error, "warnings": r.warnings,
                        "data": r.data, "body_chars": len(a.body)})
    print(f"\nGemini リクエスト数: {ex.requests} / モデル別: {ex.used} / 使えなくなったモデル: {ex.unavailable}")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print(f"結果: {out_path}")


if __name__ == "__main__":
    main()
