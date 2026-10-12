# gateway-deliver

通用 HTTP 请求转发网关，运行在 **Cloudflare Workers（Python）** 上。  
外部调用本服务后，根据请求中的「转发目标」把请求原样转到对应 HTTP 地址，并尽量完整回传上游响应（方法、状态码、Header、Body）。

> 说明：这类 API 网关应部署到 **Workers**。Cloudflare Pages 面向静态站点；本项目不走 Pages。

## 快速开始

### 环境要求

- [Node.js](https://nodejs.org/)（Wrangler 需要）
- [uv](https://docs.astral.sh/uv/) ≥ **0.12.3**（`pywrangler` 依赖；可用 `uv self update` 升级）

### 安装与本地调试

```bash
uv sync
uv run pywrangler dev
```

浏览器或 curl 访问 `http://127.0.0.1:8787/` 可看到健康检查与用法说明。

### 部署到 Cloudflare

**本地 CLI（推荐先验证）：**

```bash
# 登录（首次）
uv run pywrangler login

# 部署
uv run pywrangler deploy
```

**Git 连接自动部署（Workers Builds）：**

本项目是 **Workers**，不是 Pages。在 Worker → **Settings → Builds** 中配置：

| 设置 | 值 |
|------|-----|
| Build command | **留空**（本项目无前端构建步骤） |
| Deploy command | `uv run pywrangler deploy` |
| Non-production deploy | `uv run pywrangler versions upload` |

若 Build command 写成 `uv run build`，会报 `Failed to spawn: build`（仓库里没有名为 `build` 的命令）。不要用 Pages 的「输出目录」流程部署本项目。

可选：设置访问令牌（部署后在 Dashboard 或 CLI 配置 Secret）：

```bash
uv run pywrangler secret put GATEWAY_TOKEN
```

调用时带上：

```http
Authorization: Bearer <token>
# 或
X-Gateway-Key: <token>
```

## 如何指定转发目标

优先级从高到低：

| 方式 | 示例 | 说明 |
|------|------|------|
| Header 绝对地址 | `X-Forward-To: https://httpbin.org/post` | 最明确，推荐 |
| Query | `?__target=https://httpbin.org/get` | 适合简单 GET |
| 命名路由 | `X-Route: demo` | 需配置 `ROUTE_MAP`，路径接在 base 后 |
| 仅改 Host | `X-Forward-Host: httpbin.org` | 保留网关侧 path/query |
| Path 嵌入 | `/proxy/https://httpbin.org/get` | URL 写在路径里 |

等价 Header：`X-Target-Url`；等价 Query：`__url`；等价 Route Query：`__route`；等价 Host Query：`__host`。

### 调用示例

**1. Header 指定完整上游 URL（推荐）**

```bash
curl -X POST "https://<your-worker>.workers.dev/" \
  -H "X-Forward-To: https://httpbin.org/post" \
  -H "Content-Type: application/json" \
  -d '{"hello":"world"}'
```

**2. 保留路径，只换上游 Host**

```bash
# 请求网关 /anything/foo → 转发到 https://httpbin.org/anything/foo
curl "https://<your-worker>.workers.dev/anything/foo" \
  -H "X-Forward-Host: httpbin.org"
```

**3. 命名路由（适合固定后端）**

在 `wrangler.toml`：

```toml
[vars]
ROUTE_MAP = '{"demo":"https://httpbin.org","pet":"https://petstore.swagger.io/v2"}'
ALLOWED_HOSTS = "httpbin.org,petstore.swagger.io"
```

```bash
curl "https://<your-worker>.workers.dev/get" -H "X-Route: demo"
```

**4. Path 模式**

```bash
curl "https://<your-worker>.workers.dev/proxy/https://httpbin.org/get"
```

## 透传行为

- **Method / Status / Body**：原样转发
- **Request Header**：过滤 hop-by-hop（`Host`、`Connection`、`Transfer-Encoding` 等）以及网关自身控制头（`X-Forward-To` 等）
- **若配置了 `GATEWAY_TOKEN`**：不会把网关鉴权用的 `Authorization` / `X-Gateway-Key` 转给上游
- **Response Header**：过滤 hop-by-hop，并附加 `X-Gateway: gateway-deliver`
- **Redirect**：`redirect: manual`，由客户端处理 3xx
- **CORS**：支持 `OPTIONS` 预检；若请求带 `Origin`，响应会回写 `Access-Control-Allow-Origin`

## 配置项（`wrangler.toml` / Secrets）

| 变量 | 类型 | 说明 |
|------|------|------|
| `GATEWAY_TOKEN` | Secret | 非空则要求鉴权 |
| `ALLOWED_HOSTS` | Var | 逗号分隔主机白名单；空=不限制 |
| `ROUTE_MAP` | Var | JSON：`{"name":"https://base"}` |

## 项目结构

```text
src/
  entry.py         # Workers 入口
  gateway.py       # 鉴权、组请求、转发、回写响应
  target.py        # 解析转发目标
  headers_util.py  # Header 过滤规则
wrangler.toml      # Cloudflare Workers 配置
pyproject.toml     # Python / pywrangler 依赖
```

## 安全建议

1. 生产环境务必设置 `GATEWAY_TOKEN`，避免网关被当成开放代理滥用。
2. 配合 `ALLOWED_HOSTS` 限制可访问的上游域名。
3. 不要把仅内网可达的地址配进 `ROUTE_MAP`（Workers 出网是公网视角）。
