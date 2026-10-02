"""一次フィルタ（仕様「一次フィルタ」＋逃さないための拡張）。AI は使わない。

1段目（タイトル）: セールらしい記事だけ本文を取りに行く
2段目（本文）:     沖縄の地名がある、または那覇就航会社の全路線セールなら Gemini へ

仕様は「本文に那覇・沖縄・OKA・石垣を含むもの」だが、全路線セールの記事には
那覇の文字が無いことがある（実例: ジェットスター「最強開運日セール」国内777円〜）。
それを拾うため、那覇就航会社の「全路線」記事も通す。
"""
from __future__ import annotations

from .fetch import OKINAWA_WORDS, Article
from .judge import Rules

SALE_WORDS = ("セール", "SALE", "Sale", "特価", "割引運賃", "キャンペーン運賃", "運賃",
              "円から", "円〜", "円～", "円~", "弾丸", "タイムセール")
ALL_ROUTE_WORDS = ("全路線", "全線", "国内線全", "国際線全", "アジア全", "全便", "全方面")


def title_pass(article: Article) -> bool:
    """1段目。一覧ページから拾ったリンクはセール記事なので素通し。"""
    if article.source_kind == "page_links":
        return True
    return any(w in article.title for w in SALE_WORDS)


def mentioned_carrier(text: str, rules: Rules) -> str | None:
    for canon, info in rules.airlines.items():
        for n in [canon] + list(info.get("aliases", [])):
            if len(n) >= 3 and n.lower() in text.lower():
                return canon
    return None


def body_pass(article: Article, rules: Rules) -> tuple[bool, str]:
    """2段目。(通すか, 理由) を返す。"""
    text = f"{article.title}\n{article.body}"
    if any(w in text for w in OKINAWA_WORDS[:7]):   # 那覇・沖縄・OKA・石垣・宮古・下地島・久米島
        return True, "沖縄の地名あり"
    carrier = article.airline_hint or mentioned_carrier(article.title, rules) \
        or mentioned_carrier(article.body[:600], rules)
    if carrier and rules.is_naha_carrier(carrier) and any(w in text for w in ALL_ROUTE_WORDS):
        return True, f"那覇就航の{carrier}の全路線セール"
    return False, "沖縄と関係なし"
