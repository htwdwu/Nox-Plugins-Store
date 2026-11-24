"""把上传的插件包写入商店仓库并直接上架，同时标记为已确认（跳过审核）。

与 ingest_inbox.py 的区别：本脚本由上传者自己发起，产物直接算作「自己人上传」，
所以写完索引条目后立刻往 state/reviewed.json 记一笔，让它不出现在审核 App 的待审核列表。

用法（工作流里调用）：
    python scripts/upload_plugin.py --file /tmp/pkg.zip
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "index.json"
REVIEWED = REPO / "state" / "reviewed.json"
PACKAGES_DIR = REPO / "packages"
ICONS_DIR = REPO / "icons"

MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_ICON_BYTES = 512 * 1024
ICON_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def localized(text) -> dict:
    if isinstance(text, dict):
        default = str(text.get("default") or "").strip()
        zh = str(text.get("zh-CN") or "").strip()
        out = {"default": default or zh}
        if zh:
            out["zh-CN"] = zh
        return out
    return {"default": str(text or "").strip()}


def read_manifest(archive: zipfile.ZipFile) -> dict:
    entry = next(
        (i for i in archive.infolist() if Path(i.filename).name == "plugin.json"),
        None,
    )
    if entry is None:
        raise SystemExit("包内没有 plugin.json")
    return json.loads(archive.read(entry))


def read_icon(archive: zipfile.ZipFile, manifest: dict) -> tuple[str, bytes] | None:
    declared = str(manifest.get("icon") or "").strip()
    candidates = []
    if declared:
        candidates.append(declared)
    candidates += [
        i.filename
        for i in archive.infolist()
        if not i.is_dir()
        and i.filename.startswith("assets/")
        and i.filename.lower().endswith(ICON_SUFFIXES)
    ]
    for path in candidates:
        entry = next((i for i in archive.infolist() if i.filename == path), None)
        if entry is None:
            continue
        data = archive.read(entry)
        if 0 < len(data) <= MAX_ICON_BYTES:
            return Path(path).suffix.lower(), data
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="本地插件包路径")
    args = parser.parse_args()

    package = Path(args.file)
    if not package.is_file():
        raise SystemExit(f"找不到文件: {package}")
    data = package.read_bytes()
    if len(data) > MAX_PACKAGE_BYTES:
        raise SystemExit(f"包超过 {MAX_PACKAGE_BYTES // 1024 // 1024}MB")

    with zipfile.ZipFile(package) as archive:
        manifest = read_manifest(archive)
        icon = read_icon(archive, manifest)

    plugin_id = str(manifest.get("id") or "").strip()
    version = str(manifest.get("version") or "").strip()
    if not plugin_id or not version:
        raise SystemExit("plugin.json 缺少 id 或 version")

    (PACKAGES_DIR / f"{plugin_id}_{version}.zip").parent.mkdir(parents=True, exist_ok=True)
    (PACKAGES_DIR / f"{plugin_id}_{version}.zip").write_bytes(data)

    logo_path = None
    if icon:
        suffix, icon_data = icon
        icon_file = ICONS_DIR / f"{plugin_id}{suffix}"
        icon_file.parent.mkdir(parents=True, exist_ok=True)
        icon_file.write_bytes(icon_data)
        logo_path = icon_file

    index = json.loads(INDEX.read_text(encoding="utf-8"))
    entry = {
        "id": plugin_id,
        "name": localized(manifest.get("name")),
        "description": localized(manifest.get("description")),
        "author": str(manifest.get("publisher") or manifest.get("author") or "").strip(),
        "repository": str(manifest.get("repository") or "").strip(),
        "license": str(manifest.get("license") or "").strip(),
        "version": version,
        "published_at": now_iso(),
        "package_url": (
            "https://raw.githubusercontent.com/xzyy03/Nox-Plugins-Store/main/"
            f"packages/{plugin_id}_{version}.zip"
        ),
    }
    if logo_path is not None:
        entry["logo"] = (
            "https://raw.githubusercontent.com/xzyy03/Nox-Plugins-Store/main/"
            f"icons/{logo_path.name}"
        )

    plugins = [p for p in index.get("plugins", []) if p.get("id") != plugin_id]
    plugins.append(entry)
    plugins.sort(key=lambda p: p["id"])
    index["plugins"] = plugins
    INDEX.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 自己上传的插件直接算已确认，不进待审核列表
    reviewed = {}
    if REVIEWED.is_file():
        reviewed = json.loads(REVIEWED.read_text(encoding="utf-8"))
    reviewed[plugin_id] = {"version": version, "at": now_iso()}
    REVIEWED.parent.mkdir(parents=True, exist_ok=True)
    REVIEWED.write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"已上架 {plugin_id} v{version}，并标记为已确认")
    return 0


if __name__ == "__main__":
    sys.exit(main())
