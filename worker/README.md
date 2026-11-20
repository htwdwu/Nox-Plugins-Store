# Nox 插件提交代理（Cloudflare Worker）

Nox 的「上传插件」不把 GitHub 令牌放进 APK。App 只把插件包 POST 到这里，Worker 用
服务端的令牌完成建仓库、发 Release、向索引仓库提 PR；索引仓库的 CI 负责审计与合并。

## 部署（一次性，免费）

1. 打开 https://dash.cloudflare.com/ → Workers & Pages → Create → Worker，名字随意。
2. 用本目录的 `submit-worker.js` 内容替换默认代码，Deploy。
3. 在 Worker 的 Settings → Variables 里添加：

   | 变量 | 必填 | 说明 |
   |---|---|---|
   | `GITHUB_TOKEN` | 是 | 有 `repo` 权限的令牌（建议 fine-grained，只勾选你的插件仓库与 `Nox-Plugins-Store` 的 Contents / Pull requests 读写）。**勾选 Encrypt 保存** |
   | `GITHUB_OWNER` | 是 | 插件仓库归属账号，例如 `xzyy03` |
   | `INDEX_REPO` | 否 | 索引仓库，默认 `<GITHUB_OWNER>/Nox-Plugins-Store` |
   | `SUBMIT_SECRET` | 否 | 自用防刷口令；配了之后 App 也要填同样的值 |

4. 复制 Worker 地址（形如 `https://nox-plugin-submit.<你的子域>.workers.dev`），
   填进 Nox 的「插件设置 → 上传服务地址」。

## 接口

```
POST /submit
Content-Type: application/json

{
  "id": "xzyy03.demo-kit",
  "name": "示例工具箱",
  "version": "1.0.0",
  "description": "插件说明",
  "author": "xzyy03",
  "license": "MIT",
  "package": "<zip 的 base64>"
}
```

成功返回 `{ "ok": true, "pr": "<PR 链接>" }`；失败返回 `{ "ok": false, "error": "..." }`。

`GET /health` 用于确认 Worker 是否在线。

## 限制

- 插件包上限 8MB（App 侧同样限制）
- `id` 必须是 `发布者.插件名` 形式，版本必须是 `x.y.z`
- 只做格式与大小校验；**是否恶意由索引仓库的 CI 审计决定**，审计不通过 PR 不会合并
- 重复的 `id` 会按 id 覆盖索引条目，版本号相同时资源会被替换
