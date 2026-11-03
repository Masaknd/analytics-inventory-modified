"""collectors/gtm.py のユニットテスト（公開バージョンからの GA4 Measurement ID 抽出）。

実行: リポジトリルートで `python -m pytest`
"""
from collectors import gtm


def _tag(ttype, params=None):
    return {"type": ttype,
            "parameter": [{"key": k, "value": v} for k, v in (params or {}).items()]}


def test_google_tag_tag_id_is_measurement_id():
    """現行の Google タグ（googtag）は ID を tagId に持つ。"""
    tags = [_tag("googtag", {"tagId": "G-V9J3JFVZRE"})]
    assert gtm.extract_ga4_measurement_ids(tags) == ["G-V9J3JFVZRE"]


def test_destination_google_tag_is_measurement_id():
    """新しいコンテナの Google タグは API 上 dest_ga / destinationId で返る（実データの形）。"""
    tags = [_tag("dest_ga", {"ignoreDupeGtagConfigCalls": "true",
                             "destinationId": "G-V9J3JFVZRE"})]
    assert gtm.extract_ga4_measurement_ids(tags) == ["G-V9J3JFVZRE"]


def test_google_tag_for_ads_is_not_ga4():
    """googtag は Google 広告(AW-)にも使われるので GA4 として数えない。"""
    tags = [_tag("googtag", {"tagId": "AW-123456789"})]
    assert gtm.extract_ga4_measurement_ids(tags) == []


def test_legacy_ga4_tags_still_detected():
    tags = [_tag("gaawc", {"measurementId": "G-AAAA1111"}),
            _tag("gaawe", {"measurementIdOverride": "G-BBBB2222"})]
    assert gtm.extract_ga4_measurement_ids(tags) == ["G-AAAA1111", "G-BBBB2222"]


def test_ids_are_deduplicated_in_order():
    tags = [_tag("googtag", {"tagId": "G-V9J3JFVZRE"}),
            _tag("gaawe", {"measurementIdOverride": "G-V9J3JFVZRE"})]
    assert gtm.extract_ga4_measurement_ids(tags) == ["G-V9J3JFVZRE"]


def test_variable_reference_kept_as_is():
    """変数参照はここでは解決しない（従来の gaawc/gaawe と同じ扱い）。"""
    tags = [_tag("googtag", {"tagId": "{{GA4 ID}}"})]
    assert gtm.extract_ga4_measurement_ids(tags) == ["{{GA4 ID}}"]
