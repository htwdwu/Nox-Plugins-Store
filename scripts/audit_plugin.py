#!/usr/bin/env python3
"""插件包静态审计。

不是沙箱、也不是完整的安全审查：只做两类判断——
1. 结构性风险：包内有可执行文件、超大文件、路径穿越；
2. 代码风险：脚本里出现插件运行环境不该有的能力（进程、shell、动态执行、提权）。

命中任何一条即判定不通过，交由人工复核后再合并。
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = ROOT / "index.json"
API = "https://api.github.com"
USER_AGENT = "muse-plugin-audit"

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_TEXT_BYTES = 2 * 1024 * 1024

DANGEROUS_PATTERNS = [
    (re.compile(r"\bchild_process\b"), "调用子进程（child_process）"),
    (re.compile(r"\brequire\s*\(\s*['\"](?:child_process|fs|net|http|https|os)['\"]"), "加载 Node 系统模块"),
    (re.compile(r"\bprocess\s*\.\s*(?:binding|dlopen|mainModule)\b"), "访问 Node 进程内部"),
    (re.compile(r"\bnew\s+Function\s*\("), "动态构造函数"),
    (re.compile(r"\beval\s*\("), "动态执行字符串代码"),
    (re.compile(r"\bexecSync\b|\bspawnSync\b|\bspawn\s*\("), "执行外部程序"),
    (re.compile(r"Runtime\s*\.\s*getRuntime\s*\(\s*\)"), "调用 Java Runtime"),
    (re.compile(r"\bsu\s+-c\b|/system/xbin/su"), "提权命令"),
    (re.compile(r"\bpm\s+(?:install|uninstall|grant)\b"), "调用系统包管理器"),
    (re.compile(r"/bin/sh\b|\bsh\s+-c\b"), "调用 shell"),
]

BLOCKED_EXTENSIONS = {
    ".so", ".dex", ".apk", ".jar", ".class", ".elf", ".exe", ".dll", ".dylib",
    ".bin", ".pyc", ".wasm",
}

TEXT_EXTENSIONS = {
    ".js", ".mjs", ".cjs", ".ts", ".html", ".htm", ".css", ".json", ".md",
    ".txt", ".yaml", ".yml", ".vue", ".xml", ".svg",
}


def api_get(url: str, token: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def download(url: str, token: str | None, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"插件包超过 {limit // 1024 // 1024}MB 上限")
    return data


def latest_release_asset(repository: str, version: str | None, token: str | None) -> str:
    owner_repo = repository.rstrip("/").split("github.com/")[-1]
    releases = json.loads(api_get(f"{API}/repos/{owner_repo}/releases", token))
    if not isinstance(releases, list) or not releases:
        raise ValueError("仓库没有 Release")

    candidates = releases
    if version:
        tagged = [r for r in releases if r.get("tag_name", "").lstrip("v") == version]
        if tagged:
            candidates = tagged

    for release in candidates:
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if name.endswith(".zip") or name.endswith(".pluginkit"):
                return asset["browser_download_url"]
    raise ValueError("Release 里没有插件包资产（.zip）")


def audit_archive(data: bytes) -> list[str]:
    problems: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ["插件包不是合法的 zip"]

    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        problems.append(f"文件数过多（{len(infos)}）")

    for info in infos:
        name = info.filename
        if name.startswith("/") or ".." in name.split("/"):
            problems.append(f"路径越界：{name}")
            continue
        suffix = Path(name).suffix.lower()
        if suffix in BLOCKED_EXTENSIONS:
            problems.append(f"包含可执行文件：{name}")
            continue
        if suffix not in TEXT_EXTENSIONS:
            continue
        if info.file_size > MAX_TEXT_BYTES:
            problems.append(f"文本文件过大：{name}")
            continue
        try:
            text = archive.read(info).decode("utf-8", errors="replace")
        except Exception:
            continue
        for pattern, reason in DANGEROUS_PATTERNS:
            match = pattern.search(text)
            if match:
                problems.append(f"{name}: {reason}（{match.group(0)[:40]}）")
                break

    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--token", default=None)
    parser.add_argument("--limit", type=int, default=0, help="只审计前 N 个插件，0 表示全部")
    args = parser.parse_args()

    token = args.token or os.environ.get("GITHUB_TOKEN")
    index = json.loads(Path(args.index).read_text(encoding="utf-8"))
    plugins = index.get("plugins", [])
    if args.limit:
        plugins = plugins[: args.limit]

    failures = 0
    for plugin in plugins:
        plugin_id = plugin.get("id", "?")
        repository = plugin.get("repository", "")
        version = plugin.get("version")
        try:
            url = latest_release_asset(repository, version, token)
            data = download(url, token, MAX_ARCHIVE_BYTES)
            problems = audit_archive(data)
        except (urllib.error.URLError, ValueError, KeyError) as error:
            print(f"✗ {plugin_id}: 无法审计（{error}）")
            failures += 1
            continue

        if problems:
            failures += 1
            print(f"✗ {plugin_id}: 未通过审计")
            for problem in problems[:10]:
                print(f"    - {problem}")
        else:
            print(f"✓ {plugin_id}: 通过")

    print(f"\n审计完成：{len(plugins) - failures} 通过，{failures} 未通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
