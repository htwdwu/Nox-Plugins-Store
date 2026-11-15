#!/usr/bin/env python3
"""把上传暂存区的插件并入商店索引。

暂存区是独立仓库（默认 yyds0399/Muse-Plugins-Inbox），App 只对它有写权限。
本脚本定时扫描它的 incoming/ 目录，逐个审计并合并：

- 审计通过：包复制到本仓库 packages/，索引新增或覆盖该 id
- 审计不通过：写入 state/rejected.json 记录原因，不动索引
- 索引里已有同 id 同版本：跳过，保证幂等

这样凭据边界很清楚：App 只能往暂存区写，能否上架完全由本仓库的审计决定。
"""

import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_plugin  # noqa: E402

API = "https://api.github.com"
USER_AGENT = "muse-plugin-ingest"

DEFAULT_INBOX = "yyds0399/Muse-Plugins-Inbox"
DEFAULT_INDEX_REPO = "xzyy03/Muse-Plugins-Store"
PACKAGES_DIR = "packages"
STATE_PATH = "state/rejected.json"
MAX_PACKAGE_BYTES = 20 * 1024 * 1024


def gh(token, method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            text = resp.read().decode()
            return resp.status, (json.loads(text) if text.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def read_manifest(data: bytes) -> dict | None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    entry = next(
        (i for i in archive.infolist() if Path(i.filename).name == "plugin.json"),
        None,
    )
    if entry is None:
        return None
    try:
        return json.loads(archive.read(entry).decode("utf-8"))
    except Exception:
        return None


def localized(text) -> dict:
    if isinstance(text, dict):
        return text
    if isinstance(text, str):
        return {"default": text}
    return {}


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    inbox = os.environ.get("INBOX_REPO", DEFAULT_INBOX)
    index_repo = os.environ.get("INDEX_REPO", DEFAULT_INDEX_REPO)
    if not token:
        print("缺少 GITHUB_TOKEN")
        return 1

    # 1. 读索引
    status, index_file = gh(token, "GET", f"{API}/repos/{index_repo}/contents/index.json")
    if status != 200:
        print(f"读索引失败: {status} {index_file}")
        return 1
    index_sha = index_file["sha"]
    index = json.loads(base64.b64decode(index_file["content"]).decode("utf-8"))
    plugins = index.setdefault("plugins", [])
    known = {(p.get("id"), p.get("version")) for p in plugins}

    # 2. 列出暂存区
    status, listing = gh(token, "GET", f"{API}/repos/{inbox}/contents/incoming")
    if status == 404:
        print("暂存区没有 incoming 目录，跳过")
        return 0
    if status != 200:
        print(f"读暂存区失败: {status} {listing}")
        return 1
    candidates = [f for f in listing if f["name"].endswith(".zip")]
    print(f"暂存区待处理 {len(candidates)} 个包")

    rejected = {}
    changed = False
    packages: list[tuple[str, bytes]] = []

    for item in candidates:
        status, blob = gh(token, "GET", item["url"])
        if status != 200:
            print(f"  ! 下载失败 {item['name']}: {status}")
            continue
        data = base64.b64decode(blob["content"])
        if len(data) > MAX_PACKAGE_BYTES:
            rejected[item["name"]] = f"超过 {MAX_PACKAGE_BYTES // 1024 // 1024}MB"
            continue

        manifest = read_manifest(data)
        if manifest is None:
            rejected[item["name"]] = "包内没有合法的 plugin.json"
            continue

        plugin_id = manifest.get("id", "")
        version = manifest.get("version", "")
        if not plugin_id or not version:
            rejected[item["name"]] = "plugin.json 缺少 id 或 version"
            continue
        if (plugin_id, version) in known:
            print(f"  - 跳过已上架 {plugin_id} {version}")
            continue

        problems = audit_plugin.audit_archive(data)
        if problems:
            rejected[item["name"]] = "；".join(problems[:5])
            print(f"  ✗ {plugin_id} {version}: {problems[0]}")
            continue

        packages.append((f"{plugin_id}_{version}.zip", data))
        plugins.append(
            {
                "id": plugin_id,
                "name": localized(manifest.get("name")),
                "description": localized(manifest.get("description")),
                "author": manifest.get("publisher") or inbox.split("/")[0],
                "repository": manifest.get("repository") or f"https://github.com/{inbox}",
                "license": manifest.get("license") or "MIT",
                "version": version,
                "package_url": (
                    f"https://raw.githubusercontent.com/{index_repo}/main/"
                    f"{PACKAGES_DIR}/{plugin_id}_{version}.zip"
                ),
            }
        )
        known.add((plugin_id, version))
        changed = True
        print(f"  ✓ {plugin_id} {version} 通过")

    # 3. 写入包文件
    for name, data in packages:
        encoded = base64.b64encode(data).decode()
        status, existing = gh(
            token, "GET", f"{API}/repos/{index_repo}/contents/{PACKAGES_DIR}/{name}"
        )
        body = {"message": f"plugin: {name}", "content": encoded}
        if status == 200:
            body["sha"] = existing["sha"]
        status, resp = gh(
            token, "PUT", f"{API}/repos/{index_repo}/contents/{PACKAGES_DIR}/{name}", body
        )
        print(f"  包写入 {name}: {status}")

    # 4. 更新索引
    if changed:
        status, resp = gh(
            token,
            "PUT",
            f"{API}/repos/{index_repo}/contents/index.json",
            {
                "message": f"plugin: 并入 {len(packages)} 个新插件",
                "content": base64.b64encode(
                    (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode()
                ).decode(),
                "sha": index_sha,
            },
        )
        print(f"索引更新: {status}")

    # 5. 记录被拒项
    if rejected:
        existing_status, existing_file = gh(
            token, "GET", f"{API}/repos/{index_repo}/contents/{STATE_PATH}"
        )
        state = {}
        if existing_status == 200:
            state = json.loads(base64.b64decode(existing_file["content"]).decode())
        state.update(rejected)
        body = {
            "message": f"chore: 记录 {len(rejected)} 个未通过的插件包",
            "content": base64.b64encode(
                json.dumps(state, ensure_ascii=False, indent=2).encode()
            ).decode(),
        }
        if existing_status == 200:
            body["sha"] = existing_file["sha"]
        status, _ = gh(token, "PUT", f"{API}/repos/{index_repo}/contents/{STATE_PATH}", body)
        print(f"拒绝记录: {status}")

    print(f"\n处理完成：通过 {len(packages)}，拒绝 {len(rejected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
