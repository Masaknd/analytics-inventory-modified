"""改善コックピットから受け取る「施策・診断の実施日」の台帳。

目的は1つ。**KPIが動いたとき、施策の効果なのか計測設定が変わったせいなのかを切り分ける**こと。

変化ログ（changes.py）は「計測設定が変わった」ことしか知らず、改善コックピットは
「施策を打った」ことしか知らない。この台帳が後者を受け取り、変化ログと同じ時間軸に並べる。

設計の前提:

- **単体で動く**。連携していなければこの台帳は空のまま＝変化ログは従来どおり。
- **受け取るだけで、こちらから取りに行かない**。相手が落ちていても影響しない。
- **書き込みなので連携キーで保護する**（読み取り系APIと違い、誰でも叩けてはいけない）。
  キーは integration.py が持つ。
- 同じ施策の更新は上書き（source + external_id で突き合わせ）。相手が何度送っても増えない。

data/external_events.json に保存。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from config import DATA_DIR

EXTERNAL_EVENTS_PATH = DATA_DIR / "external_events.json"
_lock = Lock()

# 台帳の上限。変化ログ（MAX_LOG_ENTRIES=1000）より小さくてよい＝施策はそこまで多くない。
MAX_EVENTS = 500

# 1リクエストで受け取る最大件数（取り込み時の上限）
MAX_BATCH = 200

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# 相手が何を送ってきたかに関わらず、こちらが解釈する種別はこの2つだけ。
# 未知の種別は "other" に倒す（画面の分岐を増やさないため）。
KINDS = ("initiative", "diagnosis", "other")

_MAX_TITLE = 200
_MAX_URL = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, limit: int) -> str:
    """制御文字を落として長さを切る。相手の入力をそのまま画面に出さないため。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    return text[:limit]


def _clean_url(value: Any) -> Optional[str]:
    """http/https のみ。相手の画面へ戻るリンクに使うので他スキームは落とす。"""
    url = str(value or "").strip()
    if not url[:8].lower().startswith(("http://", "https:/")):
        return None
    if " " in url or "\n" in url:
        return None
    return url[:_MAX_URL]


def normalize(raw: Any, *, source: str = "cockpit") -> Optional[dict]:
    """受け取った1件を台帳の形に整える。要件を満たさなければ None（＝受け取らない）。

    必須は external_id・occurred_on（YYYY-MM-DD）・title の3つ。
    property_id は任意だが、無いとプロパティに紐づけられず全体の時間軸にしか出ない。

    `deleted: true` のときは墓標（`_deleted`）を返す。相手側で施策が取り消されたときに
    こちらにだけ残り続けないようにするためで、墓標は保存せず既存の記録を消すのに使う。
    """
    if not isinstance(raw, dict):
        return None
    ext_id = str(raw.get("external_id") or raw.get("id") or "").strip()
    if not _ID_RE.match(ext_id):
        return None
    if raw.get("deleted"):
        # 消す指示に実施日やタイトルは要らない
        return {"source": source, "external_id": ext_id, "_deleted": True}
    occurred_on = str(raw.get("occurred_on") or "").strip()
    if not _DATE_RE.match(occurred_on):
        return None
    try:
        datetime.strptime(occurred_on, "%Y-%m-%d")
    except ValueError:
        return None
    title = _clean_text(raw.get("title"), _MAX_TITLE)
    if not title:
        return None

    kind = str(raw.get("kind") or "initiative").strip()
    if kind not in KINDS:
        kind = "other"
    pid = str(raw.get("property_id") or "").strip()
    if pid and not re.match(r"^[0-9]{1,20}$", pid):
        pid = ""

    return {
        "source": source,
        "external_id": ext_id,
        "kind": kind,
        "property_id": pid or None,
        "title": title,
        "occurred_on": occurred_on,
        "status": _clean_text(raw.get("status"), 32) or None,
        "url": _clean_url(raw.get("url")),
        "received_at": _now(),
    }


def _load() -> list[dict]:
    if not EXTERNAL_EVENTS_PATH.exists():
        return []
    try:
        data = json.loads(EXTERNAL_EVENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write(events: list[dict]) -> None:
    tmp = EXTERNAL_EVENTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, EXTERNAL_EVENTS_PATH)


def merge(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """source + external_id で突き合わせて上書き。実施日の新しい順に並べ、上限で切る。

    純粋関数（テストしやすいように副作用を持たせない）。
    """
    by_key: dict[tuple, dict] = {}
    for e in existing:
        if isinstance(e, dict):
            by_key[(e.get("source"), e.get("external_id"))] = e
    for e in incoming:
        key = (e.get("source"), e.get("external_id"))
        if e.get("_deleted"):
            by_key.pop(key, None)  # 墓標は保存しない。既存の記録を消すだけ
        else:
            by_key[key] = e
    out = list(by_key.values())
    out.sort(key=lambda e: (str(e.get("occurred_on") or ""), str(e.get("received_at") or "")),
             reverse=True)
    return out[:MAX_EVENTS]


def record(raw_events: Any, *, source: str = "cockpit") -> dict:
    """受け取ったイベント群を取り込む。結果は {accepted, rejected, total}。

    1件でも不正があっても全体を失敗にしない（相手の1件の不備で連携が止まらないように）。
    """
    if isinstance(raw_events, dict):
        raw_events = [raw_events]
    if not isinstance(raw_events, list):
        return {"accepted": 0, "rejected": 0, "total": 0, "error": "events は配列で送ってください"}
    raw_events = raw_events[:MAX_BATCH]

    normalized, rejected, deleted = [], 0, 0
    for raw in raw_events:
        item = normalize(raw, source=source)
        if item is None:
            rejected += 1
        elif item.get("_deleted"):
            deleted += 1
            normalized.append(item)
        else:
            normalized.append(item)

    with _lock:
        merged = merge(_load(), normalized)
        _write(merged)
    return {"accepted": len(normalized) - deleted, "deleted": deleted,
            "rejected": rejected, "total": len(merged)}


def load(limit: int = MAX_EVENTS) -> list[dict]:
    """台帳を実施日の新しい順で返す。"""
    return _load()[:limit]


def for_property(pid: str, limit: int = MAX_EVENTS) -> list[dict]:
    """1プロパティに紐づくものだけ。"""
    target = str(pid)
    return [e for e in load(limit=MAX_EVENTS) if str(e.get("property_id") or "") == target][:limit]


def delete_source(source: str) -> int:
    """連携を解除したときに、その送り元の記録をまとめて消す。消した件数を返す。"""
    with _lock:
        existing = _load()
        kept = [e for e in existing if e.get("source") != source]
        _write(kept)
        return len(existing) - len(kept)


def timeline(change_events: list[dict] | None, ext_events: list[dict] | None) -> list[dict]:
    """変化ログと施策を1本の時間軸にまとめる（新しい順）。

    変化ログは検出日時（ISO日時）、施策は実施日（日付）なので、並べ替えは
    **日付の文字列**で行う。同じ日の中での前後関係は保証しない（施策の実施時刻は
    そもそも分からないため、分かるふりをしない）。
    """
    rows: list[dict] = []
    for e in change_events or []:
        if not isinstance(e, dict):
            continue
        at = str(e.get("detected_at") or e.get("snapshot") or "")
        rows.append({"_row": "change", "_date": at[:10], "_at": at, "event": e})
    for e in ext_events or []:
        if not isinstance(e, dict):
            continue
        day = str(e.get("occurred_on") or "")
        rows.append({"_row": "external", "_date": day, "_at": day, "event": e})
    rows.sort(key=lambda r: (r["_date"], r["_row"] == "change", r["_at"]), reverse=True)
    return rows
