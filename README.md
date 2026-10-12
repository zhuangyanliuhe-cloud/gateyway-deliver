# gateway-deliver

通用 HTTP 请求转发网关。外部调用本服务后，根据请求中的「转发目标」把请求转到对应 HTTP 地址，并回传上游的状态码、Header 和 Body。

进程是普通的 Python ASGI 服务，用 uvicorn 常驻，适合部署在 AWS EC2 上。上游可以是公网，也可以是 VPC 内网地址。

## 快速开始

### 环境要求

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)

### 安装与本地调试

```bash
uv sync
uv run python src/app.py
```

默认监听 `127.0.0.1:8787`。浏览器或 curl 访问 `http://127.0.0.1:8787/health` 可看到健康检查与用法说明。

本地要改端口或加上鉴权时，先复制环境变量文件再启动：

```bash
cp .env.example .env
# 编辑 .env 后：
set -a && source .env && set +a
uv run python src/app.py
```

Windows PowerShell：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#][^=]*)=(.*)$') { Set-Item -Path "env:$($Matches[1].Trim())" -Value $Matches[2] }
}
uv run python src/app.py
```

## 部署到 EC2

在实例上安装 Python 3.12 和 uv，把仓库放到 `/opt/gateway-deliver`。

```bash
sudo useradd --system --home /opt/gateway-deliver --shell /usr/sbin/nologin gateway
sudo chown -R gateway:gateway /opt/gateway-deliver
cd /opt/gateway-deliver
uv sync --frozen
sudo cp .env.example /etc/gateway-deliver.env
sudo chmod 600 /etc/gateway-deliver.env
# 编辑 /etc/gateway-deliver.env，至少设置 GATEWAY_TOKEN 和 ALLOWED_HOSTS
sudo cp deploy/gateway-deliver.service /etc/systemd/system/gateway-deliver.service
sudo systemctl daemon-reload
sudo systemctl enable --now gateway-deliver
```

服务监听 `127.0.0.1:8787`，不对公网开放。前面用 Nginx 或 ALB 做 TLS，健康检查路径是 `GET /health`（不需要令牌）。改完 `/etc/gateway-deliver.env` 后执行 `sudo systemctl restart gateway-deliver`。

安全组只放行 443 到 Nginx/ALB。应用端口留在本机。

生产环境务必设置：

| 变量 | 说明 |
|------|------|
| `GATEWAY_TOKEN` | 非空则要求 `Authorization: Bearer <token>` 或 `X-Gateway-Key` |
| `ALLOWED_HOSTS` | 逗号分隔的上游主机白名单 |
| `ROUTE_MAP` 或 `ROUTE_MAP_FILE` | 命名路由，JSON：`{"demo":"https://httpbin.org"}` |
| `GATEWAY_UPSTREAM_TIMEOUT` | 上游超时秒数，默认 60；`0` 表示不限制 |
| `BIND_HOST` / `BIND_PORT` | 监听地址，默认 `127.0.0.1:8787` |
| `GATEWAY_MAX_BODY_BYTES` | 请求体上限，默认 33554432；`0` 为不限制 |

`ROUTE_MAP` 含引号时，systemd 的 EnvironmentFile 会吃掉双引号。复杂 JSON 写到文件里，用 `ROUTE_MAP_FILE` 指向它。

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
curl -X POST "http://127.0.0.1:8787/" \
  -H "Authorization: Bearer <token>" \
  -H "X-Forward-To: https://httpbin.org/post" \
  -H "Content-Type: application/json" \
  -d '{"hello":"world"}'
```

**2. 保留路径，只换上游 Host**

```bash
# 请求网关 /anything/foo → 转发到 https://httpbin.org/anything/foo
curl "http://127.0.0.1:8787/anything/foo" \
  -H "Authorization: Bearer <token>" \
  -H "X-Forward-Host: httpbin.org"
```

**3. 命名路由（适合固定后端）**

```bash
curl "http://127.0.0.1:8787/get" \
  -H "Authorization: Bearer <token>" \
  -H "X-Route: demo"
```

**4. Path 模式**

```bash
curl "http://127.0.0.1:8787/proxy/https://httpbin.org/get" \
  -H "Authorization: Bearer <token>"
```

## 透传行为

- **Method / Status / Body**：原样转发。请求体先读入（默认上限 32MiB，可用 `GATEWAY_MAX_BODY_BYTES` 调整，`0` 为不限制）再以 `Content-Length` 发给上游；响应体按流式回写
- **Request Header**：过滤 hop-by-hop（`Host`、`Connection`、`Transfer-Encoding` 等）以及网关自身控制头（`X-Forward-To` 等）
- **若配置了 `GATEWAY_TOKEN`**：不会把网关鉴权用的 `Authorization` / `X-Gateway-Key` 转给上游
- **Response Header**：过滤 hop-by-hop，并附加 `X-Gateway: gateway-deliver`
- **Redirect**：不自动跟随 3xx，由客户端处理
- **CORS**：`OPTIONS` 预检不要求令牌；若请求带 `Origin`，响应会回写 `Access-Control-Allow-Origin`
- **健康检查**：`GET /health` 与 `GET /_gateway/health` 不要求令牌，方便 ALB / Nginx 探测

## 项目结构

```text
src/
  app.py           # ASGI 入口，读取环境变量并监听端口
  gateway.py       # 鉴权、组请求、转发、回写响应
  settings.py      # 环境变量
  target.py        # 解析转发目标
  headers_util.py  # Header 过滤规则
deploy/
  gateway-deliver.service
```

## 安全建议

1. 生产环境务必设置 `GATEWAY_TOKEN`，避免网关被当成开放代理。
2. 配合 `ALLOWED_HOSTS` 限制可访问的上游域名。
3. 链路本地地址和云厂商元数据地址（如 `169.254.169.254`、`fd00:ec2::254`）会被直接拒绝。
4. 进程只监听 `127.0.0.1`，由 Nginx 或 ALB 对外提供 HTTPS。
