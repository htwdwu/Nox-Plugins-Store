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
import urllib.parse
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
    # 插件 id 允许中文，写回仓库时路径里会出现非 ASCII 字符；
    # urllib 只接受 ASCII 的 URL，这里统一做一次百分号编码（已编码部分不受影响）。
    url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
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


def _semver_key(version: str):
    """把 SemVer 拆成可比较的元组；预发布版本排在正式版之前。"""
    main, _, pre = (version or "").strip().partition("-")
    parts = []
    for chunk in main.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    # 无预发布标记的正式版更大；有预发布时按字符串比较，够用。
    return (tuple(parts), 0 if pre else 1, pre)


def semver_gt(candidate: str, current: str) -> bool:
    return _semver_key(candidate) > _semver_key(current)


def ascii_author(publisher, plugin_id, fallback: str) -> str:
    """索引的 author 尽量给出 ASCII 形式。

    旧版客户端对 author 用 `^[a-z][a-z0-9-]*$` 校验，一个中文作者会让整份索引被拒，
    连带其他插件都看不到。清单里的中文作者名仍然保留在插件自己的 name/publisher 里。
    """
    candidates = [publisher, (plugin_id or "").split(".")[0], fallback]
    for value in candidates:
        text = (value or "").strip().lower()
        if not text:
            continue
        slug = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "-") else "-" for ch in text)
        slug = slug.strip("-")
        if slug and slug[0].isalpha():
            return slug
    return "plugin"


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
    # 同一个 id 只保留一个条目：新版本上架时替换旧的，版本号更低的包直接跳过。
    # 若按 (id, version) 判重，暂存区里遗留的旧包会在下次扫描时被重新加回索引。
    latest_by_id = {p.get("id"): (p.get("version") or "") for p in plugins}

    def newer_than_indexed(plugin_id: str, version: str) -> bool:
        current = latest_by_id.get(plugin_id)
        if current is None:
            return True
        return semver_gt(version, current)


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
    stale_packages: list[str] = []

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
        if not newer_than_indexed(plugin_id, version):
            print(f"  - 跳过 {plugin_id} {version}（索引里已有 {latest_by_id.get(plugin_id)}）")
            continue

        problems = audit_plugin.audit_archive(data)
        if problems:
            rejected[item["name"]] = "；".join(problems[:5])
            print(f"  ✗ {plugin_id} {version}: {problems[0]}")
            continue

        # 同 id 的旧条目连同旧包一起下架，避免商店里出现两个同名插件。
        for old in [p for p in plugins if p.get("id") == plugin_id]:
            old_pkg = f"{plugin_id}_{old.get('version') or ''}.zip"
            plugins.remove(old)
            stale_packages.append(old_pkg)
        packages.append((f"{plugin_id}_{version}.zip", data))
        plugins.append(
            {
                "id": plugin_id,
                "name": localized(manifest.get("name")),
                "description": localized(manifest.get("description")),
                "author": ascii_author(
                    manifest.get("publisher"), manifest.get("id"), inbox.split("/")[0]
                ),
                "repository": manifest.get("repository") or f"https://github.com/{inbox}",
                "license": manifest.get("license") or "MIT",
                "version": version,
                "package_url": (
                    f"https://raw.githubusercontent.com/{index_repo}/main/"
                    f"{PACKAGES_DIR}/{plugin_id}_{version}.zip"
                ),
            }
        )
        latest_by_id[plugin_id] = version
        changed = True
        print(f"  ✓ {plugin_id} {version} 通过")

    # 3. 删除被替换的同 id 旧包
    for name in dict.fromkeys(stale_packages):
        if name in {n for n, _ in packages}:
            continue
        status, existing = gh(
            token, "GET", f"{API}/repos/{index_repo}/contents/{PACKAGES_DIR}/{name}"
        )
        if status == 200:
            status, _ = gh(
                token,
                "DELETE",
                f"{API}/repos/{index_repo}/contents/{PACKAGES_DIR}/{name}",
                {"message": f"plugin: 移除被替换的 {name}", "sha": existing["sha"]},
            )
            print(f"  旧包删除 {name}: {status}")

    # 4. 写入包文件
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

    # 5. 更新索引
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

    # 6. 记录被拒项
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
