"""GTM API v2 collectors — accounts, containers, workspaces, tags."""
from __future__ import annotations

import random
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


_MAX_RETRIES = 6


def gtm_client(creds):
    return build("tagmanager", "v2", credentials=creds, cache_discovery=False)


def _execute_with_retry(request):
    """Exponential backoff for 429/500/503 — handles GTM API rate limits."""
    for attempt in range(_MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status in (429, 500, 502, 503) and attempt < _MAX_RETRIES - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)
                continue
            raise


def list_accounts(creds) -> list[dict]:
    svc = gtm_client(creds)
    resp = _execute_with_retry(svc.accounts().list())
    return resp.get("account", [])


def list_containers(creds, account_path: str) -> list[dict]:
    svc = gtm_client(creds)
    resp = _execute_with_retry(svc.accounts().containers().list(parent=account_path))
    return resp.get("container", [])


def get_live_version(creds, container_path: str) -> dict | None:
    svc = gtm_client(creds)
    try:
        return _execute_with_retry(
            svc.accounts().containers().versions().live(parent=container_path)
        )
    except Exception:
        return None


def summarize_container(creds, container: dict) -> tuple[dict, dict | None]:
    """Returns (summary, live_version_data) — caller persists live version to disk."""
    out = {
        "account_id": container.get("accountId"),
        "container_id": container.get("containerId"),
        "name": container.get("name"),
        "public_id": container.get("publicId"),
        "usage_context": container.get("usageContext"),
        "domain_name": container.get("domainName"),
        "path": container.get("path"),
        "tag_count": 0,
        "trigger_count": 0,
        "variable_count": 0,
        "ga4_measurement_ids": [],
        "version_name": None,
        "version_id": None,
    }
    live = get_live_version(creds, container["path"])
    if live:
        tags = live.get("tag", []) or []
        out["tag_count"] = len(tags)
        out["trigger_count"] = len(live.get("trigger", []) or [])
        out["variable_count"] = len(live.get("variable", []) or [])
        out["version_name"] = live.get("name")
        out["version_id"] = live.get("containerVersionId")
        out["ga4_measurement_ids"] = extract_ga4_measurement_ids(tags)
    return out, live


# GTM の「Google タグ」は API 上で 2 つの形がある（新しいコンテナは dest_ga で返る）
_GOOGLE_TAG_ID_PARAMS = {("googtag", "tagId"), ("dest_ga", "destinationId")}


def _is_ga4_id_param(ttype: str | None, key: str | None, value: str) -> bool:
    if ttype in ("gaawe", "gaawc"):
        return key in ("measurementId", "measurementIdOverride")
    if (ttype, key) in _GOOGLE_TAG_ID_PARAMS:
        # Google タグは Google 広告(AW-)等にも使われるので、GA4(G-)か未解決の変数参照だけを採る
        return value.startswith(("G-", "{{"))
    return False


def extract_ga4_measurement_ids(tags: list) -> list[str]:
    """公開バージョンのタグから GA4 Measurement ID を出現順・重複なしで集める。

    現行の Google タグは googtag なら tagId、dest_ga なら destinationId に ID を持ち、
    旧世代の GA4 設定/イベントタグ（gaawc/gaawe）は measurementId(Override) に持つ。
    """
    ids: list[str] = []
    for t in tags or []:
        for p in t.get("parameter", []) or []:
            v = p.get("value") or ""
            if v and v not in ids and _is_ga4_id_param(t.get("type"), p.get("key"), v):
                ids.append(v)
    return ids


def list_tags(creds, container: dict) -> list[dict]:
    live = get_live_version(creds, container["path"])
    if not live:
        return []
    out = []
    for t in live.get("tag", []) or []:
        out.append({
            "tag_id": t.get("tagId"),
            "name": t.get("name"),
            "type": t.get("type"),
            "paused": t.get("paused", False),
            "firing_trigger_id": t.get("firingTriggerId", []),
        })
    return out
