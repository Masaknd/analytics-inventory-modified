"""「この期間のデータは信用しない」を変化ログから導く。

目的は、改善コックピット側の**前後比較・月次レポート・A/Bの検出力判定**が、
計測が壊れていた期間の数字をそのまま使ってしまうのを防ぐこと。

**計測設定の履歴を持っているのは管理システムだけ**なので、これは相手側では作れない。

導けるものは2種類ある。

- **期間 (periods)**: 計測が止まっていた等、その間のデータが実際におかしい区間。
  開始イベントと終了イベントの対で作る。終了が来ていなければ継続中（end=None）。
- **境界 (boundaries)**: その日を境に数字の意味が変わる一点。キーイベントの削除、
  ストリームの削除、計測設計の変更など。**またいだ前後比較は成立しない**。

### 正直さについて

変化ログが持っているのは**検知日時**であって、問題が起きた日ではない。実際の発生は
「ひとつ前のスキャン」と「検知したスキャン」の間のどこかにある。ここでは検知日時を
そのまま返し、フィールド名も detected_from / detected_to にして、**発生日が分かる
ふりをしない**。判定できないときは False ではなく "unknown" を返す。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

# 期間を開く／閉じるイベント。対にならないもの（開きっぱなし）は継続中として扱う。
_PERIOD_OPEN = {
    "tracking_stopped": ("critical", "計測が停止していました"),
    "property_removed": ("critical", "プロパティが一覧から消えていました（削除または権限喪失）"),
    "tags_all_removed": ("critical", "GTMコンテナのタグが全て無くなっていました"),
    "mid_removed": ("critical", "GTMから測定IDが外れていました"),
    "ecommerce_stopped": ("warn", "eコマース計測が止まっていました"),
}
_PERIOD_CLOSE = {
    "tracking_resumed": "tracking_stopped",
    "property_added": "property_removed",
    "mid_added": "mid_removed",
    "tags_increased": "tags_all_removed",
}

# その日を境に数字の意味が変わる一点。またいだ前後比較は成立しない。
_BOUNDARY = {
    "key_events_removed": ("critical", "キーイベントが削除されました（CVの定義が変わっています）"),
    "key_events_added": ("info", "キーイベントが追加されました（CVの定義が変わっています）"),
    "stream_removed": ("critical", "データストリームが削除されました"),
    "cd_decreased": ("warn", "カスタムディメンションが減りました"),
    "sessions_plunge": ("warn", "セッションが急減しました（計測不具合の可能性）"),
    "clicks_plunge": ("warn", "検索クリックが急減しました"),
    "tags_decreased": ("warn", "GTMのタグが減りました"),
    "version_published": ("info", "GTMコンテナの新バージョンが公開されました"),
}

VERDICTS = ("ok", "unreliable", "unknown")


def _day(value: Any) -> Optional[str]:
    text = str(value or "")[:10]
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _entity_events(change_events: list[dict] | None, entity_id: str,
                   kinds: tuple[str, ...] = ("ga4",)) -> list[dict]:
    """1つの対象に関する変化イベントを、古い順に並べて返す。"""
    target = str(entity_id)
    out = []
    for e in change_events or []:
        if not isinstance(e, dict):
            continue
        if e.get("kind") not in kinds or str(e.get("entity_id") or "") != target:
            continue
        if _day(e.get("detected_at") or e.get("snapshot")):
            out.append(e)
    out.sort(key=lambda e: str(e.get("detected_at") or e.get("snapshot") or ""))
    return out


def build(change_events: list[dict] | None, entity_id: str,
          kinds: tuple[str, ...] = ("ga4",)) -> dict:
    """変化ログから、信用できない期間と境界を組み立てる（純粋関数）。"""
    events = _entity_events(change_events, entity_id, kinds)
    periods: list[dict] = []
    boundaries: list[dict] = []
    open_by_reason: dict[str, dict] = {}

    for e in events:
        ctype = str(e.get("change_type") or "")
        at = str(e.get("detected_at") or e.get("snapshot") or "")
        day = _day(at)
        if ctype in _PERIOD_OPEN:
            if ctype in open_by_reason:
                continue  # 既に開いている同じ理由は重ねない
            level, label = _PERIOD_OPEN[ctype]
            period = {"reason": ctype, "level": level, "label": label,
                      "detected_from": day, "detected_to": None, "ongoing": True}
            open_by_reason[ctype] = period
            periods.append(period)
        elif ctype in _PERIOD_CLOSE:
            opened = open_by_reason.pop(_PERIOD_CLOSE[ctype], None)
            if opened is not None:
                opened["detected_to"] = day
                opened["ongoing"] = False
        if ctype in _BOUNDARY:
            level, label = _BOUNDARY[ctype]
            boundaries.append({"reason": ctype, "level": level, "label": label,
                               "detected_at": day,
                               "message": str(e.get("message") or "")[:200]})

    periods.sort(key=lambda p: str(p.get("detected_from") or ""), reverse=True)
    boundaries.sort(key=lambda b: str(b.get("detected_at") or ""), reverse=True)
    return {
        "periods": periods,
        "boundaries": boundaries,
        "note": ("日付は変化ログの検知日です。実際の発生はひとつ前のスキャンとの間の"
                 "どこかにあります（発生日そのものは分かりません）。"),
    }


def log_covers(change_events: list[dict] | None, start: str) -> Optional[bool]:
    """変化ログが start の時点まで遡れているか。

    ログがその前から始まっていれば True。ログの最も古い記録が start より後なら、
    その期間に何があったかは**分からない**ので False を返し、判定は unknown に倒す。
    記録が1件も無ければ None（ログ自体が無い＝やはり分からない）。
    """
    days = [_day(e.get("detected_at") or e.get("snapshot"))
            for e in (change_events or []) if isinstance(e, dict)]
    days = [d for d in days if d]
    if not days:
        return None
    return min(days) <= start


def _overlaps(p_from: Optional[str], p_to: Optional[str], start: str, end: str) -> bool:
    """期間 [p_from, p_to]（p_to=None は継続中）が窓 [start, end] と重なるか。"""
    if not p_from:
        return False
    if p_from > end:
        return False
    if p_to is not None and p_to < start:
        return False
    return True


def judge_window(change_events: list[dict] | None, entity_id: str, start: str, end: str,
                 kinds: tuple[str, ...] = ("ga4",)) -> dict:
    """この窓の数字を使ってよいか。

    - "unreliable": 壊れていた期間と重なる、または境界をまたいでいる
    - "ok": 重なりも境界も無く、かつ変化ログがその期間をカバーしている
    - "unknown": 変化ログがその期間まで遡れていない（＝「問題なし」とは言えない）

    **「分からない」を「問題なし」に化けさせない**のがこの関数の肝。
    """
    s, e = _day(start), _day(end)
    if not s or not e or s > e:
        return {"verdict": "unknown", "reason": "期間の指定が不正です",
                "window": {"start": start, "end": end},
                "periods": [], "boundaries": []}

    built = build(change_events, entity_id, kinds)
    hit_periods = [p for p in built["periods"]
                   if _overlaps(p.get("detected_from"), p.get("detected_to"), s, e)]
    hit_boundaries = [b for b in built["boundaries"]
                      if b.get("detected_at") and s <= b["detected_at"] <= e]

    covered = log_covers(change_events, s)
    if hit_periods or hit_boundaries:
        verdict = "unreliable"
    elif covered:
        verdict = "ok"
    else:
        verdict = "unknown"

    return {
        "verdict": verdict,
        "window": {"start": s, "end": e},
        "log_covers_window": covered,
        "periods": hit_periods,
        "boundaries": hit_boundaries,
        "note": built["note"],
    }


def default_window(days: int = 30, today: Optional[date] = None) -> dict:
    """窓の指定が無いときに見る既定の範囲（直近 N 日）。"""
    base = today or datetime.now(timezone.utc).date()
    return {"start": (base - timedelta(days=days)).isoformat(), "end": base.isoformat()}
