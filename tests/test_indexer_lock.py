"""収集中かどうかの判定（ロックの心拍）のテスト。

守りたいのは1点だけ。**動いている収集を「終わった」と判定しない**こと。

以前は「ロックが開始から90分より古ければ無効」としていたため、アカウントが増えて
収集が90分を超えると、走っている最中に完了扱いになった。その状態で再収集を押すと
indexer が二重に起動し、inventory.json と details を同時に書いて壊す。

実行: リポジトリルートで `python -m pytest`
"""
import time

import indexer


def test_touch_lock_writes_timestamp(tmp_path, monkeypatch):
    lock = tmp_path / "indexer.lock"
    monkeypatch.setattr(indexer, "INDEXER_LOCK_PATH", lock)
    monkeypatch.setattr(indexer, "_last_heartbeat", 0.0)
    indexer._touch_lock()
    assert lock.exists()
    assert lock.read_text(encoding="utf-8").strip()


def test_touch_lock_is_throttled(tmp_path, monkeypatch):
    """ログのたびに呼ばれるので、毎回書き込まない（無駄なI/Oを増やさない）。"""
    lock = tmp_path / "indexer.lock"
    monkeypatch.setattr(indexer, "INDEXER_LOCK_PATH", lock)
    monkeypatch.setattr(indexer, "_last_heartbeat", 0.0)
    indexer._touch_lock()
    first = lock.read_text(encoding="utf-8")
    time.sleep(0.01)
    indexer._touch_lock()  # 間隔内なので書き換わらない
    assert lock.read_text(encoding="utf-8") == first


def test_touch_lock_writes_again_after_interval(tmp_path, monkeypatch):
    lock = tmp_path / "indexer.lock"
    monkeypatch.setattr(indexer, "INDEXER_LOCK_PATH", lock)
    monkeypatch.setattr(indexer, "_last_heartbeat", 0.0)
    indexer._touch_lock()
    first = lock.read_text(encoding="utf-8")
    # 間隔を過ぎた状態を作る
    monkeypatch.setattr(indexer, "_last_heartbeat",
                        time.time() - indexer._HEARTBEAT_INTERVAL_SEC - 1)
    time.sleep(0.01)
    indexer._touch_lock()
    assert lock.read_text(encoding="utf-8") != first


def test_touch_lock_survives_unwritable_path(tmp_path, monkeypatch):
    """ロックが書けなくても収集そのものは止めない。"""
    monkeypatch.setattr(indexer, "INDEXER_LOCK_PATH", tmp_path / "no-such-dir" / "x.lock")
    monkeypatch.setattr(indexer, "_last_heartbeat", 0.0)
    indexer._touch_lock()  # 例外が出ないこと


def test_log_emits_heartbeat(tmp_path, monkeypatch, capsys):
    """進捗ログが心拍を兼ねる（収集が動いている限り必ず更新される）。"""
    lock = tmp_path / "indexer.lock"
    monkeypatch.setattr(indexer, "INDEXER_LOCK_PATH", lock)
    monkeypatch.setattr(indexer, "_last_heartbeat", 0.0)
    indexer._log("property 123 'テスト'")
    capsys.readouterr()
    assert lock.exists()


def test_running_judged_by_heartbeat_not_start_time(tmp_path, monkeypatch):
    """開始から何時間経っていても、心拍が新しければ『実行中』。"""
    import os

    import server
    from config import INDEXER_LOCK_PATH as real_path

    lock = tmp_path / "indexer.lock"
    lock.write_text("x", encoding="utf-8")
    monkeypatch.setattr("config.INDEXER_LOCK_PATH", lock)

    # 3時間前に始まったが、いま心拍があった状況
    assert server._indexer_running() is True

    # 心拍が timeout を超えて途絶えた状況（異常終了）
    stale = time.time() - server.INDEXER_HEARTBEAT_TIMEOUT_SEC - 60
    os.utime(lock, (stale, stale))
    assert server._indexer_running() is False

    # ロックが無ければ実行中ではない
    lock.unlink()
    assert server._indexer_running() is False
    assert real_path  # import されていることの確認（未使用変数対策）
