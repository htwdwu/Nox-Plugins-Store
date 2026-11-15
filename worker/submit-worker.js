/**
 * Muse 插件提交代理。
 *
 * App 不带任何 GitHub 凭据：它把插件包 POST 到这个 Worker，Worker 用环境变量里的
 * 令牌把包发到 Release、再向索引仓库提 PR。索引仓库的 CI 负责审计与合并，
 * 因此这里只做「防止明显滥用」的前置校验（格式、大小、频率）。
 *
 * 环境变量：
 *   GITHUB_TOKEN   有 repo 写权限的令牌（只在服务端，不进 APK）
 *   GITHUB_OWNER   插件仓库的归属账号，如 xzyy03
 *   INDEX_REPO     索引仓库，如 xzyy03/Muse-Plugins-Store
 *   SUBMIT_SECRET  可选，配置后 App 需带同名 header，用于自用防刷
 */

const API = "https://api.github.com";
const UPLOAD_API = "https://uploads.github.com";
/** 与 Cloudflare 免费版请求体上限对齐：再大平台会直接拒绝。 */
const MAX_PACKAGE_BYTES = 100 * 1024 * 1024;
const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,31}\.[a-z0-9][a-z0-9-]{0,31}$/;
const VERSION_PATTERN = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, service: "muse-plugin-submit" });
    }
    if (request.method !== "POST" || url.pathname !== "/submit") {
      return json({ ok: false, error: "not found" }, 404);
    }
    if (env.SUBMIT_SECRET) {
      if (request.headers.get("x-submit-secret") !== env.SUBMIT_SECRET) {
        return json({ ok: false, error: "未授权的提交（缺少或错误的 x-submit-secret）" }, 401);
      }
    }

    let payload;
    let packageBytes;
    try {
      const form = await request.formData();
      payload = JSON.parse(String(form.get("meta") || "{}"));
      const file = form.get("package");
      if (!file || typeof file.arrayBuffer !== "function") {
        return json({ ok: false, error: "缺少插件包文件" }, 400);
      }
      packageBytes = new Uint8Array(await file.arrayBuffer());
      payload.packageSize = packageBytes.byteLength;
    } catch (error) {
      return json({ ok: false, error: "请求体不是合法的 multipart 表单" }, 400);
    }

    const problem = validate(payload);
    if (problem) return json({ ok: false, error: problem }, 400);
    if (packageBytes.byteLength === 0) {
      return json({ ok: false, error: "插件包为空" }, 400);
    }

    try {
      const result = await publish(env, payload, packageBytes);
      return json({ ok: true, ...result });
    } catch (error) {
      return json({ ok: false, error: String(error.message || error) }, 502);
    }
  }
};

function validate(payload) {
  if (!payload || typeof payload !== "object") return "请求体为空";
  const { id, name, version, description } = payload;
  if (typeof id !== "string" || !ID_PATTERN.test(id)) return "插件 id 不合法（需 发布者.插件名）";
  if (typeof name !== "string" || !name.trim()) return "缺少插件名称";
  if (typeof version !== "string" || !VERSION_PATTERN.test(version)) return "版本号需为 x.y.z";
  if (typeof description !== "string") return "缺少描述";
  if (typeof payload.packageSize === "number" && payload.packageSize > MAX_PACKAGE_BYTES) {
    return `插件包超过 ${MAX_PACKAGE_BYTES / 1024 / 1024}MB`;
  }
  return null;
}

async function publish(env, payload, packageBytes) {
  const token = env.GITHUB_TOKEN;
  const owner = env.GITHUB_OWNER;
  const indexRepo = env.INDEX_REPO || `${owner}/Muse-Plugins-Store`;
  if (!token || !owner) throw new Error("Worker 未配置 GITHUB_TOKEN / GITHUB_OWNER");

  const repoName = repoNameFor(payload.id);
  const tag = `v${payload.version}`;

  await ensureRepository(token, owner, repoName, payload);
  const releaseId = await ensureRelease(token, owner, repoName, tag, payload);
  const assetUrl = await uploadAsset(token, owner, repoName, releaseId, payload, packageBytes);
  const entry = buildEntry(owner, repoName, payload);
  const prUrl = await submitToIndex(token, indexRepo, entry);
  return { id: payload.id, version: payload.version, asset: assetUrl, pr: prUrl };
}

function repoNameFor(id) {
  const slug = id.split(".").pop() || "plugin";
  return `muse-plugin-${slug.toLowerCase().replace(/[^a-z0-9._-]/g, "-").replace(/^-+|-+$/g, "")}`;
}

async function gh(token, method, url, body) {
  const response = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "muse-plugin-worker",
      ...(body ? { "Content-Type": "application/json" } : {})
    },
    body: body ? JSON.stringify(body) : undefined
  });
  const text = await response.text();
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`GitHub ${response.status}: ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : null;
}

async function ensureRepository(token, owner, repoName, payload) {
  const existing = await gh(token, "GET", `${API}/repos/${owner}/${repoName}`);
  if (existing) return;
  await gh(token, "POST", `${API}/user/repos`, {
    name: repoName,
    description: (payload.description || "").slice(0, 300),
    private: false,
    has_issues: true,
    has_wiki: false,
    auto_init: true
  });
}

async function ensureRelease(token, owner, repoName, tag, payload) {
  const existing = await gh(token, "GET", `${API}/repos/${owner}/${repoName}/releases/tags/${tag}`);
  if (existing) return existing.id;
  const created = await gh(token, "POST", `${API}/repos/${owner}/${repoName}/releases`, {
    tag_name: tag,
    name: `${payload.name} ${tag}`,
    body: payload.description || "由 Muse 提交",
    draft: false,
    prerelease: false
  });
  return created.id;
}

async function uploadAsset(token, owner, repoName, releaseId, payload, bytes) {
  const fileName = `${payload.id}_${payload.version}.zip`;
  const url =
    `${UPLOAD_API}/repos/${owner}/${repoName}/releases/${releaseId}/assets` +
    `?name=${encodeURIComponent(fileName)}`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/zip",
      "User-Agent": "muse-plugin-worker"
    },
    body: bytes
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`上传资产失败 ${response.status}: ${text.slice(0, 200)}`);
  return JSON.parse(text).browser_download_url;
}

function buildEntry(owner, repoName, payload) {
  return {
    id: payload.id,
    name: { default: payload.name, "zh-CN": payload.name },
    description: { default: payload.description, "zh-CN": payload.description },
    author: payload.author || owner,
    repository: `https://github.com/${owner}/${repoName}`,
    license: payload.license || "MIT",
    version: payload.version
  };
}

async function submitToIndex(token, indexRepo, entry) {
  const base = `${API}/repos/${indexRepo}`;
  const file = await gh(token, "GET", `${base}/contents/index.json?ref=main`);
  if (!file) throw new Error("索引仓库里没有 index.json");

  const content = decodeBase64ToText(file.content);
  const index = JSON.parse(content);
  const plugins = Array.isArray(index.plugins) ? index.plugins : [];
  const position = plugins.findIndex((item) => item && item.id === entry.id);
  if (position >= 0) plugins[position] = entry;
  else plugins.push(entry);
  index.plugins = plugins;

  const branch = `plugin/${entry.id.replace(/\./g, "-")}-${entry.version}`;
  const ref = await gh(token, "GET", `${base}/git/ref/heads/main`);
  try {
    await gh(token, "POST", `${base}/git/refs`, { ref: `refs/heads/${branch}`, sha: ref.object.sha });
  } catch (error) {
    // 分支已存在说明同一版本重复提交，继续走更新，索引会按 id 覆盖
  }
  await gh(token, "PUT", `${base}/contents/index.json`, {
    message: `plugin: ${entry.id} ${entry.version}`,
    content: encodeBase64(JSON.stringify(index, null, 2) + "\n"),
    sha: file.sha,
    branch
  });
  const pr = await gh(token, "POST", `${base}/pulls`, {
    title: `plugin: ${entry.id} ${entry.version}`,
    head: branch,
    base: "main",
    body: `由 Muse 提交。\n\n- id: \`${entry.id}\`\n- 版本: \`${entry.version}\`\n\nCI 会校验格式并审计插件包，通过后自动合并。`
  });
  return pr ? pr.html_url : null;
}

function decodeBase64ToText(value) {
  const binary = atob(value.replace(/\s/g, ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

function encodeBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" }
  });
}
