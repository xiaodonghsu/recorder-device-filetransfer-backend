# 嘉环声枢录音文件传输服务（filetransfer）

> 独立的 Python FastAPI 服务：**独立开发、独立部署、独立配置**，不改动、不导入、不共享
> `frontend/` 与 `backend/` 的任何代码、数据库或进程。两个后端只共用同一个 Casdoor 实例做认证。
>
> 文件存储支持 **本地磁盘** 与 **华为云 OBS**（官方 Python SDK）两种后端，按
> `租户 / 用户名(工号) / 设备SN` 三级隔离；同一员工名下多台录音卡各占一个设备目录，
> 录音互不混放。每次上传的文件名、设备 SN、对象键、大小、摘要、**标签**等元数据
> 登记在自有 **SQLite / MySQL** 数据库中。标签可用上传表单指定、后续整体覆盖修改，
> 并按标签筛选文件；它只存元数据，不参与目录 / 对象键命名。
>
> **版本约定（2026-11-16 起）**：设备 SN 是强制的一级目录，旧的 `租户/工号/` 两级布局**不做兼容**，
> 不提供迁移、双写或旧路径回退（见 §12.1）。

---

## 1. 定位与边界

| 项目 | 本服务 `filetransfer/` | 现有 `backend/` |
| --- | --- | --- |
| 职责 | 录音文件上传 / 保存 / 列表 / 下载 / 删除 | 设备资产、领用、绑定、凭证、事件 |
| 监听端口（建议） | `127.0.0.1:8030` | `127.0.0.1:8040` |
| 接口前缀 | `/voicehub-ogg` | `/voicehub-api` |
| 配置文件 | `filetransfer/.env` | `backend/.env` |
| 文件存储 | 本地磁盘 或 华为云 OBS（可切换），`租户/工号/设备SN/` 三级 | — |
| 元数据 | 自有 SQLite / MySQL（文件索引） | MySQL / SQLite（业务数据） |
| 依赖 | `filetransfer/pyproject.toml` + `uv.lock` | `backend/requirements.txt` |

服务自带 `/voicehub-ogg` 前缀，**nginx 原样透传**（`proxy_pass` 不带 URI 部分），
与 `/voicehub-api` 的部署方式一致。这样同一域名下三个子系统互不干扰：

```
浏览器 ──> https://office.uassist.cn:40000
             ├── /voicehub/       → 管理后台静态资源（frontend/）
             ├── /voicehub-api/   → 设备管理后端 127.0.0.1:8040
             └── /voicehub-ogg/   → 文件传输服务 127.0.0.1:8030
                                      ├── 本地磁盘 / 华为云 OBS（文件本体）
                                      └── SQLite / MySQL（文件元数据）
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
│   ├── storage.py      # 后端契约、设备 SN / 路径净化、公共流式写入、LocalStorage
│   ├── storage_obs.py  # 华为云 OBS 后端（esdk-obs-python 官方 SDK）
│   ├── tags.py         # 标签解析 / 归一化 / JSON 序列化（与路径无关）
│   ├── db.py           # SQLAlchemy 模型 + FileRepository（SQLite / MySQL，含设备 SN / tags 列）
│   ├── schemas.py      # 响应模型 + 标签修改请求模型
│   ├── api.py          # HTTP 路由（不含前缀），串联存储与元数据
│   └── main.py         # 应用入口、lifespan、早拒绝中间件、CORS、探针
├── tests/              # 离线测试（假 Casdoor 校验器 + 假 OBS 客户端）
│   ├── conftest.py
│   ├── test_api.py
│   ├── test_obs.py
│   └── test_tags.py    # 标签上传 / 修改 / 筛选 / 隔离 / 校验
├── pyproject.toml / uv.lock     # uv 依赖管理（uv sync / uv run）
├── requirements.txt            # pip 部署备选
├── .env.example        # 参数说明；实际 .env 已创建且被忽略
├── .gitignore          # 忽略 .env / data/ / __pycache__ / .venv
├── pytest.ini
├── nginx.conf.example  # 合并进现有 server 块的配置片段
├── Dockerfile
└── README.md
```

运行时会创建 `filetransfer/data/uploads/`（本地后端文件）与 `data/filetransfer.db`
（SQLite 元数据），均已加入 `.gitignore`。

---

## 3. 快速启动（本地开发）

```bash
cd filetransfer
uv sync                              # 按 pyproject.toml / uv.lock 建 .venv
cp .env.example .env                 # 若尚未创建；Windows: copy .env.example .env
uv run -m app.main                   # 默认 http://127.0.0.1:8030
```

> pip 备选：`python -m venv .venv && pip install -r requirements.txt && python -m app.main`

在线文档：<http://127.0.0.1:8030/voicehub-ogg/docs>
存活探针：<http://127.0.0.1:8030/healthz>（根路径，内网 / 容器用）
就绪探针：<http://127.0.0.1:8030/readyz>（同时检查存储后端与数据库）

切换存储后端只需改 `.env` 一行并重启：

```dotenv
STORAGE_BACKEND=obs        # local（默认）| obs
```

`obs` 模式需要 `esdk-obs-python`（已在依赖中）与有效的 AK/SK；
`local` 模式即使没装 OBS SDK 也能运行（SDK 为延迟导入）。

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
| `STORAGE_DIR` | `./data/uploads` | 本地后端文件根目录；OBS 模式仍用于临时文件 |
| `MAX_UPLOAD_MB` | `100` | 单文件上限；nginx `client_max_body_size` 必须 ≥ 该值 |
| `ALLOWED_EXTENSIONS` | `.ogg,.oga,.opus,.wav,.mp3,.m4a,.flac,.webm` | 扩展名白名单，`*` 表示不限制 |
| `MAX_SUBDIR_DEPTH` | `3` | `subdir` 最大层数（位于设备 SN 目录之内） |
| `OVERWRITE_EXISTING` | `false` | 同名是否覆盖；否则自动追加 `-1` / `-2` |
| `MAX_LIST_ITEMS` | `1000` | 列表接口单用户返回上限 |
| `MAX_TAGS_PER_FILE` | `20` | 单文件最多标签数（去重后），超限 422 |
| `MAX_TAG_LENGTH` | `32` | 单个标签最大字符数，超限 422 |
| **`STORAGE_BACKEND`** | `local` | `local` 本地磁盘 / `obs` 华为云 OBS |
| **`OBS_ACCESS_KEY_ID`** | 空 | 华为云 AK（机密，只放 `.env`） |
| **`OBS_SECRET_ACCESS_KEY`** | 空 | 华为云 SK（机密，只放 `.env`） |
| **`OBS_SERVER`** | `obs.cn-south-1.myhuaweicloud.com` | Endpoint；只填域名会自动补 `https://` |
| **`OBS_BUCKET`** | `delocalization-public-files` | 桶名 |
| **`OBS_PREFIX`** | `voicehub/sounds` | 桶内统一前缀 |
| **`OBS_CREATE_FOLDER`** | `true` | 自动创建前缀 / 租户 / 用户 / 设备 SN 四级目录对象 |
| `OBS_MAX_LIST_KEYS` | `1000` | 运维列举对象时的单页上限 |
| **`DATABASE_URL`** | `sqlite:///./data/filetransfer.db` | SQLite；相对路径以 `filetransfer/` 为基准 |
| `DB_ECHO` | `false` | 是否打印 SQL |
| `CASDOOR_BASE_URL` | `https://office.uassist.cn:40000/auth` | 带 `/auth` 前缀 |
| `CASDOOR_CLIENT_ID` | `6932b8ce0b655b9421b5` | 公开标识符，已内置默认值 |
| `CASDOOR_CLIENT_SECRET` | 空 | 机密，仅 `introspect` 模式需要 |
| `CASDOOR_ORGANIZATION` | `bestlink` | 组织名即 tenant_id，与设备管理后端一致 |
| `CASDOOR_VERIFY_METHOD` | `auto` | `auto` / `userinfo` / `introspect` / `jwt` |
| `CASDOOR_REQUIRE_ORGANIZATION` | `true` | 校验 Token 中的组织是否匹配 |
| `CASDOOR_TIMEOUT` / `CASDOOR_VERIFY_TLS` | `10` / `true` | Casdoor HTTP 客户端 |
| `ALLOW_QUERY_TOKEN` | `false` | 是否允许 `?access_token=`（默认关闭，见 §6） |
| `CORS_ORIGINS` | `https://office.uassist.cn:40000` | nginx 同源代理时用不到 CORS |

MySQL 示例（多实例 / 生产推荐）：

```dotenv
DATABASE_URL=mysql+pymysql://user:password@127.0.0.1:3306/voicehub_files?charset=utf8mb4
```

启动时用 `Base.metadata.create_all()` 幂等建表；如需版本化迁移可接入 Alembic。

### 4.1 设备 SN 分层（无配置项）

设备 SN **不是配置项**，而是每次上传必填的表单字段 `device_sn`，由 APP 从蓝牙读到的
录音卡序列号提供，与设备管理后端 `device.sn` 同一口径（如 `VH-2026-0001`，≤ 64 字符）。

| 维度 | 取值来源 | 能否被客户端伪造 |
| --- | --- | --- |
| 租户 `tenant_id` | Casdoor Token 的 `owner` | 否，服务端推导 |
| 用户名 `user_id` | Casdoor Token 的 `preferred_username`（工号） | 否，服务端推导 |
| 设备 `device_sn` | 上传表单 `device_sn`，严格净化 | 仅能指定**自己名下**的目录名 |

净化规则：只允许 `A-Za-z0-9._-`、首字符必须是字母或数字、长度 ≤ 64、
大小写保持原样；出现 `/`、`..`、控制字符、中文等一律 422 `validation_error`，
**不做猜测性替换**。这样目录名可预测，`relpath` 首段可以安全地用于反查。

> 本服务与设备管理后端完全独立，**不校验该 SN 是否真的领用给当前工号**。
> SN 在本服务里只是「自己名下的一层目录标签」，跨用户隔离由 Token 保证。
> 若后续要求强归属校验，需让 APP 先过 `/my/devices` 或由本服务调用业务后端。

---

## 5. 接口

除探针与在线文档外，**所有接口都需要** `Authorization: Bearer <Casdoor Access Token>`。
前缀统一为 `/voicehub-ogg`。

| 方法 | 路径 | 说明 | 成功状态 |
| --- | --- | --- | --- |
| `GET` | `/voicehub-ogg/healthz` | 存活探针（可经 nginx 访问，无需令牌） | 200 |
| `GET` | `/voicehub-ogg/me` | 查看当前 Token 解析出的身份（排障用） | 200 |
| `POST` | `/voicehub-ogg/upload` | 上传文件，`multipart/form-data`，**必填 `device_sn`**，可选 `tags` | 201 |
| `GET` | `/voicehub-ogg/files?device_sn=&subdir=&tag=` | 从**元数据数据库**列出当前租户/工号的文件，可按设备 / 子目录 / 标签叠加筛选 | 200 |
| `PUT` | `/voicehub-ogg/files/{设备SN}/{子目录}/{文件名}/tags` | **整体覆盖**修改文件标签（传空数组清空），只改元数据 | 200 |
| `GET` | `/voicehub-ogg/files/{设备SN}/{子目录}/{文件名}` | 下载自己的文件 | 200 |
| `DELETE` | `/voicehub-ogg/files/{设备SN}/{子目录}/{文件名}` | 删除自己的文件 + 数据库软删除 | 200 |
| `GET` | `/healthz`、`/readyz` | 根路径探针（内网 / 容器，无需令牌） | 200 / 503 |

### 5.1 上传

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 文件本体，字段名固定 |
| **`device_sn`** | **是** | **录音卡设备 SN**（如 `VH-2026-0001`），用户目录下的强制一级目录；缺失或格式非法返回 422 |
| `filename` | 否 | 自定义落盘文件名；不传用上传时的文件名。**都会做安全净化** |
| `subdir` | 否 | **设备目录内**的子目录，如 `2026-11-12/meeting`，最多 3 层；不传则文件直接落在设备目录下 |
| `tags` | 否 | **文件标签**，多个用逗号分隔（`,` 与 `，` 均可），如 `会议,重要,客户A`；空项与重复项自动忽略，超限 422 |

标签只是元数据，**不参与目录 / 对象键 / `relpath` 命名**；不传则 `tags: []`。
去重后超过 `MAX_TAGS_PER_FILE`、单个超过 `MAX_TAG_LENGTH`、含控制字符一律 422，
且在写盘之前校验 —— 不会出现「文件已落盘但请求失败」。覆盖同名文件时新记录以本次
上传的标签为准，不继承旧记录。

```bash
TOKEN="<Casdoor Access Token>"
curl -X POST "https://office.uassist.cn:40000/voicehub-ogg/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@meeting.ogg" \
  -F "device_sn=VH-2026-0001" \
  -F "subdir=2026-11-12" \
  -F "filename=周会.ogg" \
  -F "tags=会议,重要，客户A"
```

响应（201，`STORAGE_BACKEND=obs` 时的示例）：

```json
{
  "code": "ok",
  "message": "上传成功",
  "file": {
    "name": "周会.ogg",
    "relpath": "VH-2026-0001/2026-11-12/周会.ogg",
    "device_sn": "VH-2026-0001",
    "owner": "JH000059",
    "size": 1048576,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "content_type": "audio/ogg",
    "modified_at": "2026-11-12T08:01:02Z",
    "download_url": "/voicehub-ogg/files/VH-2026-0001/2026-11-12/%E5%91%A8%E4%BC%9A.ogg",
    "tenant_id": "bestlink",
    "backend": "obs",
    "object_key": "voicehub/sounds/bestlink/JH000059/VH-2026-0001/2026-11-12/周会.ogg",
    "bucket": "delocalization-public-files",
    "record_id": 42,
    "uploaded_at": "2026-11-12T08:01:02Z",
    "tags": ["会议", "重要", "客户A"]
  }
}
```

`local` 后端下 `object_key` / `bucket` 为 `null`。
租户 `bestlink` 与用户名 `JH000059` 都来自 Token，**客户端无法指定**；
`device_sn` 由客户端声明并严格净化，只能落到**自己名下**的设备目录。

`relpath` 是相对用户根目录的完整路径，**首段恒为设备 SN**，下载 / 删除直接用它，
不需要再传设备参数。`download_url` 已做 URL 编码，中文路径可直接拼接。
同一工号换一台设备上传时只需改 `device_sn`，文件自然进入另一个目录。

### 5.2 列表 / 下载 / 删除

```bash
# 列出该工号名下所有设备（多设备时一次拿全）
curl -H "Authorization: Bearer $TOKEN" \
  "https://office.uassist.cn:40000/voicehub-ogg/files"

# 只列某台设备、某天子目录
curl -H "Authorization: Bearer $TOKEN" \
  "https://office.uassist.cn:40000/voicehub-ogg/files?device_sn=VH-2026-0001&subdir=2026-11-12"

# 下载 / 删除：relpath 首段即设备 SN，URL 自带完整定位信息
curl -H "Authorization: Bearer $TOKEN" -o meeting.ogg \
  "https://office.uassist.cn:40000/voicehub-ogg/files/VH-2026-0001/2026-11-12/%E5%91%A8%E4%BC%9A.ogg"

curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "https://office.uassist.cn:40000/voicehub-ogg/files/VH-2026-0001/2026-11-12/%E5%91%A8%E4%BC%9A.ogg"
```

`device_sn` 缺省时列表返回该用户所有设备的文件；`subdir` 是**设备目录内**的子目录前缀，
对多台设备同时生效。下载 / 删除不接受额外设备参数，路径里换成另一台设备的 SN
只能命中该用户自己名下那台设备的目录，跨用户一律 404。

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
| `validation_error` | 422 | 参数错误、缺 `device_sn`、设备 SN 格式非法、`relpath` 缺设备 SN 段、非法文件名、路径含 `..`、目录层数超限 |
| `payload_too_large` | 413 | 文件超过 `MAX_UPLOAD_MB` |
| `unsupported_media_type` | 415 | 扩展名不在白名单 |
| `not_found` | 404 | 文件不存在 / 不属于当前用户 / 设备目录下无该文件 |
| `storage_error` | 500 | 本地磁盘写入失败 / OBS 配置缺失 |
| `remote_storage_error` | 502 | 华为云 OBS 调用失败（权限、网络、桶不存在） |
| `casdoor_error` | 502 | Casdoor 不可达或响应异常（**不是**令牌无效） |

### 5.3 标签：修改与筛选

**修改标签**用 `PUT`，请求体是**完整标签列表（整体覆盖）**，不是增量追加；
传 `[]` 即清空。路径与下载 / 删除同一口径，首段必须是设备 SN：

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tags": ["会议", "重要"]}' \
  "https://office.uassist.cn:40000/voicehub-ogg/files/VH-2026-0001/2026-11-12/%E5%91%A8%E4%BC%9A.ogg/tags"
```

响应 200，返回更新后的完整 `FileInfo`（`relpath` / `download_url` / `object_key` /
`record_id` 均不变）：

```json
{
  "name": "周会.ogg",
  "relpath": "VH-2026-0001/2026-11-12/周会.ogg",
  "tags": ["会议", "重要"],
  "record_id": 42
}
```

要点：

- **只改元数据**：不调用存储后端、不搬文件、不改对象键，因此本地 / OBS 两种后端行为一致；
- **隔离**：租户、工号、设备 SN、后端、未删除五条件同时命中才更新；别人的文件、
  别的设备、已删除记录、不存在的路径一律 404（不泄漏存在性）；
- **校验**：与上传完全一致（去空白、丢空项、去重、数量 / 长度 / 控制字符），非法返回 422；
- **筛选**：`GET /files?tag=会议` 只列含该标签的文件，可与 `device_sn`、`subdir` 叠加。
  只接受单个标签，逗号分隔多值返回 422；响应体 `tag` 字段回显本次筛选条件。

| code | HTTP | 标签相关原因 |
| --- | --- | --- |
| `validation_error` | 422 | 标签超数量 / 超长 / 含控制字符；`tag` 筛选传了多个值 |
| `not_found` | 404 | 修改标签时文件不存在、已删除或不属于当前用户 / 设备 |

---

## 6. 认证与身份口径

沿用设备管理后端已确立的三层标识约定，**姓名不参与身份匹配**：

| 属性 | Casdoor claim | 用途 |
| --- | --- | --- |
| `user_id` | `sub` / `id` | 技术 UUID，仅审计与展示 |
| `username` | `preferred_username` | **工号，即存储路径里的 `user_id` 段** |
| `display_name` | `name` / `displayName` | 姓名，仅展示 |
| `tenant_id` | `owner` / `organization` | 组织，即存储路径里的 `tenant_id` 段 |
| `device_sn` | 上传表单 `device_sn`（APP 从蓝牙读取） | **设备 SN，即存储路径里的第三段** |

> 这里的 `user_id` 指业务侧工号，不是 Casdoor 的技术 UUID：与设备管理后端
> `device_assignment.user_id` 保持同一口径，便于后续把录音关联到领用记录。
> `device_sn` 则与设备管理后端 `device.sn` 同一口径；姓名与技术 UUID 都不参与
> 目录命名，设备层用 SN 而不是 `device_id`，因为 APP 蓝牙侧拿到的就是 SN。

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
- **令牌与 AK/SK 不落日志**：令牌只存在于内存与请求头；OBS 凭证只在 SDK 客户端里，
  日志里的数据库 URL 已隐藏密码。严禁把 `Authorization` 写进 access log。
  `ALLOW_QUERY_TOKEN` 默认关闭 —— 一旦开启，URL 会被 nginx / 浏览器历史记录。
- **`502` 不等于 `401`**：Casdoor / OBS 抖动时返回 502，客户端不应误触发重新登录。

---

## 7. 存储后端：本地磁盘 / 华为云 OBS

两种后端共用同一套归属口径（租户 + 工号由服务端从 Token 推导，设备 SN 由上传表单提供并严格净化）：

```
本地磁盘：{STORAGE_DIR}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}
华为云 OBS：obs://{OBS_BUCKET}/{OBS_PREFIX}/{tenant_id}/{user_id}/{device_sn}/{subdir}/{filename}

例：obs://delocalization-public-files/voicehub/sounds/bestlink/JH000059/VH-2026-0001/2026-11-12/周会.ogg
```

### 7.1 本地磁盘（`STORAGE_BACKEND=local`）

```
{STORAGE_DIR}/
└── bestlink/
    └── JH000059/
        ├── VH-2026-0001/            ← 录音卡 A
        │   ├── 2026-11-12/
        │   │   └── 周会.ogg
        │   └── standup.ogg
        └── VH-2026-0002/            ← 同一员工名下的录音卡 B
            └── standup.ogg
```

设备目录由 `LocalStorage.device_root()` 统一构造，保存 / 下载 / 删除都从它出发；
`user_root()` 只负责 `租户/工号` 这一层，不再直接承载文件。

上传先把 multipart 流分块写入**同目录** `.part` 临时文件，完成后 `os.replace`
原子发布（同卷保证 Windows 上不会跨盘 rename 失败），下载方不会读到半截文件。

### 7.2 华为云 OBS（`STORAGE_BACKEND=obs`）

使用华为云官方 SDK `esdk-obs-python`（`from obs import ObsClient`），
不使用 S3 兼容接口或第三方封装。用到的官方方法：

| SDK 方法 | 用途 |
| --- | --- |
| `ObsClient(access_key_id, secret_access_key, server)` | 初始化（`server` 自动补 `https://`） |
| `putObject(bucket, key, b"")` | 创建以 `/` 结尾的 0 字节「目录对象」 |
| `headObject` | 同名检查，避免覆盖 |
| `putFile(bucket, key, file_path)` | 上传临时文件（单次 ≤ 5 GB，满足录音场景） |
| `getObject(..., downloadPath=...)` | 下载到临时文件，再由 `FileResponse` 返回并后台清理 |
| `deleteObject` / `listObjects` | 删除、运维列举 / 就绪探针 |

**不存在的租户、用户、设备会自动创建路径**：OBS 是扁平对象存储、没有真实目录，
开启 `OBS_CREATE_FOLDER=true` 后，首次上传会幂等写入四级目录对象：

```
voicehub/sounds/
voicehub/sounds/bestlink/
voicehub/sounds/bestlink/JH000059/
voicehub/sounds/bestlink/JH000059/VH-2026-0001/
```

目录对象键包含设备 SN，因此同一工号下每台设备首次上传各写一次；进程内按完整键
缓存，避免重复写，多实例各自写一次也是幂等的。运维接口 `list_object_keys()`
不传 `device_sn` 时列该用户所有设备，传了则只看该设备前缀。

SDK 是同步阻塞客户端，所有网络调用都放在 `asyncio.to_thread` 线程池里，
不阻塞 FastAPI 事件循环。上传失败返回 502 且**不写数据库**，避免产生「有记录无文件」的脏数据。

> 已在生产桶做过一次只读连通检查与一次「上传 → 列举 → 下载 → 删除」闭环自检，
> HTTP 200/204 全部通过，自检对象已删除，未留下残留。

### 7.3 安全边界（两种后端共用）

1. **净化**：租户 / 用户名 / 设备 SN / 文件名 / 子目录逐段白名单化，剥离路径分隔符、
   控制字符、`.` / `..`、Windows 保留设备名；文件名只取 basename，`../../x.ogg` 会落到
   `x.ogg`；设备 SN 只允许 `A-Za-z0-9._-`，非法字符**直接拒绝**而不是替换。
2. **归属**：所有路径都从当前用户的根目录出发，不接受绝对路径、相对根路径，
   也不接受客户端传来的租户或用户名。设备 SN 由客户端声明，但只能成为
   **该用户目录下**的一层，无法跨到别人的目录。
3. **落点校验**：`Path.resolve()` 后必须仍在**设备根目录**内，同时挡住符号链接逃逸；
   OBS 则由 `租户/工号/设备SN` 前缀 + `parse_relpath` 双重约束。
   下载 / 删除的 `relpath` 若缺设备 SN 段，直接 422，不会回退到用户根目录。
4. **原子写入 / 临时文件清理**：本地用 `.part` + `os.replace`；OBS 上传临时文件在
   `finally` 中删除，下载临时文件在响应后台任务中删除。

大小限制有两道：中间件按 `Content-Length` 预拒绝（含 1 MB multipart 开销预算），
存储层按实际读取字节数精确拒绝（覆盖 chunked 传输）。扩展名由服务端按落盘文件名判定，
**不采信客户端的 Content-Type**。

同名文件默认追加 `-1` / `-2` 后缀（`OVERWRITE_EXISTING=true` 时覆盖）。
极高并发下两个同名请求可能先后发布导致后者覆盖前者；如需严格不覆盖，
让客户端在 `filename` 里带唯一 ID（例如录音 UUID）。

---

## 8. 元数据数据库

为便于后续 ASR、会议纪要、知识库等场景复用文件，两种后端都把每次成功上传登记到
本地数据库。后续业务只需查表即可定位文件，不必扫描磁盘或遍历 OBS 前缀。

表 `ft_file_record`（SQLAlchemy 2.0，SQLite / MySQL 同一套模型）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | 记录 ID，随上传响应返回 `record_id` |
| `tenant_id` / `username` | VARCHAR(128) | 租户 / 工号，所有权过滤依据 |
| `device_sn` | VARCHAR(64) | **录音卡 SN**，设备级过滤依据（有独立索引） |
| `filename` | VARCHAR(255) | **用户上传的文件名**（净化后） |
| `relpath` | VARCHAR(512) | 相对用户根目录的路径，**首段为设备 SN**，下载 / 删除用 |
| `object_key` | VARCHAR(768) | OBS 完整对象键；本地后端为 NULL |
| `backend` / `bucket` | VARCHAR | `local` / `obs`、OBS 桶名 |
| `size_bytes` / `sha256` | BIGINT / VARCHAR(64) | 大小与内容摘要 |
| `content_type` | VARCHAR(128) | 按扩展名推断的 MIME |
| `tags` | TEXT | **文件标签**，JSON 数组文本（如 `["会议","重要"]`）；空为 `[]`，不参与路径 |
| `status` / `created_at` / `deleted_at` | — | 软删除状态与时间（naive UTC） |

行为约定：

- **上传**：后端保存成功 → 写一条 `active` 记录（含 `device_sn` 与 `tags`）；覆盖同名时先把
  **同一设备下**的旧记录标记为 `deleted` 保留历史。
- **列表**：`GET /files` 直接读数据库，按 `(tenant_id, username, backend, status=active)`
  过滤，带 `device_sn` 时再叠加设备条件，因此切换后端不会串出另一后端的文件、
  也不会串出别的设备；`subdir` 按**设备内相对路径**用内存前缀过滤，`tag` 按 JSON
  数组内容内存过滤（多取一批再截断），避免 LIKE 通配符歧义。
- **修改标签**：`PUT /files/{relpath}/tags` 命中唯一 active 记录后整体覆盖 `tags` 列，
  不产生新记录、不改 `relpath`，也不触碰存储后端。
- **删除**：删除后端对象 + 数据库软删除（含设备 SN 条件），历史可审计（标签随记录保留）。
- **索引**：`ix_ft_file_owner(tenant_id, username, device_sn, backend, status)`。
- **就绪**：`/readyz` 同时执行存储后端探针与 `SELECT 1`。
- **连接串**：SQLite 相对路径自动锚定到 `filetransfer/`，不受启动 cwd 影响；
  MySQL URL 原样交给 SQLAlchemy / PyMySQL，日志里隐藏密码。

---

## 9. nginx 子路由部署

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

## 10. Docker（可选）

```bash
docker build -t voicehub-ogg:1.0.0 -f filetransfer/Dockerfile filetransfer
docker run -d --name voicehub-ogg -p 127.0.0.1:8030:8030 \
  -v /srv/voicehub-ogg:/var/lib/voicehub-ogg \
  --env-file filetransfer/.env \
  voicehub-ogg:1.0.0
```

镜像内 `STORAGE_DIR=/var/lib/voicehub-ogg`、SQLite 库也在该卷下，务必挂载卷。
`STORAGE_BACKEND=obs` 时把 AK/SK 通过 `--env-file` 或密钥管理注入，不要写进镜像。

---

## 11. 测试

```bash
cd filetransfer
uv run python -m pytest        # 或：pytest
```

**71 passed**。离线运行：假 Casdoor 校验器 + 假 OBS 客户端 + `tmp_path` 独立 SQLite，
不联网、不碰生产桶、不写真实 `data/`。

覆盖：无令牌 / 无效令牌、按 `租户/工号/设备SN` 落盘、设备 SN 必填与 10 类非法值、
同一工号多台设备隔离与按设备筛选、跨设备下载 404、`relpath` 缺设备段 422、
文件名与子目录穿越、扩展名白名单、两级大小限制与临时文件清理、同名不覆盖、
列表 / 下载 / 删除的用户隔离、跨组织拒绝、Casdoor 不可达 502；
OBS 对象键规则、四级目录对象创建与按设备缓存、假 SDK 上传/下载/删除闭环、
远端失败 502 且不入库、数据库 `device_sn` 列 / 软删除 / 后端隔离、
SQLite 与 MySQL URL 解析、OpenAPI 中 `device_sn` 为必填字段；
标签上传（中英文逗号 / 去重 / 去空）、无标签默认空数组、写盘前 422 校验、
整体覆盖与清空、改标签不动对象 / 文件、跨用户 / 跨设备 / 已删除 404、
请求体校验、按标签筛选与叠加设备、标签隔离、OBS 后端标签不产生额外对象操作、
OpenAPI 标签契约。

---

## 12. 已知限制与后续

| 方向 | 现状 | 后续建议 |
| --- | --- | --- |
| 存储后端 | 本地磁盘 / 华为云 OBS | 已满足 README §8 P2 的对象存储诉求；如需多云可再加抽象 |
| 断点续传 / 分片 | 单文件一次上传（OBS 5 GB 上限） | 超大文件可改用 SDK 的 `uploadFile` 分段接口 |
| 配额 / 清理 | 数据库有软删除记录 | 按工号做容量配额、TTL 归档、回收站与物理清理任务 |
| 审计 | 应用日志 + 上传/删除记录 | 下载行为写独立审计表或日志流 |
| 内容校验 | 只校验扩展名与大小 | 可加 magic number 校验、病毒扫描、服务端加密 |
| 目录大小写 | Windows 大小写不敏感 | Linux 上若 Token 工号大小写不一致会生成两个目录，建议统一大写 |
| 与业务数据关联 | 记录含 `tenant_id` / `username` / `device_sn` / `sha256` / `tags` | 后续可把 `object_key` 写回设备管理后端做录音归属，或用标签驱动 ASR 分流 |
| 标签筛选 | 标签存 JSON 文本，筛选在内存做（多取一批再截断） | 数据量上来后若需高频 / 多标签组合检索，改独立 `ft_file_tag` 关联表 + 索引 |
| 数据库迁移 | 启动时 `create_all()` 幂等建表 | 表结构演进建议接入 Alembic（本次升级需重建表，见 §12.1） |
| 匿名枚举风险 | `CASDOOR_REQUIRE_ORGANIZATION` 校验组织 | Token 不含 `owner` 时会回退配置值并在日志告警，见 §6 |
| 设备 SN 归属 | 只做格式净化，**不校验**该 SN 是否领用给当前工号 | 需要强归属时由 APP 先过 `/my/devices`，或本服务调用业务后端核验 |
| 设备 SN 大小写 | 保持客户端原样，不做大写归一 | Linux 上大小写不一致会生成两个设备目录，APP 侧统一用设备管理后端返回的 `sn` |
| 旧布局兼容 | **无**：不迁移、不双写、不回退 | 见 §12.1 |

本服务不实现业务归属判定（哪台设备真的属于谁 / 哪段录音属于哪次会议），
只负责「认证 → 按 `租户/工号/设备SN` 隔离 → 安全保存与取回 → 登记元数据」这一层。

### 12.1 破坏性升级说明（不做兼容）

本次把目录级别从 `租户/工号/` 改为 `租户/工号/设备SN/`，属于**破坏性变更**：

1. **`relpath` 语义变了**：首段新增设备 SN。所有下载 / 删除 URL 必须改为
   `/voicehub-ogg/files/{设备SN}/{...}`，旧 `relpath` 会返回 422（缺设备段）。
2. **上传必填 `device_sn`**：APP / 脚本需补表单字段，否则 422 `validation_error`。
3. **数据库表新增 `device_sn` 列**：`create_all()` 不会给已存在的表加列。
   本地 / 测试环境直接删掉 `data/filetransfer.db` 重建；生产 MySQL 需手工
   `ALTER TABLE ft_file_record ADD COLUMN device_sn VARCHAR(64) NOT NULL DEFAULT '' ...`
   并重建 `ix_ft_file_owner` 索引，或按新模型重建表后重新登记历史文件。
4. **已落盘的历史文件**：旧目录 `租户/工号/<文件>` 与旧 OBS 对象键不会被自动搬迁，
   也不会出现在新列表接口里（列表只读数据库且按设备过滤）。需要保留时必须手工
   搬目录 / 拷对象并按 `设备SN/文件名` 重新登记元数据。

> 上述 3、4 均为一次性运维动作，代码里**不保留**任何兼容分支。

### 12.2 新增标签列的升级说明（不做自动迁移）

标签功能给 `ft_file_record` 增加了 `tags TEXT NOT NULL DEFAULT '[]'` 列。
`Base.metadata.create_all()` **不会给已存在的表加列**，因此已有库需手工执行一次：

```sql
-- SQLite / MySQL 通用
ALTER TABLE ft_file_record ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
```

旧记录的标签读作空数组 `[]`（`decode_tags` 对空串 / 脏数据也退化为 `[]`，不会 500），
不影响下载 / 删除 / 列表。不需要重建目录或搬对象，历史 OBS 对象键与本地文件路径均不变。

> 若选择干脆重建库表，则需按 §12.1 第 4 条重新登记历史文件；代码里不保留迁移分支。
