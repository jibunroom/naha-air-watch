"""エントリポイント（仕様「システム構成」の 起動→取得→一次フィルタ→抽出→判定→保存→通知）。

使い方:
  python -m src.main                 本番（メール送信・git push あり）
  python -m src.main --dry-run       メールも保存もせず、結果を画面に出す
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta

from . import config, notify, slots
from .extract import AllModelsUnavailable, Extraction, Extractor, make_gemini_caller
from .fetch import (Article, Fetcher, fetch_body, read_feed, read_page_links,
                    read_skymark_fares)
from .judge import ALL_ROUTES, JST, Deal, Rules, judge, recommend_major
from .prefilter import body_pass, title_pass
from .state import State, now_jst

log = logging.getLogger(__name__)
FIRST_RUN_DAYS = 21          # 初回は過去3週間分の記事だけ見る（PR TIMES は何年分も載っているため）
SAME_SALE_DAYS = 14          # 日付が分からない同額のセールは、この日数以内なら同じものとみなす


# --- 抽出結果 → 判定候補 ---


def deals_from_extraction(r: Extraction, rules: Rules) -> list[Deal]:
    d, a = r.data, r.article
    if not d or not d.get("is_sale") or d.get("okinawa_included") == "no":
        return []
    name, info = rules.airline_info(d.get("airline") or a.airline_hint)
    atype = (info or {}).get("type") or d.get("airline_type") or "不明"
    common = dict(
        airline=name, airline_type=atype, sale_name=d.get("sale_name"),
        booking_start=d.get("booking_start"), booking_end=d.get("booking_end"),
        travel_start=d.get("travel_start"), travel_end=d.get("travel_end"),
        excluded_periods=d.get("excluded_periods") or [], peak_flags=d.get("peak") or {},
        member_only=d.get("member_only"), presale=d.get("presale"),
        conditions=d.get("conditions") or [], urls=[a.url], sources=[a.source],
        official_url=d.get("official_url"), product=d.get("product") or "航空券",
    )
    out = []
    for f in d.get("fares") or []:
        deal = Deal(origin=f["origin"], destination=f["destination"], price=f["price"],
                    currency=f["currency"], tax=f["tax"], trip=f["trip"],
                    basis=f.get("basis", "route"), note=f.get("note"), **common)
        # 除外期間は「全路線共通」＋「その路線だけ」。他の路線の除外は混ぜない
        deal.excluded_periods = list(common["excluded_periods"]) + list(f.get("excluded_periods") or [])
        out.append(deal)
    if out:
        return out
    # 那覇の個別価格が無い全路線セール。沖縄対象と明記、または那覇就航会社なら拾う（逃さない）
    froms = d.get("from_prices") or []
    if froms and (d.get("okinawa_included") == "yes" or rules.is_naha_carrier(name)):
        return [Deal(origin="OKA", destination="全路線", price=min(f["price"] for f in froms),
                     basis="from", from_prices=froms, **common)]
    return []


def deals_from_skymark(rows: list[dict], rules: Rules, page_url: str) -> list[Deal]:
    """スカイマーク公式の運賃表（いま得の最安）。那覇発の直行便だけ。"""
    out = []
    for row in rows:
        if row["origin"] != "OKA" or row["via"] or not row["imatoku"]:
            continue
        out.append(Deal(airline="スカイマーク", airline_type="中堅", origin="OKA",
                        destination=row["destination"], price=row["imatoku"], tax="税込",
                        trip="片道", basis="official", sale_name="いま得（公式運賃表の最安）",
                        urls=[page_url], sources=["スカイマーク（運賃CSV）"],
                        conditions=["予約変更不可の運賃（いま得）"]))
    return out


# --- 重複排除（仕様「データ保存と重複排除」） ---


def _overlap(a: Deal | dict, b: Deal | dict) -> bool | None:
    """予約期間が重なるか。どちらかの期間が不明なら None。"""
    g = (lambda x, k: x.get(k) if isinstance(x, dict) else getattr(x, k))
    a0, a1, b0, b1 = (g(a, "booking_start"), g(a, "booking_end"),
                      g(b, "booking_start"), g(b, "booking_end"))
    if not (a0 or a1) or not (b0 or b1):
        return None
    return (a0 or "0000")[:10] <= (b1 or "9999")[:10] and (b0 or "0000")[:10] <= (a1 or "9999")[:10]


def same_key(d: Deal) -> tuple:
    return (d.airline, d.origin, d.destination, d.oneway_price, d.basis == "official")


def merge_duplicates(deals: list[Deal]) -> list[Deal]:
    """同じセールを複数サイトが報じたら1件にまとめ、元記事URLは全部残す。

    仕様のキーは「航空会社＋路線＋予約開始日＋価格」だが、同じタイガーエアのセールで
    予約開始日が 10/2 と 10/1（会員先行を含めるか）に分かれた実例があったため、
    「航空会社＋路線＋価格」が同じで予約期間が重なれば同じセールとみなす。
    """
    merged: list[Deal] = []
    for d in deals:
        for m in merged:
            if same_key(m) == same_key(d) and _overlap(m, d) is not False:
                m.urls += [u for u in d.urls if u not in m.urls]
                m.sources += [s for s in d.sources if s not in m.sources]
                for k in ("booking_start", "booking_end", "travel_start", "travel_end", "official_url"):
                    if not getattr(m, k) and getattr(d, k):
                        setattr(m, k, getattr(d, k))
                # 税・片道往復・価格の根拠は、はっきり書いてあった記事の方を採る
                if m.tax == "不明" and d.tax != "不明":
                    m.tax = d.tax
                if m.trip == "不明" and d.trip != "不明":
                    m.trip = d.trip
                if m.basis == "from" and d.basis == "route":
                    m.basis = "route"
                m.conditions += [c for c in d.conditions if c not in m.conditions]
                if d.presale:
                    m.presale = True
                break
        else:
            merged.append(d)
    return merged


def already_notified(d: Deal, state: State, now: datetime) -> bool:
    for s in state.sales:
        if tuple(s["key"]) != same_key(d):
            continue
        if d.basis == "official":
            return True          # 公式運賃表は値段が変わったときだけ通知
        ov = _overlap(d, s)
        if ov:
            return True
        if ov is None and s.get("notified_at", "") >= (now - timedelta(days=SAME_SALE_DAYS)).isoformat():
            return True
    return False


def record_notified(deals: list[Deal], state: State, now: datetime) -> None:
    for d in deals:
        state.sales.append({
            "key": list(same_key(d)), "airline": d.airline, "origin": d.origin,
            "destination": d.destination, "price": d.price, "rank": d.rank,
            "booking_start": d.booking_start, "booking_end": d.booking_end,
            "urls": d.urls, "notified_at": now.isoformat(timespec="minutes"),
        })


def record_history(deals: list[Deal], state: State, now: datetime) -> None:
    """価格履歴（v2 で中央値ベースのしきい値に使う）。同じ日の同じ値は重ねない。"""
    day = now.strftime("%Y-%m-%d")
    have = {(h["date"], h["airline"], h["origin"], h["destination"], h["price"]) for h in state.history}
    for d in deals:
        if d.destination == "全路線":
            continue
        k = (day, d.airline, d.origin, d.destination, d.price)
        if k not in have:
            have.add(k)
            state.history.append({"date": day, "airline": d.airline, "origin": d.origin,
                                  "destination": d.destination, "price": d.price, "tax": d.tax,
                                  "trip": d.trip, "basis": d.basis, "source": (d.sources or [""])[0]})


# --- 取得元の健康状態（仕様「故障検知」を取得元ごとに） ---


def update_health(state: State, name: str, ok: bool, count: int, now: datetime) -> None:
    h = state.health.setdefault(name, {"fail_streak": 0, "alerted": False, "daily": {}})
    h["fail_streak"] = 0 if ok else h["fail_streak"] + 1
    if ok:
        h["alerted"] = False
    day = now.strftime("%Y-%m-%d")
    h["daily"][day] = h["daily"].get(day, 0) + count
    h["daily"] = dict(sorted(h["daily"].items())[-30:])


def health_alerts(state: State) -> list[str]:
    """2回続けて取得に失敗した取得元（ページ構造の変更などで黙って0件になるのを防ぐ）。"""
    out = []
    for name, h in state.health.items():
        if h["fail_streak"] >= 2 and not h["alerted"]:
            out.append(name)
            h["alerted"] = True
    return out


# --- 本体 ---


def _smtp() -> dict:
    return {"host": config.env("SMTP_HOST", "mail65.onamae.ne.jp"), "port": config.env("SMTP_PORT", "465"),
            "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
            "mail_to": config.env("MAIL_TO")}


def run(args) -> int:
    config.load_env()
    settings, rules, sources = config.load_settings(), Rules.load(), config.load_sources()
    if args.max_requests:
        settings["gemini"]["max_requests_per_run"] = args.max_requests
    now = now_jst()
    state = State(dry_run=args.dry_run)
    fetcher = Fetcher(settings)
    first_run = state.first_run
    status, articles, sky_deals = [], [], []

    # 1) 取得
    for src in sources:
        rows: list = []
        if src["kind"] == "rss":
            items, ok = read_feed(fetcher, src, is_seen=state.is_seen)
        elif src["kind"] == "page_links":
            items, ok = read_page_links(fetcher, src)
        elif src["kind"] == "skymark_fares":
            rows, _ = read_skymark_fares(fetcher, src["url"])
            items, ok = [], bool(rows)
            sky_deals = deals_from_skymark(rows, rules, src["url"])
        else:
            continue
        count = len(items) or len(rows)
        update_health(state, src["name"], ok, count, now)
        status.append(f"{src['name']}: {'OK ' + str(count) + '件' if ok else '取得失敗'}")
        articles.extend(items)

    # 2) 新着（同じ記事は1つに。初回は古い記事を見ない）
    uniq: dict[str, Article] = {}
    for a in articles:
        uniq.setdefault(a.url, a)
    new = [a for a in uniq.values() if not state.is_seen(a.url)]
    days = args.since_days or (FIRST_RUN_DAYS if first_run else None)
    if days:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        old = [a for a in new if a.published and a.published[:10] < cutoff]
        for a in old:
            state.mark_seen(a.url)
        new = [a for a in new if not (a.published and a.published[:10] < cutoff)]

    # 3) 一次フィルタ（AI不使用）
    max_chars = settings["extract"]["max_chars"]
    cands, body_failed = [], 0
    for a in new:
        if not title_pass(a):
            state.mark_seen(a.url)
            continue
        if not fetch_body(fetcher, a, max_chars):
            body_failed += 1          # 既読にしない＝次回もう一度取りに行く
            continue
        ok, _why = body_pass(a, rules)
        if not ok:
            state.mark_seen(a.url)
            continue
        cands.append(a)

    # 4) 抽出（前回の持ち越し分から先に）
    queue = [Article(**p) for p in state.pending] + cands
    g = settings["gemini"]
    extractor = Extractor(settings, make_gemini_caller(config.env("GEMINI_API_KEY"), g["timeout_sec"]))
    results: list[Extraction] = []
    carry: list[Article] = []
    started = time.monotonic()
    for i, a in enumerate(queue):
        if time.monotonic() - started > g["time_budget_sec"]:
            log.warning("抽出の時間上限に達した。残り%d件は次回へ（見つけた分は先に通知する）", len(queue) - i)
            carry = queue[i:]
            break
        log.info("抽出 %d/%d: %s", i + 1, len(queue), a.title[:40])
        try:
            r = extractor.extract(a)
        except AllModelsUnavailable as e:
            log.warning("AI抽出を中断（%s）。残り%d件は次回へ", e, len(queue) - i)
            carry = queue[i:]
            break
        results.append(r)
        state.mark_seen(a.url)
    state.pending = [a.to_dict() for a in carry]

    # 5) 判定
    deals: list[Deal] = []
    review = [r.article for r in results if not r.ok]   # 抽出失敗も捨てない（要確認で通知）
    for r in results:
        if r.ok:
            deals.extend(deals_from_extraction(r, rules))
    deals.extend(sky_deals)
    record_history(deals, state, now)
    deals = merge_duplicates(deals)          # 判定の前にまとめる（税などの情報を寄せてから判定するため）
    for d in deals:
        judge(d, rules, now)
    hits = [d for d in deals if d.rank]
    fresh = [d for d in hits if not already_notified(d, state, now)]
    recommend_major(fresh, rules)

    # 6) 通知
    carried = [Deal(**x) for x in state.undelivered]
    for d in carried:
        judge(d, rules, now)
    to_send = [d for d in carried if d.rank] + fresh
    summary = [
        f"新着記事 {len(new)}件 → 一次通過 {len(cands)}件 → AI抽出 {len(results)}件"
        f"（失敗{len(review)}・持ち越し{len(carry)}・本文取得失敗{body_failed}）",
        f"判定: 通知 {len(fresh)}件／通知済みで省略 {len(hits) - len(fresh)}件",
        f"Gemini: {extractor.requests}回 {extractor.used}" + (f" 使えなかったモデル {extractor.unavailable}"
                                                         if extractor.unavailable else ""),
    ] + status
    if review:
        summary.append("AI抽出に失敗した記事（要確認）:")
        summary += [f"  {a.title[:50]} {a.url}" for a in review]

    smtp = _smtp()
    if to_send or review:
        subject = notify.build_subject(to_send, now, rules) if to_send else \
            f"[那覇セール] 要確認{len(review)}件（{now.month}/{now.day}）"
        text, html_body = notify.build_body(to_send, summary, rules, now)
        sent = notify.send_mail(subject, text, html_body, smtp, dry_run=args.dry_run)
        if sent:
            record_notified(fresh, state, now)
            state.undelivered = []
        else:
            state.undelivered = [dataclasses.asdict(d) for d in to_send]
    elif args.dry_run:
        print("通知対象なし\n" + "\n".join(summary))

    # 7) 稼働確認（月曜）と故障警告
    broken = health_alerts(state)
    if broken:
        notify.send_mail(f"[那覇セール] 取得元の故障の疑い: {'、'.join(broken)}",
                         "次の取得元が2回続けて取得に失敗しました。ページ構造の変更などの可能性があります。\n\n"
                         + "\n".join(status) + "\n", None, smtp, dry_run=args.dry_run)
    if now.weekday() == 0 and slots.current_slot(now) == "morning":
        week = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        lines = [f"{n}: {sum(h['daily'].get(d, 0) for d in week)}件" for n, h in state.health.items()]
        notify.send_mail(f"[那覇セール] 稼働中・今週の取得{sum(int(l.split(': ')[1][:-1]) for l in lines)}件",
                         "今週の取得件数（取得元ごと）\n" + "\n".join(lines) + "\n", None, smtp,
                         dry_run=args.dry_run)

    slots.mark_done(now, state.last_batch)
    state.save()
    if not args.dry_run:
        git_persist()
    return 0


def git_persist() -> None:
    """data/ をコミット & push（入札監視と同じ方式）。変更が無ければ何もしない。"""
    try:
        subprocess.run(["git", "add", "data/"], cwd=config.ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=config.ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", f"chore: 状態更新 {now_jst():%Y-%m-%dT%H:%M}"],
                       cwd=config.ROOT, check=True)
        subprocess.run(["git", "push"], cwd=config.ROOT, check=True)
    except subprocess.CalledProcessError as e:
        log.error("git 操作に失敗: %s", e)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="那覇発 格安航空券ウォッチ")
    p.add_argument("--dry-run", action="store_true", help="メール送信・保存・push をせず画面に出す")
    p.add_argument("--since-days", type=int, default=None, help="この日数より古い記事は見ない")
    p.add_argument("--max-requests", type=int, default=None, help="この実行の Gemini 上限")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return run(args)
    except Exception:
        tb = traceback.format_exc()
        log.error("実行エラー:\n%s", tb)
        try:
            notify.send_mail("[那覇セール] 実行エラー", f"実行中に例外が発生しました。\n\n{tb}\n", None,
                             _smtp(), dry_run=args.dry_run)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
