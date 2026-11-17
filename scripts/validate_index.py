#!/usr/bin/env python3
"""校验 index.json 的格式、仓库、Release 资产与包内清单。"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = ROOT / "index.json"
DEFAULT_SCHEMA = ROOT / "schema" / "index.schema.json"
API = "https://api.github.com"
USER_AGENT = "aicode-plugin-index-validator"
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
DEFAULT_KEY = "default"

SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)
REPOSITORY = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")

errors = []
warnings = []


def escape_annotation(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def error(message):
    errors.append(message)
    print(f"::error file=index.json::{escape_annotation(message)}")


def warning(message):
    warnings.append(message)
    print(f"::warning file=index.json::{escape_annotation(message)}")


def normalize_tag(tag):
    return tag.strip().lower().replace("_", "-")


def parse_localized(value):
    if isinstance(value, str):
        return {DEFAULT_KEY: value.strip()}
    if isinstance(value, dict):
        result = {}
        for tag, text in value.items():
            if not isinstance(text, str):
                return None
            result[normalize_tag(tag)] = text.strip()
        return result or None
    return None


def resolve_localized(values, tag):
    tag = normalize_tag(tag)
    language = tag.split("-")[0]
    for key in (tag, language, DEFAULT_KEY, "en"):
        if key in values:
            return values[key]
    return next(iter(values.values()))


def semver_key(version):
    match = SEMVER.match(version)
    if not match:
        return (0, 0, 0, 0, ())
    major, minor, patch, prerelease, _ = match.groups()
    if prerelease is None:
        return (int(major), int(minor), int(patch), 1, ())
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    )
    return (int(major), int(minor), int(patch), 0, identifiers)


def load_index(path):
    if not path.is_file():
        error(f"找不到 {path}")
        return None
    if path.stat().st_size > MAX_INDEX_BYTES:
        error(f"{path.name} 超过 2 MiB")
        return None
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        error(f"{path.name} 不是合法 JSON: {exc}")
        return None
    if not isinstance(index, dict):
        error(f"{path.name} 顶层必须是对象")
        return None
    plugins = index.get("plugins")
    if not isinstance(plugins, list):
        error(f"{path.name} 缺少 plugins 数组")
        return None
    return index


def validate_schema(index, schema_path):
    try:
        import jsonschema
    except ImportError:
        error("缺少 jsonschema 依赖，请运行 python3 -m pip install jsonschema")
        return
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"读取 {schema_path.name} 失败: {exc}")
        return
    validator = jsonschema.Draft202012Validator(schema)
    for issue in sorted(validator.iter_errors(index), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(part) for part in issue.absolute_path) or "(root)"
        error(f"schema: {path}: {issue.message}")


def api_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_get(path):
    request = urllib.request.Request(f"{API}{path}", headers=api_headers())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise RuntimeError("GitHub API 触发限流，请配置 GITHUB_TOKEN") from exc
        raise RuntimeError(f"GitHub API 返回 HTTP {exc.code}: {path}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API 请求失败: {path}: {exc.reason}") from exc


def parse_repository(url):
    if not isinstance(url, str):
        return None
    match = REPOSITORY.match(url)
    if not match:
        return None
    return match.group(1), match.group(2).removesuffix(".git")


def asset_name(plugin_id, version):
    return f"{plugin_id}_{version}.zip"


def asset_version(plugin_id, name):
    prefix = f"{plugin_id}_"
    if not name.startswith(prefix) or not name.endswith(".zip"):
        return None
    version = name[len(prefix) : -len(".zip")]
    if version.startswith("v") or not SEMVER.match(version):
        return None
    return version


def find_asset(releases, plugin):
    plugin_id = plugin.get("id")
    version = plugin.get("version")
    if version:
        expected = asset_name(plugin_id, version)
        for release in releases:
            if release.get("draft"):
                continue
            for asset in release.get("assets", []):
                if asset.get("name") == expected:
                    return asset, version
        return None, None
    candidates = []
    for release in releases:
        if release.get("draft"):
            continue
        for asset in release.get("assets", []):
            candidate = asset_version(plugin_id, asset.get("name", ""))
            if candidate:
                candidates.append((candidate, asset))
    if not candidates:
        return None, None
    version, asset = max(candidates, key=lambda item: semver_key(item[0]))
    return asset, version


def download_asset(asset, plugin_id):
    size = asset.get("size")
    if not isinstance(size, int) or size <= 0 or size > MAX_ARCHIVE_BYTES:
        error(f"{plugin_id}: Release 资产大小非法: {size}")
        return None
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        error(f"{plugin_id}: Release 资产缺少 https 下载地址")
        return None
    request = urllib.request.Request(ascii_url(url), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.URLError as exc:
        error(f"{plugin_id}: 下载 Release 资产失败: {exc}")
        return None
    if len(data) > MAX_ARCHIVE_BYTES:
        error(f"{plugin_id}: Release 资产超过 16 MiB")
        return None
    return data


def read_manifest(data, plugin_id):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                info = archive.getinfo("plugin.json")
            except KeyError:
                error(f"{plugin_id}: ZIP 根目录缺少 plugin.json")
                return None
            if info.file_size > MAX_MANIFEST_BYTES:
                error(f"{plugin_id}: plugin.json 超过 1 MiB")
                return None
            return json.loads(archive.read(info).decode("utf-8"))
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        error(f"{plugin_id}: 读取 plugin.json 失败: {exc}")
        return None


def check_localized(plugin_id, field, index_value, manifest_value):
    index_text = parse_localized(index_value)
    manifest_text = parse_localized(manifest_value)
    if index_text is None:
        error(f"{plugin_id}: 索引 {field} 不是字符串或语言映射")
        return
    if manifest_text is None:
        error(f"{plugin_id}: 包内 {field} 不是字符串或语言映射")
        return
    for tag, text in index_text.items():
        actual = resolve_localized(manifest_text, tag)
        if actual != text:
            error(
                f"{plugin_id}: {field} 的 {tag} 文本与包内不一致: "
                f"索引={text!r} 包内={actual!r}"
            )
    missing = sorted(set(manifest_text) - set(index_text))
    if missing:
        warning(f"{plugin_id}: 索引 {field} 未覆盖包内语言: {', '.join(missing)}")


def check_manifest(plugin, manifest, version):
    plugin_id = plugin["id"]
    if not isinstance(manifest, dict):
        error(f"{plugin_id}: 包内 plugin.json 顶层必须是对象")
        return
    # repository 不比对：商店把插件重新托管到商店自己的仓库时，索引里的地址
    # 与原作者写在包内清单里的地址本来就会不同。
    checks = (
        ("id", manifest.get("id"), plugin.get("id")),
        ("version", manifest.get("version"), version),
        ("publisher", manifest.get("publisher"), plugin.get("author")),
    )
    for field, actual, expected in checks:
        if actual != expected:
            error(
                f"{plugin_id}: 包内 {field}={actual!r} 与索引 {expected!r} 不一致"
            )
    check_localized(plugin_id, "name", plugin.get("name"), manifest.get("name"))
    check_localized(
        plugin_id, "description", plugin.get("description"), manifest.get("description")
    )


def ascii_url(url):
    """插件 id 允许中文，包地址里会出现非 ASCII 字符；urllib 只接受 ASCII URL。"""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def fetch_with_retry(url, timeout=60, attempts=4):
    """下载包内容，404 时退避重试。

    包刚由 ingest 提交时 raw CDN 还没刷新，紧跟着跑的校验会拿到 404 —— 这是
    传播延迟，不是索引坏了。给几次退避重试，避免把时序问题判成校验失败。
    """
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(ascii_url(url), headers={"User-Agent": USER_AGENT}),
                timeout=timeout,
            ) as resp:
                return resp.read(MAX_ARCHIVE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            last = exc
            # 只有暂时性状态码值得重试；404 在 CDN 刚写入时也会短暂出现。
            if exc.code not in (404, 408, 429, 500, 502, 503, 504):
                raise
        except Exception as exc:
            last = exc
        if attempt < attempts - 1:
            time.sleep(3 * (attempt + 1))
    raise last if last is not None else RuntimeError("下载失败")


def validate_package_url(plugin, url):
    """直链托管在索引仓库自身时，只校验可下载性与包内清单 id。"""
    expected_id = plugin.get("id")
    try:
        data = fetch_with_retry(url)
    except Exception as exc:
        error(f"{expected_id}: package_url 无法下载（{exc}）")
        return
    if len(data) > MAX_ARCHIVE_BYTES:
        error(f"{expected_id}: package_url 指向的包超过上限")
        return
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        error(f"{expected_id}: package_url 不是合法 zip")
        return
    entry = next(
        (i for i in archive.infolist() if i.filename.split("/")[-1] == "plugin.json"),
        None,
    )
    if entry is None:
        error(f"{expected_id}: package_url 的包内缺少 plugin.json")
        return
    try:
        manifest = json.loads(archive.read(entry).decode("utf-8"))
    except Exception:
        error(f"{expected_id}: package_url 的 plugin.json 无法解析")
        return
    if manifest.get("id") != expected_id:
        error(
            f"{expected_id}: 包内 id 不一致（{manifest.get('id')}）"
        )


def check_plugin(plugin):
    plugin_id = plugin.get("id")
    if not isinstance(plugin_id, str) or not plugin_id:
        error("插件条目缺少合法 id")
        return
    version = plugin.get("version")
    if version is not None and not isinstance(version, str):
        error(f"{plugin_id}: version 必须是字符串")
        return
    # 直链托管的包不依赖作者仓库的 Release，单独校验后可提前返回。
    package_url = plugin.get("package_url")
    if isinstance(package_url, str) and package_url:
        validate_package_url(plugin, package_url)
        return

    repository = parse_repository(plugin.get("repository"))
    if repository is None:
        error(f"{plugin_id}: repository 不是 GitHub 仓库地址")
        return
    owner, repo = repository
    try:
        if api_get(f"/repos/{owner}/{repo}") is None:
            error(f"{plugin_id}: {owner}/{repo} 仓库不存在或不可访问")
            return
        if version:
            releases = api_get(f"/repos/{owner}/{repo}/releases?per_page=100")
            releases = releases if isinstance(releases, list) else []
        else:
            latest = api_get(f"/repos/{owner}/{repo}/releases/latest")
            releases = [latest] if isinstance(latest, dict) else []
        asset, resolved_version = find_asset(releases, plugin)
        if asset is None:
            expected = (
                asset_name(plugin_id, version)
                if version
                else f"{plugin_id}_*.zip"
            )
            error(f"{plugin_id}: 未找到 Release 资产 {expected}")
            return
        data = download_asset(asset, plugin_id)
        if data is None:
            return
        manifest = read_manifest(data, plugin_id)
        if manifest is None:
            return
        before = len(errors)
        check_manifest(plugin, manifest, resolved_version)
        if len(errors) == before:
            print(f"OK: {plugin_id} {resolved_version}")
    except RuntimeError as exc:
        error(f"{plugin_id}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="校验 AiCode 插件索引")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()

    index = load_index(args.index)
    if index is None:
        print(f"\n发现 {len(errors)} 个错误")
        return 1
    validate_schema(index, args.schema)
    seen_ids = {}
    for plugin in index.get("plugins", []):
        if isinstance(plugin, dict):
            check_plugin(plugin)
            # 同一 id 只允许出现一次：重复条目会让商店里出现两个相同的插件，
            # 且更新时无法判断该覆盖哪一条。
            plugin_id = plugin.get("id")
            if isinstance(plugin_id, str) and plugin_id:
                if plugin_id in seen_ids:
                    error(f"插件 id 重复：{plugin_id}")
                else:
                    seen_ids[plugin_id] = True
        else:
            error("plugins 数组项必须是对象")

    print()
    if warnings:
        print(f"警告 {len(warnings)} 条")
    if errors:
        print(f"发现 {len(errors)} 个错误")
        return 1
    print(f"索引校验通过，共 {len(index.get('plugins', []))} 个插件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
