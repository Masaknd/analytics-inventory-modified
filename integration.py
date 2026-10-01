"""姉妹ツール（改善コックピット / UX-Testing-Tools）との連携。

役割は2つ。

1. **外に出すペイロードの形を決める**（純粋関数）。`/api/property/<pid>/summary` が返す
   計測品質サマリーを組み立てる。
2. **連携先（改善コックピット）のURLを保持する**。data/integration.json に保存。

設計の前提:

- **どちらのツールも単体で動く**。連携先が未設定でも管理システムは通常どおり動き、
  相手が古い版でも追加フィールドを無視すれば従来どおり読める（既存キーは変えない＝後方互換）。
  このモジュールが失敗しても画面・収集を止めないよう、呼び出し側は try/except で包む。
- **取れていないものは False ではなく None（未確認）**。「計測できていない」を「問題なし」に
  化けさせないため。quality.py と同じ規律で、判定は summary["collected"] のフラグを見る。
- 判定を二重実装しない。監視除外の実効判定は annotations、変化検知は changes が正。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Optional

import annotations as ann
from config import DATA_DIR

# collectors.ga4_data.list_events の limit と揃える。
# イベント一覧がこの件数に達していたら「一覧の外に居るだけ」の可能性を捨てられないので、
# 「0件」と断定せず未確認に倒す。
EVENTS_REPORT_LIMIT = 500

HOSTNAME_LIMIT = 10
FINDING_LIMIT = 5
CHANGES_DAYS = 30

# この版が summary で返せる追加フィールドの名札。相手側はバージョン文字列を解釈せず、
# /api/health の api_features を見て出し分けられる。
API_FEATURES = ["property_summary_v2"]


# ============================================================
#  ペイロード組み立て（純粋関数）
# ============================================================

def _as_dict(value: Any) -> dict:
    """dict でなければ空 dict として扱う。

    detail は収集時に書いたJSONをそのまま読むので、収集が途中で壊れた版のファイルが
    残っていることがある。外向きAPIで型を理由に500を返さないための防波堤。
    """
    return value if isinstance(value, dict) else {}


def _parse_dt(value: Any) -> Optional[datetime]:
    """ISO8601 文字列を aware な datetime にする。naive は UTC とみなす。失敗は None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def monitoring_state(ann_entry: dict | None) -> dict:
    """監視除外の実効状態。

    相手側が「管理システムで監視対象外のプロパティ」を品質警告として出さないために使う。
    恒久除外とスヌーズを区別して返す（実効判定そのものは annotations が正）。
    """
    entry = ann_entry or {}
    effective = ann.is_effectively_excluded(entry)
    permanent = bool(entry.get("excluded"))
    reason = "permanent" if permanent else ("snoozed" if effective else None)
    return {
        "excluded": effective,
        "reason": reason,
        # 期限切れのスヌーズ日付を渡すと誤読されるので、いま効いているときだけ返す
        "snooze_until": entry.get("snooze_until") if reason == "snoozed" else None,
    }


def hostnames(detail: dict, limit: int = HOSTNAME_LIMIT) -> dict:
    """ホスト名棚卸しの上位。

    相手側が診断しようとしているURLが、そもそも計測対象ホストに入っているかの照合に使う
    （ステージングや翻訳プロキシを本番と思って診断する事故を防ぐ）。
    """
    collected = _as_dict(_as_dict(detail).get("summary")).get("collected")
    collected = _as_dict(collected)
    raw = _as_dict(_as_dict(detail).get("hostnames")).get("rows")
    raw = raw if isinstance(raw, list) else []
    rows = []
    for r in raw[:limit]:
        if not isinstance(r, dict):
            continue
        host = str(r.get("hostname") or "").strip()
        if not host:
            continue
        try:
            sessions = int(r.get("sessions") or 0)
        except (TypeError, ValueError):
            sessions = 0
        rows.append({"hostname": host, "sessions": sessions})
    # truncated は「上位 limit 件で切ったか」だけを表す。相手側はこれを見て
    # 「一覧に無い＝計測されていない」と断定して良いかを決める。
    # rows は空ホスト名の行を落とすので len(raw) と len(rows) の比較では判定できない。
    return {"ok": bool(collected.get("hostnames")), "total_count": len(raw),
            "truncated": len(raw) > limit, "rows": rows}


def key_events_state(detail: dict) -> dict:
    """キーイベントの定義数と実測。

    - defined: キーイベントの定義数
    - measured_count: 直近の実績でのキーイベント合計イベント数（未確認は None）
    - zero: 「定義はあるのに1件も計測されていない」なら True。
      定義が0件のとき・実績が取れていないとき・イベント一覧が上限に達しているときは None。
      定義0件は `defined` で判断する（別の問題なので zero には混ぜない）。
    """
    summary = _as_dict(_as_dict(detail).get("summary"))
    raw_names = summary.get("key_event_names")
    names = {str(n) for n in (raw_names if isinstance(raw_names, list) else []) if n}
    try:
        defined = int(summary.get("key_event_count") or len(names))
    except (TypeError, ValueError):
        defined = len(names)
    unknown = {"defined": defined, "measured_count": None, "zero": None}

    collected = _as_dict(summary.get("collected"))
    events = _as_dict(detail).get("events")
    if not collected.get("events") or not isinstance(events, list):
        return unknown
    if defined == 0:
        return unknown

    total = 0
    for row in events:
        if isinstance(row, dict) and str(row.get("event_name") or "") in names:
            try:
                total += int(row.get("event_count") or 0)
            except (TypeError, ValueError):
                pass
    if total == 0 and len(events) >= EVENTS_REPORT_LIMIT:
        return unknown
    return {"defined": defined, "measured_count": total, "zero": total == 0}


def recent_changes(change_events: list[dict] | None, property_id: str,
                   days: int = CHANGES_DAYS, now: datetime | None = None) -> dict:
    """このプロパティに関する直近の変化イベントの件数と最新日。

    相手側の「診断参照の陳腐化警告」や前後比較の注記に使う。
    change_events は changes.filter_excluded(changes.load_log(...)) 済みの一覧を渡す
    （監視除外の間引きは changes が正）。
    """
    pid = str(property_id)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out = {"days": days, "total": 0, "critical": 0, "warn": 0, "info": 0, "latest_at": None}
    latest_dt: Optional[datetime] = None
    for e in change_events or []:
        if not isinstance(e, dict):
            continue
        if e.get("kind") != "ga4" or str(e.get("entity_id") or "") != pid:
            continue
        dt = _parse_dt(e.get("detected_at"))
        if dt is not None and dt < cutoff:
            continue
        sev = e.get("severity")
        if sev in ("critical", "warn", "info"):
            out[sev] += 1
        out["total"] += 1
        if dt is not None and (latest_dt is None or dt > latest_dt):
            latest_dt = dt
            out["latest_at"] = str(e.get("detected_at"))
    return out


def top_findings(detail: dict, limit: int = FINDING_LIMIT) -> list[dict]:
    """品質チェックの指摘を重大な順に上位 N 件。"""
    findings = _as_dict(_as_dict(detail).get("quality")).get("findings")
    findings = [f for f in (findings if isinstance(findings, list) else []) if isinstance(f, dict)]
    rank = {"error": 0, "warn": 1, "info": 2}
    ordered = sorted(findings, key=lambda f: rank.get(f.get("level"), 3))
    return [
        {"severity": f.get("level"), "category": f.get("category"), "title": f.get("title")}
        for f in ordered[:limit]
    ]


def property_payload(pid: str, detail: dict, *, ann_entry: dict | None = None,
                     change_events: list[dict] | None = None,
                     changes_days: int = CHANGES_DAYS) -> dict:
    """`GET /api/property/<pid>/summary` の応答本体。

    detail["summary"] は呼び出し側で health.enrich_properties を通した状態であることを
    前提にする（grade/score/alert_count は保存されず都度算出のため）。

    既存キー（property_id〜property_url）は変更しない。追加分は後方互換の追記。
    """
    summary = _as_dict(_as_dict(detail).get("summary"))
    return {
        # ── 既存（v1）。並びも含めて変えない ──
        "property_id": pid,
        "display_name": summary.get("display_name"),
        "health_grade": summary.get("health_grade"),
        "health_score": summary.get("health_score"),
        "key_event_names": summary.get("key_event_names") or [],
        "quality": summary.get("quality"),
        "alerts_count": summary.get("alert_count"),
        "top_findings": top_findings(detail),
        "collected_at": summary.get("collected_at"),
        "property_url": f"/property/{pid}",
        # ── 追加（v2）。相手が古い版なら単に無視される ──
        "monitoring": monitoring_state(ann_entry),
        "hostnames": hostnames(detail),
        "key_events": key_events_state(detail),
        "recent_changes": recent_changes(change_events, pid, days=changes_days),
    }


# ============================================================
#  連携先（改善コックピット）のURL
# ============================================================

INTEGRATION_PATH = DATA_DIR / "integration.json"
_lock = Lock()

# 環境変数が設定されていればそちらを優先する（notifications の Webhook URL と同じ扱い）。
COCKPIT_URL_ENV = "COCKPIT_URL"

_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def normalize_url(value: Any) -> Optional[str]:
    """http/https のURLだけ受け付け、末尾のスラッシュを落とす。それ以外は None。"""
    url = str(value or "").strip()
    if not url:
        return None
    if not _URL_RE.match(url):
        return None
    return url.rstrip("/")


def _load() -> dict:
    if not INTEGRATION_PATH.exists():
        return {}
    try:
        d = json.loads(INTEGRATION_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cockpit_url() -> Optional[str]:
    """改善コックピットのURL（未設定なら None＝連携なしで通常動作）。"""
    env = normalize_url(os.environ.get(COCKPIT_URL_ENV))
    if env:
        return env
    return normalize_url(_load().get("cockpit_url"))


def cockpit_url_from_env() -> bool:
    """URLが環境変数由来か（画面で「環境変数が優先」と伝えるため）。"""
    return bool(normalize_url(os.environ.get(COCKPIT_URL_ENV)))


def save_cockpit_url(value: Any) -> Optional[str]:
    """URLを保存する。空文字・不正な値は未設定（None）として保存＝連携を外す。"""
    url = normalize_url(value)
    with _lock:
        d = _load()
        d["cockpit_url"] = url
        tmp = INTEGRATION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, INTEGRATION_PATH)
    return url
