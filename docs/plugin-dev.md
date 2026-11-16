# Muse 插件开发文档

Muse 的插件是一个 zip 包：根目录放一份 `plugin.json` 清单，加上代码、页面、schema 等文件。
装好后它可以给 App 增加 AI 工具、技能、子代理、工作区工具、界面入口、钩子、MCP 服务器和模型供应商。

本文档面向插件作者。看完你应该能写出、调试并发布一个插件。

---

## 一、最小插件

三个文件就够：

```
hello-plugin/
├── plugin.json
├── dist/main.js
└── schemas/echo.input.json
```

`plugin.json`：

```json
{
  "schema_version": 1,
  "id": "yourname.hello",
  "name": { "default": "Hello Plugin", "zh-CN": "打个招呼" },
  "description": { "default": "Echo text back.", "zh-CN": "把文本原样回显。" },
  "publisher": "yourname",
  "version": "1.0.0",
  "api_version": 1,
  "runtime": { "type": "javascript", "entry": "dist/main.js", "resident": false },
  "contributes": {
    "tools": [
      {
        "id": "echo",
        "title": { "zh-CN": "回显", "default": "Echo" },
        "description": { "zh-CN": "把传入的文本原样回显。", "default": "Echo text back." },
        "inputSchema": "schemas/echo.input.json",
        "handler": "tools.echo"
      }
    ]
  }
}
```

`schemas/echo.input.json`：

```json
{
  "type": "object",
  "properties": {
    "text": { "type": "string", "description": "要回显的文本" }
  },
  "required": ["text"]
}
```

`dist/main.js`：

```js
const readline = require('readline');

const handlers = {
  'tools.echo': async (args) => ({ text: String(args.text || '') }),
};

const CALL_METHODS = ['tool.invoke', 'hook.invoke', 'ui.invoke'];

function send(message) {
  process.stdout.write(JSON.stringify(message) + '\n');
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let request;
  try {
    request = JSON.parse(line);
  } catch (e) {
    return;
  }
  if (request.id === undefined) return; // 单向通知，不用回
  const method = request.method;
  const params = request.params || {};
  if (CALL_METHODS.indexOf(method) < 0) {
    return send({ jsonrpc: '2.0', id: request.id, error: { code: 'METHOD_UNKNOWN', message: method } });
  }
  const handler = handlers[params.handler];
  if (typeof handler !== 'function') {
    return send({ jsonrpc: '2.0', id: request.id, error: { code: 'HANDLER_NOT_FOUND', message: params.handler } });
  }
  Promise.resolve()
    .then(() => handler(params.args || {}))
    .then((result) => send({ jsonrpc: '2.0', id: request.id, result: result === undefined ? null : result }))
    .catch((error) =>
      send({
        jsonrpc: '2.0',
        id: request.id,
        error: { code: 'PLUGIN_ERROR', message: String((error && error.message) || error) },
      })
    );
});

process.on('uncaughtException', (e) => {
  process.stderr.write('uncaught: ' + (e && e.stack ? e.stack : e) + '\n');
});
process.on('unhandledRejection', (e) => {
  process.stderr.write('unhandled rejection: ' + e + '\n');
});
```

打包（注意是 `zip -r` 当前目录内容，不要把外层文件夹压进去）：

```bash
cd hello-plugin && zip -r ../hello-plugin-1.0.0.zip . -x '*.DS_Store'
```

装法：设置 → 插件 → 右上角 `+` 选 zip。

---

## 二、`plugin.json` 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 清单格式版本，固定 `1` |
| `id` | 是 | 全局唯一，格式 `发布者.插件名`，允许中文等 Unicode 字母 |
| `name` | 是 | 展示名，字符串或语言映射 |
| `description` | 是 | 简介，字符串或语言映射 |
| `publisher` | 是 | 发布者标识，允许中文 |
| `version` | 是 | SemVer，不能以 `v` 开头 |
| `api_version` | 是 | 宿主插件 API 版本，当前 `1` |
| `min_app_version` | 否 | 需要的最低 App 版本，SemVer |
| `runtime` | 否 | 独立进程声明，见下节 |
| `contributes` | 是 | 贡献声明，至少要有一项 |
| `icon` | 否 | 包内图标路径，文件必须真的打进包里 |
| `homepage` / `repository` / `license` | 否 | 元信息 |

`contributes` 可包含 13 种贡献：
`tools`、`skills`、`agents`、`workspaceTools`、`uiEntries`、`hooks`、`mcp`、`providers`、
`shizuku`、`overlay`、`backup`、`modelAccess`、`configPage`。

---

## 三、runtime 与 JSON-RPC

### 3.1 进程怎么起

```json
"runtime": {
  "type": "javascript",
  "entry": "dist/main.js",
  "dependencies": { "tool": "node", "file": "package.json" },
  "resident": false
}
```

- `type`：`javascript`（用 node 起）或 `python`（用 python3 起）
- `entry`：包内入口文件
- `dependencies`：声明后宿主会在启动前装依赖
- `resident`：`true` 表示不参与空闲回收

### 3.2 **最要紧的一条：`method` 是调用类别，不是 handler 名**

宿主调用插件时 `method` 固定是三个之一，handler 名放在 `params.handler`，参数放在 `params.args`：

| `method` | 触发场景 |
|---|---|
| `tool.invoke` | AI 调用了插件贡献的工具 |
| `hook.invoke` | 插件声明的 hook 事件被触发 |
| `ui.invoke` | 插件页面调用 `aicode.invoke(...)`，或 `uiEntries` 的动作型入口 |

正确的消息：

```json
{
  "jsonrpc": "2.0",
  "id": "call-1",
  "method": "tool.invoke",
  "params": { "handler": "tools.echo", "args": { "text": "hello" } }
}
```

**不要**把 handler 名当 `method`：

```json
{ "jsonrpc": "2.0", "id": "call-1", "method": "tools.echo", "params": { "text": "hello" } }
```

按这种写法实现，宿主每次调用都命中「未知 method」分支。症状是**页面一直转圈、工具调用超时**，看着像插件没起来，其实进程活着但一直在回错误。

### 3.3 应答与错误

```json
{ "jsonrpc": "2.0", "id": "call-1", "result": { "text": "hello" } }
```

出错用 `error: { code, message }`，宿主会把它显示成工具失败原因。建议的错误码：

| code | 含义 |
|---|---|
| `METHOD_UNKNOWN` | 调用类别不认识（多半是协议写错） |
| `HANDLER_NOT_FOUND` | 类别对，但 handler 没注册 |
| `PLUGIN_ERROR` | handler 内部抛错 |

### 3.4 进程必须自己保证不退出

任何未捕获异常都会让进程退出，宿主侧表现是 `PLUGIN_EXITED` / 「插件进程 stdout 已关闭」。
必须挂 `uncaughtException` 与 `unhandledRejection`（见第一节骨架），Python 用 `try/except` 包住主循环。

### 3.5 `resident` 该不该开

**要开**：插件要在没人主动调用时也保持活着——起本地服务（如 `providers` 的 baseUrl）、维持长连接、监听 hook 事件、提供悬浮窗、页面随时可能 `aicode.invoke` 过来。

**不要开**：只靠工具调用、没有后台逻辑的插件。让宿主按需拉起就行，省内存。

漏设的症状：插件能装、页面能开，但过一会儿点按钮就报 `PLUGIN_EXITED`。非 resident 进程空闲 10 分钟后会被回收。

### 3.6 可调用的宿主方法

插件进程通过 `{ "jsonrpc": "2.0", "id": "r1", "method": "host.xxx", "params": {...} }` 请求宿主：

| 方法 | 用途 |
|---|---|
| `host.chat.openSession` / `getSession` / `renameSession` / `pinSession` / `deleteSession` | 会话管理 |
| `host.chat.getRunState` / `waitForRun` | 读运行状态、等待一轮结束 |
| `host.chat.listApprovals` / `resolveToolApproval` / `resolveQuestion` / `resolvePlanApproval` | 审批与提问 |
| `host.model.listProviders` / `listModels` | 列供应商与模型 |
| `host.model.create` / `start` / `read` / `cancel` / `close` | 模型句柄：流式调用 |
| `host.clipboard.read` / `write` | 剪贴板 |
| `host.share.text` | 系统分享 |
| `host.ui.openUrl` | 打开链接 |
| `host.app.info` | 宿主版本、包名 |
| `host.log` | 写宿主日志文件 |
| `host.tools.execute` / `pending` / `resolve` | 调用宿主工具、处理待审批 |
| `host.mcp.status` | MCP 连接状态 |
| `host.http.request` | 受管 HTTP，会经过 `agent.http.before` hook |
| `host.fs.read` / `write` / `list` / ... | 文件操作，支持 `data://` |

---

## 四、贡献类型

### 4.1 tools —— AI 可调用的工具

```json
{
  "id": "echo",
  "title": { "zh-CN": "回显", "default": "Echo" },
  "description": { "zh-CN": "把传入的文本原样回显。", "default": "Echo text back." },
  "inputSchema": "schemas/echo.input.json",
  "handler": "tools.echo"
}
```

- `inputSchema` 指向包内 JSON Schema
- `handler` 是 runtime 里注册的函数名
- 装好后 AI 侧看到的工具名形如 `plugin__<插件ID>__<工具ID>`
- 没有 `runtime` 却声明 `tools` → 清单无效

### 4.2 skills / agents

```json
"skills": [ { "id": "notes", "file": "skills/project-notes/SKILL.md" } ],
"agents": [ { "id": "reviewer", "file": "agents/reviewer.md" } ]
```

装好后在 AI 的能力清单里显示为 `<插件ID>:<名字>`，只读，用户不能在技能页编辑。

### 4.3 workspaceTools —— 右侧「项目工具」抽屉

```json
{
  "id": "snapshot",
  "title": { "zh-CN": "项目快照" },
  "description": { "zh-CN": "列出当前项目的文件结构。" },
  "icon": "layers",
  "category": "通用",
  "pageFile": "pages/project-snapshot.html"
}
```

字段名是 **`pageFile`**（不是 `page`），`id` / `title` / `description` / `icon` / `category` / `pageFile` 全部必填。

### 4.4 uiEntries —— 界面入口（13 个槽位）

```json
{ "slot": "chat.header.menu", "id": "open", "title": { "zh-CN": "打开面板" }, "page": "pages/panel.html" }
```

槽位：`settings.general`、`settings.ai`、`settings.environment`、`settings.permissions`、
`settings.data`、`settings.help`、`drawer.footer`、`chat.header.menu`、`chat.welcome`、
`chat.session.menu`、`files.tree.menu`、`editor.selection.menu`、`backup.data`。

**每个槽位最多 2 条**。`backup.data` 只能给 `handler`，不能带 `page`。
只给 `handler` 的条目是动作型入口，走 `ui.invoke`。

### 4.5 hooks —— 生命周期与拦截

```json
{ "id": "h1", "event": "agent.tool.before", "mode": "transform", "handler": "hooks.before" }
```

| mode | 能做的事 |
|---|---|
| `observe` | 只观察，返回内容被忽略 |
| `transform` | 等待宿主应用结构化变更 |
| `review` | 只在宿主判定 ASK 后做代审核，返回 allow / deny / pass |

事件表：

| 事件 | 模式 | 说明 |
|---|---|---|
| `app.started` | observe | App 进程启动完成 |
| `agent.run.started` / `finished` | observe | 一次对话轮次 |
| `agent.request.started` / `finished` | observe | 一次模型请求 |
| `agent.tool.started` / `finished` | observe | 一次工具调用 |
| `agent.compaction` | observe | 上下文压缩 |
| `agent.tool.before` | transform | 可拒绝或改写工具参数 |
| `agent.tool.after` | transform | 只可改写成功结果的 `data` |
| `agent.http.before` | transform | 改写 provider 请求，鉴权头以 `{{Token}}` 占位 |
| `agent.permission.review` | review | ASK 分支代审核 |
| `plugin.installed` / `enabled` / `disabled` / `uninstalled` | observe | 只派发给自身 |
| `backup.export.before` / `backup.import.finished` | observe | 需同时声明 `contributes.backup` |

`transform` 的返回结构：

```js
return { decision: 'deny', reason: '插件拒绝访问 .env' };  // agent.tool.before
return { arguments: { ...newArgs } };                      // 改写参数
return {};                                                 // 放行
return { data: { ...newData } };                           // agent.tool.after
return { url, headers, body };                             // agent.http.before
```

`review` 的返回：`{ decision: 'allow' }` / `{ decision: 'deny', reason }` / `{ decision: 'pass' }`。

### 4.6 mcp

```json
"mcp": [ { "id": "fs", "file": "mcp/fs.json" } ]
```

装好后服务器名带 `plugin__` 前缀，由插件启停控制，不写进用户的 mcp.json。

### 4.7 providers

```json
"providers": [
  {
    "id": "local",
    "name": { "zh-CN": "本地网关" },
    "description": { "zh-CN": "..." },
    "format": "openai",
    "baseUrl": "http://127.0.0.1:7864/v1"
  }
]
```

`format` 目前只接受 `openai`。宿主侧 id 为 `plugin:<插件ID>:<供应商ID>`，列表里带「插件」标签。
**声明了 providers 就要设 `resident: true`**，否则空闲回收后本地服务没了，点进去报连接失败。

### 4.8 shizuku / overlay / backup / modelAccess

- `shizuku`、`backup`、`modelAccess` 只接受 `purpose`
- `overlay` 支持 `purpose` + `surfaces[]`，最多 3 个

这些是高危能力，用户在插件详情页逐个授权。

### 4.9 configPage

```json
"configPage": { "page": "pages/config.html", "title": { "zh-CN": "设置" } }
```

在插件详情页出现「配置」入口。

---

## 五、页面桥

### 5.1 引入方式：必须绝对路径

```html
<script src="/__aicode/aicode.js"></script>
```

`/__aicode/` 映射到宿主内置资源。写成相对路径会落到插件目录里，导致 404，页面报 `aicode is not defined`。

同一目录下还有：`vue.global.prod.js`、`aicode-ui-vue.js`、`ui.css`、`eruda.min.js`、`markdown/`。

### 5.2 调用方式

```js
await aicode.call('app.info');
await aicode.invoke('tools.echo', { name: '世界' });     // 调自己 runtime 的 handler
await aicode.bash('ls -la');
await aicode.tools.execute('Bash', { command: 'ls' });
aicode.toast('已保存');
const ctx = await aicode.context();
```

`aicode.invoke(handler, args)` 是最常用的：handler 名与 runtime 的 `handlers` 键名一致，
宿主把它转成 `ui.invoke` 发给插件进程。

**桥没有 `plugin` 命名空间**，`aicode.plugin.invoke(...)` 不存在。

### 5.3 文件与 `data://`

路径支持绝对路径、`~/` 家目录、`data://` 私有存储。

```js
await aicode.fs.write('data://config.json', JSON.stringify(cfg));
const file = await aicode.fs.read('data://config.json');
const cfg = JSON.parse(file.content || '{}');
```

**`data://` 是配置持久化的标准做法**：映射到插件自己的数据目录，卸载时自动清除。
宿主不提供 `config.get/set`，配置就是自己读写一个 `data://` 下的 JSON 文件。

### 5.4 其他可用方法

- 文件：`fs.read`、`fs.readBinary`、`fs.write`、`fs.append`、`fs.list`、`fs.stat`、`fs.mkdir`、`fs.delete`、`fs.rename`、`fs.copy`、`fs.watch`、`fs.unwatch`
- 悬浮窗：`overlay.create`、`overlay.update`、`overlay.close`、`overlay.self`、`overlay.resize`、`overlay.setDragRegion`、`overlay.postMessage`、`overlay.onMessage`
- 会话：`chat.createSession`、`listSessions`、`openSession`、`getSession`、`renameSession`、`pinSession`、`deleteSession`、`listMessages`、`sendMessage`、`setDraft`、`stop`、`getRunState`、`waitForRun`、`listApprovals`、`resolveToolApproval`、`resolveQuestion`、`resolvePlanApproval`
- 工具：`tools.list`、`tools.execute`、`tools.pending`、`tools.resolve`
- 终端：`terminal.run`、`terminal.poll`、`terminal.write`、`terminal.stop`、`terminal.close`、`terminal.list`
- 其他：`app.info`、`app.isForeground`、`context`、`clipboard.read/write`、`share.text`、`ui.openUrl`、`ui.close`、`ui.overlay`、`bash`、`shizuku.exec`、`secrets.get/set/list/delete`、`toast`、`tip`

---

## 六、调试

### 6.1 开发目录

设置 → 插件 → 齿轮 →「开发者侧载」。开启后 `~/.muse/plugin-dev/<项目目录>/`
下每个含 `plugin.json` 的子目录都出现在「开发中」标签页，改文件会自动重打包替换。

### 6.2 让 AI 自己装

宿主内置 `plugin` 工具，AI 可以直接操作：

| action | 作用 |
|---|---|
| `list` | 列出已装插件、状态、各自贡献，并给出 `devDir` 路径 |
| `install` | `source=dev` 按目录名装开发目录项目；`source=zip` 按路径装本地包 |
| `reload` | 重新打包并热替换开发目录项目 |
| `enable` / `disable` / `uninstall` | 启停与卸载 |
| `logs` | 读该插件最近运行日志 |

自举流程：写文件到 `~/.muse/plugin-dev/<目录>/` → `install(source=dev)` → 新工具立刻可用 →
改代码 → `reload` → 起不来 → `logs` 看 `stderr` 与退出码。

### 6.3 日志

`/storage/emulated/0/Android/data/com.muse/files/logs/`，应用内「设置 → 日志」也能看。
调成 Debug 级别能看到插件通信细节。插件进程的日志行带 `[插件ID]` 前缀。

### 6.4 排查清单

1. 插件在「已安装」列表里吗？不在 → 安装没走完
2. 开关能点吗？灰的 → 清单解析失败，详情页会指出具体字段
3. 只声明 `tools` 的插件不会有任何界面入口，这是正常的
4. 页面打不开 → 检查 `page` 路径真实存在、`.html` 结尾、大小写一致
5. 工具调不通 → 检查 `handler` 名与 runtime 注册的一致、`inputSchema` 是合法 JSON
6. 页面能开但一直转圈 → 先看 `typeof aicode` 是否 `undefined`（桥没加载），
   再查 runtime 是否在回 `METHOD_UNKNOWN`（协议写错）
7. 报 `PLUGIN_EXITED` → 进程挂了或被回收：常驻型要设 `resident: true`，并挂 `uncaughtException`
8. 报 `aicode is not defined` → 页面没引 `/__aicode/aicode.js`，或写成了相对路径
9. 本地端口连不上 → 多半是 handler 没被调用起来，先修协议

---

## 七、发布

### 7.1 上传

App 里插件页顶栏的上传图标 → 选包 → 包被写入暂存仓库
[`yyds0399/Muse-Plugins-Inbox`](https://github.com/yyds0399/Muse-Plugins-Inbox) 的 `incoming/`。

### 7.2 自动上架

商店仓库 [`xzyy03/Muse-Plugins-Store`](https://github.com/xzyy03/Muse-Plugins-Store)
的 Actions 定时扫描暂存区：解包读清单 → 审计 → 通过后复制进 `packages/` 并更新 `index.json`。

### 7.3 审计规则

**会拦截**：路径越界、可执行文件、`eval` / `new Function`、`Runtime.getRuntime`、
`su` 提权、`pm install/grant`、`/bin/sh` 或 `sh -c` 拼接。

**放行**（插件本来就是 Node 进程）：`child_process`、`spawn`、`execSync`、
`require('fs'/'http'/'https'/'net'/'os')`、宿主自带的 `eruda.min.js` 与 `vue.global.prod.js`。

### 7.4 直接改索引

也可以自己往仓库提 PR 修改 `index.json`。字段说明见 [README](../README.md)。

`index.json` 里每条插件的 `package_url` 指向 `packages/<id>_<version>.zip`。
同一个 `id` 只保留一个版本，新版本上架后旧包会被替换。

---

## 八、常见坑

- `slot` 拼错 → 整个清单解析失败，插件状态变错误、开关灰掉
- `contributes` 全空（或只写了不认识的键）→ 清单无效，至少要有一项真实贡献
- 每个槽位最多 2 条 `uiEntries`，超了无效
- `backup.data` 槽位不能带 `page`，只能是 `handler` 动作
- 声明 `tools` 或 `hooks` 却没写 `runtime` → 无效
- 页面文件必须真实存在于包内，路径大小写敏感
- 常驻型插件漏设 `resident: true` → 空闲 10 分钟后被回收
- 宿主调插件时 `method` 是 `tool.invoke` / `hook.invoke` / `ui.invoke`，handler 名在 `params.handler`
- 页面里调自己 runtime 的 handler 用 `aicode.invoke(handler, args)`
- 配置持久化写 `data://xxx.json`，别写别的路径
- 改版本号才会触发「更新」，同版本重装被当作替换
- 商店索引更新后 App 需要下拉刷新才看到新条目
- `icon` 写图片路径时文件必须真的打进包里
- 中文 `publisher` / `id` 是允许的，但索引里的 `author` 建议用 ASCII

---

## 参考

- 应用内技能：`muse-plugin-dev`（AI 写作插件时会自动加载）
- 索引仓库：[xzyy03/Muse-Plugins-Store](https://github.com/xzyy03/Muse-Plugins-Store)
- 上传暂存区：[yyds0399/Muse-Plugins-Inbox](https://github.com/yyds0399/Muse-Plugins-Inbox)
- 主程序：[xzyy03/Muse](https://github.com/xzyy03/Muse)
