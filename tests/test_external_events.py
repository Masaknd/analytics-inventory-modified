"""external_events.py のユニットテスト。

一番大事なのは次の2つ。

- test_rejects_* — 外から来るデータを素通しせず、必須項目・形式・スキームを検査すること。
  この台帳は書き込みAPIで入ってくるので、画面に出す前にここで止める。
- test_timeline_* — 変化ログと施策が同じ時間軸に並ぶこと。これが①の目的
  （KPIが動いたのは施策か、計測設定が変わったせいかを切り分ける）そのもの。

実行: リポジトリルートで `python -m pytest`
"""
import external_events as ee


def _raw(**over):
    base = {"external_id": "init_abc123", "occurred_on": "2026-09-15",
            "title": "LPのCTA文言を変更", "kind": "initiative",
            "property_id": "308259408", "status": "live",
            "url": "http://127.0.0.1:3000/initiatives/abc"}
    base.update(over)
    return base


# ============================================================
#  normalize — 受け取ってよいものだけ通す
# ============================================================

def test_normalize_accepts_well_formed_event():
    out = ee.normalize(_raw())
    assert out is not None
    assert out["external_id"] == "init_abc123"
    assert out["occurred_on"] == "2026-09-15"
    assert out["property_id"] == "308259408"
    assert out["source"] == "cockpit"
    assert out["received_at"]


def test_rejects_missing_required_fields():
    for over in ({"external_id": ""}, {"occurred_on": ""}, {"title": ""},
                 {"occurred_on": "2026/09/15"}, {"occurred_on": "2026-13-45"}):
        assert ee.normalize(_raw(**over)) is None, over


def test_rejects_non_dict():
    assert ee.normalize("nope") is None
    assert ee.normalize(None) is None
    assert ee.normalize(["x"]) is None


def test_rejects_bad_external_id():
    assert ee.normalize(_raw(external_id="a b")) is None
    assert ee.normalize(_raw(external_id="x" * 200)) is None


def test_drops_non_http_url():
    """相手の画面へ戻るリンクに使うので javascript: 等は落とす。"""
    assert ee.normalize(_raw(url="javascript:alert(1)"))["url"] is None
    assert ee.normalize(_raw(url="file:///C:/"))["url"] is None
    assert ee.normalize(_raw(url="https://ok.example.com/x"))["url"] == "https://ok.example.com/x"


def test_strips_control_characters_from_title():
    out = ee.normalize(_raw(title="改行\nと\rタブ\tを含む"))
    assert "\n" not in out["title"] and "\r" not in out["title"]


def test_unknown_kind_falls_back_to_other():
    assert ee.normalize(_raw(kind="なにか"))["kind"] == "other"
    assert ee.normalize(_raw(kind="diagnosis"))["kind"] == "diagnosis"


def test_non_numeric_property_id_is_dropped():
    """プロパティIDが数字でなければ紐づけない（リンク先を壊さない）。"""
    assert ee.normalize(_raw(property_id="../etc"))["property_id"] is None


def test_accepts_id_key_as_alias():
    assert ee.normalize(_raw(external_id=None, id="init_x"))["external_id"] == "init_x"


# ============================================================
#  merge — 同じ施策を何度送られても増えない
# ============================================================

def _norm(**over):
    return ee.normalize(_raw(**over))


def test_merge_upserts_by_source_and_id():
    first = _norm(title="旧タイトル")
    second = _norm(title="新タイトル")
    out = ee.merge([first], [second])
    assert len(out) == 1
    assert out[0]["title"] == "新タイトル"


def test_merge_sorts_newest_first_and_caps():
    items = [_norm(external_id=f"init_{i}", occurred_on="2026-09-%02d" % (i + 1))
             for i in range(5)]
    out = ee.merge([], items)
    assert [e["occurred_on"] for e in out] == [
        "2026-09-05", "2026-09-04", "2026-09-03", "2026-09-02", "2026-09-01"]


def test_merge_respects_max_events():
    many = [_norm(external_id=f"init_{i}") for i in range(ee.MAX_EVENTS + 20)]
    assert len(ee.merge([], many)) == ee.MAX_EVENTS


# ============================================================
#  timeline — ①の目的そのもの
# ============================================================

def _change(at, severity="warn", pid="308259408"):
    return {"kind": "ga4", "entity_id": pid, "severity": severity, "detected_at": at,
            "message": "セッション急減"}


def test_timeline_interleaves_changes_and_initiatives_newest_first():
    changes = [_change("2026-09-20T01:00:00+00:00"), _change("2026-09-10T01:00:00+00:00")]
    ext = [_norm(occurred_on="2026-09-15")]
    rows = ee.timeline(changes, ext)
    assert [r["_date"] for r in rows] == ["2026-09-20", "2026-09-15", "2026-09-10"]
    assert [r["_row"] for r in rows] == ["change", "external", "change"]


def test_timeline_without_external_is_just_changes():
    """連携していなければ従来どおり＝変化ログだけが並ぶ。"""
    changes = [_change("2026-09-20T01:00:00+00:00")]
    rows = ee.timeline(changes, [])
    assert len(rows) == 1 and rows[0]["_row"] == "change"
    assert ee.timeline(None, None) == []


def test_timeline_skips_malformed_entries():
    rows = ee.timeline(["nope", _change("2026-09-20T01:00:00+00:00")], [None, _norm()])
    assert len(rows) == 2


# ============================================================
#  record — 1件の不備で全体を落とさない
# ============================================================

def test_record_rejects_non_list():
    out = ee.record("nope")
    assert out["accepted"] == 0 and out["error"]


def test_record_counts_accepted_and_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "EXTERNAL_EVENTS_PATH", tmp_path / "ext.json")
    out = ee.record([_raw(), _raw(external_id="init_2"), {"bad": 1}])
    assert out["accepted"] == 2
    assert out["rejected"] == 1
    assert out["total"] == 2


def test_record_is_idempotent(tmp_path, monkeypatch):
    """相手が何度送っても増えない。"""
    monkeypatch.setattr(ee, "EXTERNAL_EVENTS_PATH", tmp_path / "ext.json")
    ee.record([_raw()])
    ee.record([_raw()])
    assert len(ee.load()) == 1


def test_for_property_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "EXTERNAL_EVENTS_PATH", tmp_path / "ext.json")
    ee.record([_raw(), _raw(external_id="init_2", property_id="111")])
    assert len(ee.for_property("308259408")) == 1
    assert len(ee.for_property("999")) == 0


def test_delete_source(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "EXTERNAL_EVENTS_PATH", tmp_path / "ext.json")
    ee.record([_raw(), _raw(external_id="init_2")])
    assert ee.delete_source("cockpit") == 2
    assert ee.load() == []


# ============================================================
#  墓標（相手で取り消された施策を残さない）
# ============================================================

def test_normalize_deleted_needs_only_id():
    out = ee.normalize({"external_id": "init_abc", "deleted": True})
    assert out == {"source": "cockpit", "external_id": "init_abc", "_deleted": True}


def test_merge_removes_on_tombstone():
    kept = _norm(external_id="init_keep")
    gone = _norm(external_id="init_gone")
    tomb = ee.normalize({"external_id": "init_gone", "deleted": True})
    out = ee.merge([kept, gone], [tomb])
    assert [e["external_id"] for e in out] == ["init_keep"]


def test_tombstone_is_not_stored():
    """墓標そのものは台帳に残さない（画面に空の行が出ないように）。"""
    tomb = ee.normalize({"external_id": "init_x", "deleted": True})
    assert ee.merge([], [tomb]) == []


def test_tombstone_for_unknown_id_is_harmless():
    kept = _norm(external_id="init_keep")
    tomb = ee.normalize({"external_id": "init_never_seen", "deleted": True})
    assert len(ee.merge([kept], [tomb])) == 1


def test_record_counts_deleted_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "EXTERNAL_EVENTS_PATH", tmp_path / "ext.json")
    ee.record([_raw(), _raw(external_id="init_2")])
    out = ee.record([_raw(), {"external_id": "init_2", "deleted": True}])
    assert out["accepted"] == 1
    assert out["deleted"] == 1
    assert out["total"] == 1
