"""integration.py のユニットテスト。

一番大事なのは test_unknown_never_becomes_false —
「取れていないものを False（問題なし）として外に出さない」ことを固定する。
相手ツール（改善コックピット）はこの値を見て警告を出す／出さないを決めるので、
未確認が「問題なし」に化けると、壊れた計測のまま改善施策が評価されてしまう。

後方互換（既存キーを変えない）も test_v1_keys_unchanged で固定する。

実行: リポジトリルートで `python -m pytest`
"""
from datetime import date, datetime, timedelta, timezone

import integration


def _detail(collected=None, summary=None, **kwargs):
    s = {"collected": collected or {}}
    s.update(summary or {})
    d = {"summary": s}
    d.update(kwargs)
    return d


# ============================================================
#  monitoring_state
# ============================================================

def test_monitoring_none_is_not_excluded():
    assert integration.monitoring_state(None) == {
        "excluded": False, "reason": None, "snooze_until": None}


def test_monitoring_permanent():
    st = integration.monitoring_state({"excluded": True})
    assert st["excluded"] is True
    assert st["reason"] == "permanent"


def test_monitoring_snoozed_returns_date():
    future = (date.today() + timedelta(days=5)).isoformat()
    st = integration.monitoring_state({"snooze_until": future})
    assert st["excluded"] is True
    assert st["reason"] == "snoozed"
    assert st["snooze_until"] == future


def test_monitoring_expired_snooze_does_not_leak_date():
    """期限切れのスヌーズ日付を渡すと相手が「停止中」と誤読するので返さない。"""
    past = (date.today() - timedelta(days=1)).isoformat()
    st = integration.monitoring_state({"snooze_until": past})
    assert st["excluded"] is False
    assert st["reason"] is None
    assert st["snooze_until"] is None


# ============================================================
#  hostnames
# ============================================================

def test_hostnames_not_collected_is_not_ok():
    d = _detail(collected={"hostnames": False},
                hostnames={"ok": False, "skipped": True, "rows": []})
    assert integration.hostnames(d) == {"ok": False, "total_count": 0,
                                        "truncated": False, "rows": []}


def test_hostnames_rows_and_limit():
    rows = [{"hostname": f"h{i}.example.com", "sessions": 100 - i} for i in range(15)]
    d = _detail(collected={"hostnames": True}, hostnames={"ok": True, "rows": rows})
    out = integration.hostnames(d, limit=3)
    assert out["ok"] is True
    assert out["total_count"] == 15  # 全体件数は削らずに伝える
    assert out["truncated"] is True  # 相手が「無い」と断定しないための印
    assert [r["hostname"] for r in out["rows"]] == [
        "h0.example.com", "h1.example.com", "h2.example.com"]


def test_hostnames_truncated_is_false_when_blank_rows_dropped():
    """空ホスト名の行を落として件数が減っても、切り詰めたわけではない。

    件数の差だけで切り詰めを推定すると、相手側の計測ホスト照合が常に「未確認」になる。
    """
    d = _detail(collected={"hostnames": True},
                hostnames={"ok": True, "rows": [
                    {"hostname": "a.example.com", "sessions": 10},
                    {"hostname": "", "sessions": 1},
                ]})
    out = integration.hostnames(d, limit=10)
    assert out["total_count"] == 2
    assert len(out["rows"]) == 1
    assert out["truncated"] is False


def test_hostnames_skips_malformed_rows():
    d = _detail(collected={"hostnames": True},
                hostnames={"ok": True, "rows": [
                    {"hostname": "", "sessions": 5},
                    "not-a-dict",
                    {"hostname": "ok.example.com", "sessions": "12"},
                ]})
    out = integration.hostnames(d)
    assert out["rows"] == [{"hostname": "ok.example.com", "sessions": 12}]


# ============================================================
#  key_events_state
# ============================================================

def test_key_events_unknown_when_events_not_collected():
    d = _detail(collected={"events": False},
                summary={"key_event_count": 2, "key_event_names": ["purchase", "form_submit"]},
                events=[{"_error": "boom"}])
    st = integration.key_events_state(d)
    assert st == {"defined": 2, "measured_count": None, "zero": None}


def test_key_events_zero_when_defined_but_not_measured():
    d = _detail(collected={"events": True},
                summary={"key_event_count": 1, "key_event_names": ["purchase"]},
                events=[{"event_name": "page_view", "event_count": 900}])
    st = integration.key_events_state(d)
    assert st["defined"] == 1
    assert st["measured_count"] == 0
    assert st["zero"] is True


def test_key_events_measured_sums_matching_names():
    d = _detail(collected={"events": True},
                summary={"key_event_count": 2, "key_event_names": ["purchase", "form_submit"]},
                events=[{"event_name": "purchase", "event_count": 12},
                        {"event_name": "form_submit", "event_count": 30},
                        {"event_name": "page_view", "event_count": 900}])
    st = integration.key_events_state(d)
    assert st["measured_count"] == 42
    assert st["zero"] is False


def test_key_events_zero_definitions_is_unknown_not_zero():
    """定義0件は「計測されていない」とは別の問題なので zero に混ぜない。"""
    d = _detail(collected={"events": True}, summary={"key_event_count": 0, "key_event_names": []},
                events=[{"event_name": "page_view", "event_count": 900}])
    st = integration.key_events_state(d)
    assert st == {"defined": 0, "measured_count": None, "zero": None}


def test_key_events_at_report_limit_is_unknown():
    """イベント一覧が上限に達しているときは一覧の外に居る可能性を捨てられない。"""
    events = [{"event_name": f"e{i}", "event_count": 1}
              for i in range(integration.EVENTS_REPORT_LIMIT)]
    d = _detail(collected={"events": True},
                summary={"key_event_count": 1, "key_event_names": ["purchase"]},
                events=events)
    assert integration.key_events_state(d)["zero"] is None


# ============================================================
#  recent_changes
# ============================================================

def _ev(pid, severity="warn", kind="ga4", days_ago=0):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"kind": kind, "entity_id": str(pid), "severity": severity, "detected_at": at}


def test_recent_changes_filters_by_property_and_kind():
    events = [_ev("111"), _ev("222"), _ev("111", kind="gtm")]
    out = integration.recent_changes(events, "111")
    assert out["total"] == 1


def test_recent_changes_counts_by_severity_and_window():
    events = [_ev("111", "critical"), _ev("111", "warn"), _ev("111", "info", days_ago=99)]
    out = integration.recent_changes(events, "111", days=30)
    assert (out["critical"], out["warn"], out["info"], out["total"]) == (1, 1, 0, 2)
    assert out["latest_at"] is not None


def test_recent_changes_empty_log():
    out = integration.recent_changes(None, "111")
    assert out["total"] == 0 and out["latest_at"] is None


# ============================================================
#  property_payload — 後方互換と「未確認」の扱い
# ============================================================

V1_KEYS = ["property_id", "display_name", "health_grade", "health_score", "key_event_names",
           "quality", "alerts_count", "top_findings", "collected_at", "property_url"]


def test_v1_keys_unchanged():
    """既存キーは消さない・改名しない（古い改善コックピットがそのまま読めること）。"""
    d = _detail(summary={"display_name": "example-site", "health_grade": "B", "health_score": 78.0})
    out = integration.property_payload("123456789", d)
    for k in V1_KEYS:
        assert k in out, k
    assert out["property_id"] == "123456789"
    assert out["property_url"] == "/property/123456789"


def test_v2_keys_present():
    out = integration.property_payload("1", _detail())
    for k in ("monitoring", "hostnames", "key_events", "recent_changes"):
        assert k in out, k


def test_unknown_never_becomes_false():
    """何も収集できていない detail でも、未確認が False（問題なし）に化けない。"""
    out = integration.property_payload("1", _detail(summary={"key_event_count": 3}))
    assert out["key_events"]["zero"] is None
    assert out["key_events"]["measured_count"] is None
    assert out["hostnames"]["ok"] is False


def test_payload_survives_garbage_detail():
    """壊れた detail でも例外を出さない（相手の画面を落とさない）。"""
    out = integration.property_payload("1", {"summary": None, "events": "nope",
                                             "hostnames": None, "quality": "nope"})
    assert out["property_id"] == "1"
    assert out["top_findings"] == []


# ============================================================
#  連携先URL
# ============================================================

def test_normalize_url_accepts_http_and_strips_slash():
    assert integration.normalize_url("http://localhost:3000/") == "http://localhost:3000"
    assert integration.normalize_url("https://a.example.com/x/") == "https://a.example.com/x"


def test_normalize_url_rejects_non_http():
    for bad in ("", None, "   ", "ftp://x", "javascript:alert(1)", "example.com", "//example.com"):
        assert integration.normalize_url(bad) is None, bad


# ============================================================
#  連携キー（書き込みAPIの保護）
# ============================================================

def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(integration, "INTEGRATION_PATH", tmp_path / "integration.json")
    monkeypatch.delenv(integration.TOKEN_ENV, raising=False)
    monkeypatch.delenv(integration.COCKPIT_URL_ENV, raising=False)


def test_check_token_is_fail_closed_when_unissued(tmp_path, monkeypatch):
    """キーが未発行なら何を送られても通さない（書き込みは拒否が既定）。"""
    _isolate(tmp_path, monkeypatch)
    assert integration.token() is None
    assert integration.check_token("anything") is False
    assert integration.check_token("") is False
    assert integration.check_token(None) is False


def test_ensure_token_issues_once_and_is_stable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = integration.ensure_token()
    assert len(first) >= 20
    assert integration.ensure_token() == first


def test_check_token_matches_only_exact(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tok = integration.ensure_token()
    assert integration.check_token(tok) is True
    assert integration.check_token(tok + "x") is False
    assert integration.check_token(tok[:-1]) is False


def test_rotate_token_invalidates_old(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    old = integration.ensure_token()
    new = integration.rotate_token()
    assert new != old
    assert integration.check_token(old) is False
    assert integration.check_token(new) is True


def test_env_token_takes_priority(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    integration.ensure_token()
    monkeypatch.setenv(integration.TOKEN_ENV, "env-token-value-1234567890")
    assert integration.token() == "env-token-value-1234567890"
    assert integration.token_from_env() is True


def test_malformed_env_token_is_ignored(tmp_path, monkeypatch):
    """短すぎる・記号混じりの環境変数は使わない（空文字で素通りさせない）。"""
    _isolate(tmp_path, monkeypatch)
    saved = integration.ensure_token()
    monkeypatch.setenv(integration.TOKEN_ENV, "short")
    assert integration.token_from_env() is False
    assert integration.token() == saved
