# Nox 插件索引

> 本仓库是 [Nox](https://github.com/xzyy03/Nox) 的插件索引与安装包托管处。
> 插件源码由作者维护在自己的 GitHub 仓库；安装包统一存放在本仓库的 `packages/`，
> 经 CI 审计后由 `index.json` 的 `package_url` 直链提供。

**插件开发文档见 [docs/plugin-dev.md](docs/plugin-dev.md)** —— 清单字段、JSON-RPC 协议、
13 种贡献类型、页面桥、调试与发布流程。

## 仓库结构

```text
.
├── index.json              # 插件索引
├── packages/               # 插件安装包 <id>_<version>.zip
├── docs/
│   └── plugin-dev.md       # 插件开发文档
├── schema/
│   └── index.schema.json
├── scripts/
│   ├── validate_index.py   # 索引校验
│   ├── audit_plugin.py     # 安装包审计
│   └── ingest_inbox.py     # 从暂存区收包
├── state/
│   └── rejected.json       # 未通过审计的记录
└── .github/
    └── workflows/
        ├── validate-index.yml
        └── ingest-inbox.yml
```

- `index.json`：插件索引的唯一数据文件。
- `schema/index.schema.json`：索引的 JSON Schema，用于编辑器和校验工具检查格式。
- `scripts/validate_index.py`：本地与 CI 共用的校验脚本。
- `.github/workflows/validate-index.yml`：推送到 `main` 或提交 PR 时自动校验。

## 索引格式

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | integer | 是 | 索引格式版本，当前固定为 `1` |
| `plugins` | array | 是 | 插件条目列表 |

### `plugins[]` 字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 插件唯一标识，必须与包内 `plugin.json` 的 `id` 一致，格式为 `发布者.插件名` |
| `name` | string \| object | 是 | 插件展示名称，可写成字符串或语言映射 |
| `description` | string \| object | 是 | 插件简介，可写成字符串或语言映射 |
| `author` | string | 是 | 发布者标识，必须与包内 `plugin.json` 的 `publisher` 一致 |
| `repository` | string | 是 | 插件源码仓库，必须为 GitHub 仓库地址 |
| `logo` | string | 否 | 插件图标地址，必须为 https URL |
| `homepage` | string | 否 | 项目主页地址，必须为 https URL |
| `license` | string | 否 | 许可证标识，例如 `MIT` |
| `tags` | array | 否 | 分类标签；填写时 1–4 个且不可重复 |
| `version` | string | 否 | 当前版本，SemVer，不能以 `v` 开头；未填写时默认最新版 |
| `min_app_version` | string | 否 | 最低兼容的 App 版本，SemVer 格式 |

`tags` 可选值：

`provider`、`tool`、`skill`、`agent`、`mcp`、`ui`、`integration`、`other`。

### 本地化文本

`name` 与 `description` 可以写成两种形式：

- 单一字符串：对所有语言生效；
- 语言标签到文本的映射，可含 `default` 键：`{"default": "Demo Kit", "zh-CN": "演示套件"}`。

取值顺序：完整标签（`zh-CN`）→ 语言部分（`zh`）→ `default` → `en` → 第一项。标签统一为小写、`-` 分隔，`zh_CN` 与 `zh-cn` 视为同一个；标签必须是 `zh`、`zh-cn` 这类语言标签，最多 16 种。`name` 最多 120 字符且不允许换行，`description` 最多 2000 字符且允许换行。

## 示例

```json
{
  "schema_version": 1,
  "plugins": [
    {
      "id": "author-name.nox-sample-plugin",
      "name": { "default": "Nox Sample Plugin", "zh-CN": "Nox 示例插件" },
      "description": { "default": "A Nox sample plugin.", "zh-CN": "一个 Nox 示例插件。" },
      "author": "author-name",
      "repository": "https://github.com/author-name/nox-sample-plugin",
      "logo": "https://raw.githubusercontent.com/author-name/nox-sample-plugin/main/logo.png",
      "homepage": "https://github.com/author-name/nox-sample-plugin",
      "license": "MIT",
      "tags": ["tool"],
      "version": "1.0.0",
      "min_app_version": "1.14.0"
    }
  ]
}
```

## 安装包托管

安装包放在 `packages/<id>_<version>.zip`，索引里用 `package_url` 直链指向它。

上传有两条路：

1. **自动收包**：在 App 里用插件页顶栏的上传图标提交，包写入暂存仓库
   [`yyds0399/Muse-Plugins-Inbox`](https://github.com/yyds0399/Muse-Plugins-Inbox) 的 `incoming/`；
   本仓库的 `ingest-inbox.yml` 定时扫描，解包读清单 → 审计 → 复制进 `packages/` 并更新 `index.json`。
2. **直接提 PR**：自己把 `packages/<id>_<version>.zip` 与 `index.json` 的改动一起提交。

审计会拦截：路径越界、可执行文件、`eval` / `new Function`、`Runtime.getRuntime`、
`su` 提权、`pm install/grant`、`/bin/sh` 或 `sh -c` 拼接。
放行 `child_process`、`spawn`、`execSync` 及常见 Node 内置模块（插件本身就是 Node 进程）。

同一个 `id` 只保留一个版本：新版本上架后旧包会被替换。

## 维护约定

- 一个插件对应 `plugins` 数组中的一个对象。
- 插件作者更新时修改自己的对象并提交 PR。
- 推送到 `main` 和 PR 会触发 GitHub Actions：校验索引格式、GitHub 仓库、`package_url` 可下载性，以及包内 `plugin.json` 的 `id`、`version`、`publisher` 与 `name` / `description`。
- 索引中出现的每种语言文本必须与包内清单一致；索引未覆盖的语言只给出警告。
- 提交前可在本地运行 `python3 scripts/validate_index.py`，需要先安装依赖：`python3 -m pip install jsonschema`。

## 人工审核（可选）

插件上传到暂存区后由 `ingest-inbox` 自动审计并上架，不阻塞流水线。
若需要人工把关，可用审核 App 或在 Actions 里手动触发 `Plugin review action`：

- **通过**：在 `state/reviewed.json` 记录 `{id: {version, at}}`。已记录当前版本的插件不再出现在待审列表。
- **拒绝**：从 `index.json` 移除该插件，删除 `packages/<id>_<version>.zip` 与 `icons/<id>.*`，并把原因写入 `state/rejected.json`。

待审核的定义是「索引里有、但 `reviewed.json` 里记录的版本与当前版本不一致」。

工作流入参：

| 输入 | 说明 |
|---|---|
| `action` | `approve` 或 `reject` |
| `plugin_ids` | 插件 id，多个用英文逗号分隔 |
| `reason` | 拒绝原因，可空 |
