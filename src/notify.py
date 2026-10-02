"""メール（仕様「メール通知仕様」）。

該当が1件以上ある回だけ送る。0件の日は送らず、毎週月曜に稼働確認だけ送る。
締切を太字にするため、テキストとHTMLの両方を入れたメールにする。
"""
from __future__ import annotations

import html
import logging
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

from .judge import ALL_ROUTES, CHEAP, INSTANT, JST, RANK_ORDER, Deal, Rules, hours_to_deadline

log = logging.getLogger(__name__)
WEEKDAYS = "月火水木金土日"


def fmt_dt(s: str | None) -> str:
    if not s:
        return "不明"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return s
    day = f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"
    return f"{day}{d:%H:%M}" if "T" in s else day


def fmt_date(s: str | None) -> str:
    if not s:
        return "不明"
    try:
        d = datetime.fromisoformat(s[:10])
        return f"{d.year}/{d.month}/{d.day}"
    except ValueError:
        return s


def sort_deals(deals: list[Deal]) -> list[Deal]:
    """即買い → 安い → 全路線。同じランクは予約締切が近い順（不明は後ろ）。"""
    return sorted(deals, key=lambda d: (RANK_ORDER.get(d.rank, 9), d.booking_end or "9999"))


def build_subject(deals: list[Deal], now: datetime, rules: Rules) -> str:
    n_i = sum(d.rank == INSTANT for d in deals)
    n_c = sum(d.rank == CHEAP for d in deals)
    n_a = sum(d.rank == ALL_ROUTES for d in deals)
    parts = [f"即買い{n_i}件", f"安い{n_c}件"] + ([f"全路線{n_a}件"] if n_a else [])
    urgent = any((h := hours_to_deadline(d, now)) is not None and 0 <= h <= rules.urgent_hours
                 for d in deals)
    return f"{'【急ぎ】' if urgent else ''}[那覇セール] {'・'.join(parts)}（{now.month}/{now.day}）"


def _conditions(d: Deal) -> list[str]:
    out = []
    if d.member_only:
        out.append("会員限定")
    if d.presale:
        out.append("会員先行販売あり")
    out.extend(c for c in d.conditions if c not in out)
    if d.airline_type == "LCC" and not any("荷物" in c and ("込" in c or "無料" in c)
                                           for c in out + [d.note or ""]):
        out.append("預け荷物は別料金")
    return out


def _block(d: Deal, rules: Rules, now: datetime, as_html: bool) -> str:
    e = html.escape if as_html else (lambda x: x)
    bold = (lambda x: f"<b>{e(x)}</b>") if as_html else (lambda x: f"【{x}】")
    lines = []
    if d.rank == ALL_ROUTES:
        lines.append(f"■【全路線セール】{e(d.airline)}{e('「' + d.sale_name + '」' if d.sale_name else '')}")
        lines.append(f"  {e(d.reason)}")
    else:
        route = f"{rules.dest_name(d.origin)}→{rules.dest_name(d.destination)}"
        lines.append(f"■【{e(d.rank)}】{e(route)}　{e(d.airline)}"
                     + (e(f"「{d.sale_name}」") if d.sale_name else ""))
        tax = d.tax if d.tax != "不明" else "税込/税別不明"
        trip = d.trip if d.trip != "不明" else "片道/往復不明"
        basis = "（路線共通の最安値）" if d.basis == "from" else ""
        lines.append(f"  {d.price:,}円（{e(tax)}・{e(trip)}）{e(basis)}　※最安値・日付で変動")
        lines.append(f"  理由: {e(d.reason)}")
    left = hours_to_deadline(d, now)
    left_s = f"・あと{int(left)}時間" if left is not None and 0 <= left <= 72 else ""
    lines.append(f"  予約: {e(fmt_dt(d.booking_start))}〜{bold('締切 ' + fmt_dt(d.booking_end) + left_s)}")
    lines.append(f"  搭乗: {e(fmt_date(d.travel_start))}〜{e(fmt_date(d.travel_end))}")
    if d.excluded_periods:
        ex = "、".join(f"{fmt_date(p.get('start'))}〜{fmt_date(p.get('end'))}" for p in d.excluded_periods)
        lines.append(f"  除外: {e(ex)}")
    conds = _conditions(d)
    if conds:
        lines.append(f"  条件: {e('／'.join(conds))}")
    if d.note:
        lines.append(f"  メモ: {e(d.note)}")
    for i, u in enumerate(d.urls):
        label = "  記事: " if i == 0 else "        "
        lines.append(label + (f'<a href="{e(u)}">{e(u)}</a>' if as_html else u))
    if d.official_url:
        lines.append("  公式: " + (f'<a href="{e(d.official_url)}">{e(d.official_url)}</a>'
                                   if as_html else d.official_url))
    return ("<br>\n".join(lines)) if as_html else "\n".join(lines)


def build_body(deals: list[Deal], summary: list[str], rules: Rules, now: datetime) -> tuple[str, str]:
    deals = sort_deals(deals)
    text = "\n\n".join(_block(d, rules, now, False) for d in deals)
    text += "\n\n--- 実行サマリ ---\n" + "\n".join(summary) + "\n"
    blocks = "<br><br>\n".join(_block(d, rules, now, True) for d in deals)
    foot = "<br>\n".join(html.escape(s) for s in summary)
    html_body = (f'<div style="font-family:sans-serif;font-size:14px;line-height:1.6">{blocks}'
                 f'<hr><div style="color:#666;font-size:12px">{foot}</div></div>')
    return text, html_body


def send_mail(subject: str, text: str, html_body: str | None, cfg: dict, dry_run: bool = False) -> bool:
    if dry_run:
        print("=" * 64 + f"\nSubject: {subject}\n" + "=" * 64 + f"\n{text}")
        return True
    missing = [k for k in ("host", "user", "password", "mail_to") if not cfg.get(k)]
    if missing:
        log.error("SMTP 設定が不足: %s", ", ".join(missing))
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["user"]
    msg["To"] = cfg["mail_to"]
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), timeout=30) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(cfg["user"], [cfg["mail_to"]], msg.as_string())
        log.info("メール送信完了: %s", subject)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.error("メール送信失敗: %s", e)
        return False
