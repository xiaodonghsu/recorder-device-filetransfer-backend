# 嘉环声枢录音文件传输服务（filetransfer）

> 独立的 Python FastAPI 服务：**独立开发、独立部署、独立配置**，不改动、不导入、不共享
> `frontend/` 与 `backend/` 的任何代码、数据库或进程。两个后端只共用同一个 Casdoor 实例做认证。

---

## 1. 定位与边界

| 项目 | 本服务 `filetransfer/` | 现有 `backend/` |
| --- | --- | --- |
| 职责 | 录音文件上传 / 保存 / 列表 / 下载 / 删除 | 设备资产、领用、绑定、凭证、事件 |
| 监听端口（建议） | `127.0.0.1:8030` | `127.0.0.1:8040` |
| 接口前缀 | `/voicehub-ogg` | `/voicehub-api` |
| 配置文件 | `filetransfer/.env` | `backend/.env` |
| 数据存储 | 本地文件系统（按工号分目录） | MySQL / SQLite |
| 依赖 | 仅 `filetransfer/requirements.txt` | 仅 `backend/requirements.txt` |

服务自带 `/voicehub-ogg` 前缀，**nginx 原样透传**（`proxy_pass` 不带 URI 部分），
与 `/voicehub-api` 的部署方式一致。这样同一域名下三个子系统互不干扰：

```
浏览器 ──> https://office.uassist.cn:40000
             ├── /voicehub/       → 管理后台静态资源（frontend/）
             ├── /voicehub-api/   → 设备管理后端 127.0.0.1:8040
             └── /voicehub-ogg/   → 文件传输服务 127.0.0.1:8030
```

---

## 2. 目录结构

```
filetransfer/
├── app/
│   ├── __init__.py
│   ├── config.py       # pydantic-settings 配置中心（只读 filetransfer/.env）
│   ├── errors.py       # 统一异常 / {code,message,status,details} 错误体
│   ├── auth.py         # Casdoor Token 校验 + 身份依赖（工号 / 组织）
│   ├── storage.py      # 按用户名隔离的落盘、路径防护、列目录、删除
│   ├── schemas.py      # 响应模型
│   ├── api.py          # HTTP 路由（不含前缀）
│   └── main.py         # 应用入口、lifespan、早拒绝中间件、CORS、探针
├── tests/              # 离线测试（假 Casdoor 校验器，不联网）
├── requirements.txt
├── .env.example        # 复制为 .env 后修改
├── .gitignore          # 忽略 .env / data/ / __pycache__
├── pytest.ini
├── nginx.conf.example  # 合并进现有 server 块的配置片段
├── Dockerfile
└── README.md
```

运行时会创建 `filetransfer/data/uploads/`（由 `STORAGE_DIR` 决定），已加入 `.gitignore`。

---

## 3. 快速启动（本地开发）

```bash
cd filetransfer
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash；CMD 用 .venv\Scripts\activate.bat
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
uv run -m app.main                   # 默认 http://127.0.0.1:8030
```

在线文档：<http://127.0.0.1:8030/voicehub-ogg/docs>
存活探针：<http://127.0.0.1:8030/healthz>（根路径，内网 / 容器用）
就绪探针：<http://127.0.0.1:8030/readyz>（检查存储目录可写）

生产启动（由 systemd / supervisor 托管）：

```bash
uv run -m uvicorn app.main:app --host 127.0.0.1 --port 8030 --proxy-headers --workers 2
```

> **Windows / Git Bash 陷阱**：不要用 `API_PREFIX=/voicehub-ogg python -m app.main`，
> MSYS 会把 `/voicehub-ogg` 转成 Windows 绝对路径导致接口全 404。把值写进
> `filetransfer/.env`，或先执行 `export MSYS_NO_PATHCONV=1`。配置校验器会拦截这种值并报错。

---

## 4. 配置项

配置优先级：环境变量 > `filetransfer/.env` > 代码默认值。完整注释见 `.env.example`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PREFIX` | `/voicehub-ogg` | 服务自身监听的接口前缀 |
| `DOCS_ENABLED` | `true` | 是否开放 docs / redoc / openapi.json |
| `HOST` / `PORT` | `127.0.0.1` / `8030` | 仅 `python -m app.main` 本地开发使用 |
| `STORAGE_DIR` | `./data/uploads` | 文件根目录，相对路径以 `filetransfer/` 为基准 |
| `MAX_UPLOAD_MB` | `100` | 单文件上限；nginx `client_max_body_size` 必须 ≥ 该值 |
| `ALLOWED_EXTENSIONS` | `.ogg,.oga,.opus,.wav,.mp3,.m4a,.flac,.webm` | 扩展名白名单，`*` 表示不限制 |
| `MAX_SUBDIR_DEPTH` | `3` | `subdir` 最大层数 |
| `OVERWRITE_EXISTING` | `false` | 同名是否覆盖；否则自动追加 `-1` / `-2` |
| `MAX_LIST_ITEMS` | `1000` | 列表接口单用户返回上限 |
| `CASDOOR_BASE_URL` | `https://office.uassist.cn:40000/auth` | 带 `/auth` 前缀 |
| `CASDOOR_CLIENT_ID` | `6932b8ce0b655b9421b5` | 公开标识符，已内置默认值 |
| `CASDOOR_CLIENT_SECRET` | 空 | 机密，仅 `introspect` 模式需要 |
| `CASDOOR_ORGANIZATION` | `bestlink` | 组织名即 tenant_id，与设备管理后端一致 |
| `CASDOOR_VERIFY_METHOD` | `auto` | `auto` / `userinfo` / `introspect` / `jwt` |
| `CASDOOR_REQUIRE_ORGANIZATION` | `true` | 校验 Token 中的组织是否匹配 |
| `CASDOOR_TIMEOUT` / `CASDOOR_VERIFY_TLS` | `10` / `true` | Casdoor HTTP 客户端 |
| `ALLOW_QUERY_TOKEN` | `false` | 是否允许 `?access_token=`（默认关闭，见 §6） |
| `CORS_ORIGINS` | `https://office.uassist.cn:40000` | nginx 同源代理时用不到 CORS |

---

## 5. 接口

除探针与在线文档外，**所有接口都需要** `Authorization: Bearer <Casdoor Access Token>`。
前缀统一为 `/voicehub-ogg`。

| 方法 | 路径 | 说明 | 成功状态 |
| --- | --- | --- | --- |
| `GET` | `/voicehub-ogg/healthz` | 存活探针（可经 nginx 访问，无需令牌） | 200 |
| `GET` | `/voicehub-ogg/me` | 查看当前 Token 解析出的身份（排障用） | 200 |
| `POST` | `/voicehub-ogg/upload` | 上传文件，`multipart/form-data` | 201 |
| `GET` | `/voicehub-ogg/files?subdir=` | 列出当前用户名下的文件 | 200 |
| `GET` | `/voicehub-ogg/files/{relpath}` | 下载自己的文件 | 200 |
| `DELETE` | `/voicehub-ogg/files/{relpath}` | 删除自己的文件 | 200 |
| `GET` | `/healthz`、`/readyz` | 根路径探针（内网 / 容器，无需令牌） | 200 / 503 |

### 5.1 上传

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 文件本体，字段名固定 |
| `filename` | 否 | 自定义落盘文件名；不传用上传时的文件名。**都会做安全净化** |
| `subdir` | 否 | 用户目录内的子目录，如 `2026-11-12/meeting`，最多 3 层 |

```bash
TOKEN="<Casdoor Access Token>"
curl -X POST "https://office.uassist.cn:40000/voicehub-ogg/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@meeting.ogg" \
  -F "subdir=2026-11-12" \
  -F "filename=周会.ogg"
```

响应（201）：

```json
{
  "code": "ok",
  "message": "上传成功",
  "file": {
    "name": "周会.ogg",
    "relpath": "2026-11-12/周会.ogg",
    "owner": "JH000059",
    "size": 1048576,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "content_type": "audio/ogg",
    "modified_at": "2026-11-12T08:01:02Z",
    "download_url": "/voicehub-ogg/files/2026-11-12/%E5%91%A8%E4%BC%9A.ogg"
  }
}
```

> 用户名 `JH000059` 来自 Token，**客户端无法指定**。`download_url` 已做 URL 编码，
> 中文路径可直接拼接使用；`relpath` 用于下载 / 删除。

### 5.2 列表 / 下载 / 删除

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://office.uassist.cn:40000/voicehub-ogg/files?subdir=2026-11-12"

curl -H "Authorization: Bearer $TOKEN" \
  -o meeting.ogg \
  "https://office.uassist.cn:40000/voicehub-ogg/files/2026-11-12/%E5%91%A8%E4%BC%9A.ogg"

curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "https://office.uassist.cn:40000/voicehub-ogg/files/2026-11-12/%E5%91%A8%E4%BC%9A.ogg"
```

错误响应与设备管理后端同构：

```json
{
  "code": "unauthorized",
  "message": "访问令牌无效或已过期",
  "status": 401
}
```

| code | HTTP | 典型原因 |
| --- | --- | --- |
| `unauthorized` | 401 | 缺令牌 / 令牌无效 / 过期 / 账号禁用 |
| `forbidden` | 403 | Token 里的组织与 `CASDOOR_ORGANIZATION` 不一致 |
| `validation_error` | 422 | 参数错误、非法文件名、路径含 `..`、目录层数超限 |
| `payload_too_large` | 413 | 文件超过 `MAX_UPLOAD_MB` |
| `unsupported_media_type` | 415 | 扩展名不在白名单 |
| `not_found` | 404 | 文件不存在 / 不属于当前用户 |
| `storage_error` | 500 | 磁盘写入或删除失败 |
| `casdoor_error` | 502 | Casdoor 不可达或响应异常（**不是**令牌无效） |

---

## 6. 认证与身份口径

沿用设备管理后端已确立的三层标识约定，**姓名不参与身份匹配**：

| 属性 | Casdoor claim | 用途 |
| --- | --- | --- |
| `user_id` | `sub` / `id` | 技术 UUID，仅审计与展示 |
| `username` | `preferred_username` | **工号，即存储目录名** |
| `display_name` | `name` / `displayName` | 姓名，仅展示 |
| `tenant_id` | `owner` / `organization` | 组织，用于越权校验 |

校验方式：

| `CASDOOR_VERIFY_METHOD` | 行为 | 是否需要 clientSecret |
| --- | --- | --- |
| `auto`（默认） | `/api/userinfo` 优先；失败且配了凭据则回退 `introspect` | 否 |
| `userinfo` | 在线换取用户对象，能拿到完整字段 | 否 |
| `introspect` | RFC7662 服务端校验 `active`，最严谨 | 是 |
| `jwt` | JWKS 本地验签（带缓存），无需每次联网 | 否 |

补充约定：

- **组织校验**：`CASDOOR_REQUIRE_ORGANIZATION=true` 时，Token 里的组织必须等于配置值，
  否则 403。若 Casdoor 返回的 claims **不含** `owner`，租户会回退到配置值并在日志里告警，
  `/me` 的 `tenant_source` 会显示 `config_fallback` —— 此时租户边界依赖 Casdoor 应用本身的签发范围，
  生产若要求强隔离，建议改用 `jwt` 模式并确认 claims 含组织信息。
- **早拒绝**：FastAPI 会先解析 multipart 请求体再执行依赖，因此仅靠 `Depends` 无法阻止
  「不带令牌的大文件」把 body 读完。服务在中间件层先检查 `Authorization` 与 `Content-Length`，
  缺失令牌直接 401、明显超限直接 413，不读请求体；真正的校验仍在依赖中完成。
- **令牌不落日志**：令牌只存在于内存与请求头，禁止写入日志、事件或响应体。
  `ALLOW_QUERY_TOKEN` 默认关闭 —— 一旦开启，URL 会被 nginx / 浏览器历史 / access log 记录。
- **`502` 不等于 `401`**：Casdoor 抖动时返回 502，客户端不应误触发重新登录。

---

## 7. 存储布局与安全边界

```
{STORAGE_DIR}/
└── JH000059/                  ← 工号（服务端从 Token 推导）
    ├── 2026-11-12/
    │   └── 周会.ogg
    └── standup.ogg
```

四道防护：

1. **净化**：用户名 / 文件名 / 子目录逐段白名单化，剥离路径分隔符、控制字符、
   `.` / `..`、Windows 保留设备名；文件名只取 basename，`../../x.ogg` 会落到 `x.ogg`。
2. **归属**：所有路径都从当前用户的根目录出发，不接受绝对路径、相对根路径，
   也不接受客户端传来的用户名。
3. **落点校验**：`Path.resolve()` 后必须仍在用户目录内，同时挡住符号链接逃逸。
4. **原子写入**：先写同目录 `.part` 临时文件，按 64 KB 分块流式读取并累计大小
   （超限立即删除临时文件），完成后 `os.replace` 发布，下载方不会读到半截文件。

大小限制有两道：中间件按 `Content-Length` 预拒绝（含 1 MB multipart 开销预算），
存储层按实际读取字节数精确拒绝（覆盖 chunked 传输）。扩展名由服务端按落盘文件名判定，
**不采信客户端的 Content-Type**。

同名文件默认追加 `-1` / `-2` 后缀（`OVERWRITE_EXISTING=true` 时覆盖）。
极高并发下两个同名请求可能先后发布导致后者覆盖前者；如需严格不覆盖，
让客户端在 `filename` 里带唯一 ID（例如录音 UUID）。

---

## 8. nginx 子路由部署

配置片段见 `nginx.conf.example`，合并进现有 `server { listen 40000 ssl; ... }` 块。要点：

| 要点 | 说明 |
| --- | --- |
| `proxy_pass http://127.0.0.1:8030;` | **末尾不能带 URI / 斜杠**，否则前缀被剥离，全部 404 |
| `client_max_body_size 120m` | 必须 ≥ `MAX_UPLOAD_MB` + multipart 开销，否则 nginx 先返回非结构化 413 |
| `proxy_request_buffering off` | 流式转发，避免 nginx 先把大文件完整缓存到磁盘 |
| `proxy_read_timeout / send_timeout 300s` | 大文件上传耗时较长 |
| `uvicorn --proxy-headers` | 让后端正确识别 `X-Forwarded-Proto` / `X-Forwarded-For` |
| `Authorization` 头 | 默认透传；严禁写进 access log |

部署后自检：

```bash
curl https://office.uassist.cn:40000/voicehub-ogg/healthz
curl -H "Authorization: Bearer $TOKEN" https://office.uassist.cn:40000/voicehub-ogg/me
```

---

## 9. Docker（可选）

```bash
docker build -t voicehub-ogg:1.0.0 -f filetransfer/Dockerfile filetransfer
docker run -d --name voicehub-ogg -p 127.0.0.1:8030:8030 \
  -v /srv/voicehub-ogg:/var/lib/voicehub-ogg \
  --env-file filetransfer/.env \
  voicehub-ogg:1.0.0
```

镜像内 `STORAGE_DIR=/var/lib/voicehub-ogg`，务必挂载卷，否则容器重建会丢文件。

---

## 10. 测试

```bash
cd filetransfer
pytest
```

离线运行：测试用假 Casdoor 校验器替换依赖，不联网、不碰真实 `data/`（用 `tmp_path`）。
覆盖：无令牌 / 无效令牌、按工号落盘、文件名与子目录穿越、扩展名白名单、
两级大小限制与临时文件清理、同名不覆盖、列表 / 下载 / 删除的用户隔离、
跨组织拒绝、Casdoor 不可达返回 502、探针与 `/me` 不泄漏令牌。

---

## 11. 已知限制与后续

| 方向 | 现状 | 后续建议 |
| --- | --- | --- |
| 存储后端 | 本地文件系统 | 量大后换 S3 兼容对象存储（README §8 P2 原计划） |
| 断点续传 / 分片 | 未实现 | 需要非常大文件时再引入 tus 或分片合并 |
| 配额 / 清理 | 未实现 | 按工号做容量配额、TTL 归档、软删除与回收站 |
| 审计 | 仅应用日志 | 上传 / 下载 / 删除写独立审计表或日志流 |
| 内容校验 | 只校验扩展名与大小 | 可加 magic number 校验、病毒扫描、服务端加密 |
| 目录大小写 | Windows 大小写不敏感 | Linux 上若 Token 工号大小写不一致会生成两个目录，建议统一大写 |
| 与业务数据关联 | 暂无 | 后续可把 `relpath` / `sha256` 写回设备管理后端做录音归属 |

本服务不实现业务归属判定（哪段录音属于哪台设备 / 哪次会议），
只负责「认证 → 按工号隔离 → 安全保存与取回」这一层。
