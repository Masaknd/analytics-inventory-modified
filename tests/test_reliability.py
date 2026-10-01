"""reliability.py のユニットテスト。

一番大事なのは test_unknown_when_log_does_not_cover_window —
**変化ログがその期間まで遡れていないとき「ok（問題なし）」と言わない**こと。
ここが崩れると、まだ監視していなかった時期の数字を「健全な期間のデータ」として
相手ツールの前後比較に使わせてしまう。

実行: リポジトリルートで `python -m pytest`
"""
import reliability as rel


def _ev(change_type, at, kind="ga4", pid="308259408", severity="warn"):
    return {"kind": kind, "entity_id": pid, "severity": severity,
            "change_type": change_type, "detected_at": at + "T12:00:00+00:00",
            "message": "テスト"}


PID = "308259408"


# ============================================================
#  期間（開始と終了の対）
# ============================================================

def test_period_opens_and_closes():
    events = [_ev("tracking_stopped", "2026-07-03"), _ev("tracking_resumed", "2026-07-10")]
    out = rel.build(events, PID)
    assert len(out["periods"]) == 1
    p = out["periods"][0]
    assert p["detected_from"] == "2026-07-03"
    assert p["detected_to"] == "2026-07-10"
    assert p["ongoing"] is False
    assert p["level"] == "critical"


def test_period_without_close_is_ongoing():
    out = rel.build([_ev("tracking_stopped", "2026-07-03")], PID)
    p = out["periods"][0]
    assert p["detected_to"] is None and p["ongoing"] is True


def test_duplicate_open_does_not_stack():
    events = [_ev("tracking_stopped", "2026-07-03"), _ev("tracking_stopped", "2026-07-05")]
    assert len(rel.build(events, PID)["periods"]) == 1


def test_close_without_open_is_ignored():
    assert rel.build([_ev("tracking_resumed", "2026-07-10")], PID)["periods"] == []


def test_only_matching_entity_and_kind():
    events = [_ev("tracking_stopped", "2026-07-03", pid="999"),
              _ev("tracking_stopped", "2026-07-03", kind="gtm")]
    assert rel.build(events, PID)["periods"] == []


# ============================================================
#  境界（その日を境に数字の意味が変わる）
# ============================================================

def test_boundary_collected():
    out = rel.build([_ev("key_events_removed", "2026-08-01", severity="critical")], PID)
    assert len(out["boundaries"]) == 1
    assert out["boundaries"][0]["detected_at"] == "2026-08-01"
    assert out["boundaries"][0]["level"] == "critical"


def test_unknown_change_type_is_neither_period_nor_boundary():
    out = rel.build([_ev("なにか新しい種別", "2026-08-01")], PID)
    assert out["periods"] == [] and out["boundaries"] == []


def test_build_handles_garbage():
    out = rel.build(["nope", None, {"kind": "ga4"}], PID)
    assert out["periods"] == [] and out["boundaries"] == []
    assert rel.build(None, PID)["periods"] == []


# ============================================================
#  judge_window — ②の肝
# ============================================================

def test_ok_when_log_covers_and_nothing_hit():
    events = [_ev("version_published", "2026-01-01", kind="gtm")]  # 別対象＝影響なし
    events.append(_ev("tracking_resumed", "2026-01-01"))  # ログの開始点を作る
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "ok"
    assert out["log_covers_window"] is True


def test_unknown_when_log_does_not_cover_window():
    """ログがその期間より後から始まっている＝何があったか分からない。okにしない。"""
    events = [_ev("tracking_resumed", "2026-08-01")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "unknown"
    assert out["log_covers_window"] is False


def test_unknown_when_no_log_at_all():
    out = rel.judge_window([], PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "unknown"
    assert out["log_covers_window"] is None


def test_unreliable_when_period_overlaps_window():
    events = [_ev("tracking_stopped", "2026-06-10"), _ev("tracking_resumed", "2026-06-20")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "unreliable"
    assert len(out["periods"]) == 1


def test_unreliable_when_ongoing_period_started_before_window():
    events = [_ev("tracking_stopped", "2026-05-01")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "unreliable"


def test_period_entirely_before_window_is_not_hit():
    events = [_ev("tracking_stopped", "2026-04-01"), _ev("tracking_resumed", "2026-04-10")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["periods"] == []
    assert out["verdict"] == "ok"


def test_period_entirely_after_window_is_not_hit():
    events = [_ev("tracking_resumed", "2026-01-01"),
              _ev("tracking_stopped", "2026-09-01")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["periods"] == []


def test_unreliable_when_boundary_inside_window():
    events = [_ev("tracking_resumed", "2026-01-01"),
              _ev("key_events_removed", "2026-06-15", severity="critical")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "unreliable"
    assert len(out["boundaries"]) == 1


def test_boundary_outside_window_is_not_hit():
    events = [_ev("tracking_resumed", "2026-01-01"),
              _ev("key_events_removed", "2026-07-15", severity="critical")]
    out = rel.judge_window(events, PID, "2026-06-01", "2026-06-30")
    assert out["verdict"] == "ok"


def test_invalid_window_is_unknown():
    for s, e in (("2026-06-30", "2026-06-01"), ("ひどい", "2026-06-30"), ("", "")):
        assert rel.judge_window([], PID, s, e)["verdict"] == "unknown"


def test_default_window_is_30_days():
    from datetime import date
    w = rel.default_window(days=30, today=date(2026, 7, 1))
    assert w == {"start": "2026-06-01", "end": "2026-07-01"}
