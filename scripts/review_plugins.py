#!/usr/bin/env python3
"""插件审核动作执行器。

由 review-action.yml 按 App 传来的 PLUGIN_IDS 与 ACTION 修改索引仓库：

- approve：在当前版本写入 state/reviewed.json，表示该插件已人工确认；
- reject：从索引移除、把原因记进 state/rejected.json，并删除对应的包与图标。

ingest_inbox.py 自动上架时**不**写 reviewed.json，所以「已在索引里但当前版本
没被确认过」的插件就是待审核项；App 端据此算差集。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
INDEX_PATH = "index.json"
REVIEWED_PATH = "state/reviewed.json"
REJECTED_PATH = "state/rejected.json"
PACKAGES_DIR = "packages"
ICONS_DIR = "icons"


def gh(token, method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nox-plugin-review",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except Exception as error:  # noqa: BLE001
        return None, str(error)


def read_json(token, repo, path, fallback):
    status, data = gh(token, "GET", f"{API}/repos/{repo}/contents/{path}")
    if status != 200:
        return fallback, None
    text = base64.b64decode(data["content"]).decode()
    return json.loads(text), data["sha"]


def write_json(token, repo, path, payload, sha, message):
    body = {
        "message": message,
        "content": base64.b64encode(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        ).decode(),
    }
    if sha:
        body["sha"] = sha
    status, response = gh(token, "PUT", f"{API}/repos/{repo}/contents/{path}", body)
    print(f"  写入 {path}: {status}")
    return status in (200, 201)


def delete_file(token, repo, path, message):
    status, data = gh(token, "GET", f"{API}/repos/{repo}/contents/{path}")
    if status != 200:
        return False
    status, _ = gh(
        token, "DELETE", f"{API}/repos/{repo}/contents/{path}",
        {"message": message, "sha": data["sha"]},
    )
    print(f"  删除 {path}: {status}")
    return status == 200


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("INDEX_REPO") or os.environ.get("GITHUB_REPOSITORY")
    action = (os.environ.get("ACTION") or "").strip().lower()
    ids = [x.strip() for x in (os.environ.get("PLUGIN_IDS") or "").split(",") if x.strip()]
    reason = (os.environ.get("REASON") or "").strip()

    if not token or not repo:
        print("缺少 GITHUB_TOKEN / INDEX_REPO")
        return 1
    if action not in ("approve", "reject"):
        print(f"未知动作：{action}")
        return 1
    if not ids:
        print("没有指定插件")
        return 1

    index, index_sha = read_json(token, repo, INDEX_PATH, {"schema_version": 1, "plugins": []})
    if index_sha is None:
        print("读不到 index.json")
        return 1
    reviewed, reviewed_sha = read_json(token, repo, REVIEWED_PATH, {})
    rejected, rejected_sha = read_json(token, repo, REJECTED_PATH, {})

    plugins = index.get("plugins", [])
    by_id = {p.get("id"): p for p in plugins}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    approved: list[str] = []
    removed: list[tuple[str, str]] = []
    missing: list[str] = []

    for plugin_id in ids:
        entry = by_id.get(plugin_id)
        if entry is None:
            missing.append(plugin_id)
            continue
        version = entry.get("version") or ""
        if action == "approve":
            reviewed[plugin_id] = {"version": version, "at": now}
            approved.append(f"{plugin_id} {version}")
        else:
            plugins.remove(entry)
            by_id.pop(plugin_id, None)
            reviewed.pop(plugin_id, None)
            removed.append((plugin_id, version))
            rejected[f"{plugin_id}_{version}.zip"] = reason or "人工拒绝"

    index["plugins"] = plugins

    if action == "approve":
        ok = write_json(token, repo, REVIEWED_PATH, reviewed, reviewed_sha, f"review: 通过 {len(approved)} 个插件")
    else:
        for plugin_id, version in removed:
            delete_file(token, repo, f"{PACKAGES_DIR}/{plugin_id}_{version}.zip",
                        f"review: 下架 {plugin_id} {version}")
            for suffix in (".png", ".webp"):
                delete_file(token, repo, f"{ICONS_DIR}/{plugin_id}{suffix}",
                            f"review: 删除 {plugin_id} 图标")
        ok = write_json(token, repo, INDEX_PATH, index, index_sha, f"review: 拒绝并下架 {len(removed)} 个插件")
        if ok:
            write_json(token, repo, REJECTED_PATH, rejected, rejected_sha, f"review: 记录 {len(removed)} 个拒绝原因")

    print(f"\n通过 {len(approved)}，拒绝 {len(removed)}，未找到 {len(missing)}")
    for line in approved:
        print(f"  ✓ {line}")
    for plugin_id, version in removed:
        print(f"  ✗ {plugin_id} {version}")
    for plugin_id in missing:
        print(f"  ? {plugin_id}（索引里没有）")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
