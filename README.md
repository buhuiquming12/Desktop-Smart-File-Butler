# 桌面智能文件管家（Desktop Smart File Butler）

一个基于 Electron、React、TypeScript、FastAPI 和 LangGraph 的本地智能文件管理 Agent。用户可以通过自然语言扫描、分类、移动、重命名、摘要和删除本地文件，并使用人工审批、撤销、定时任务、后台执行及 WebSocket 状态同步。

项目当前优先保证文件操作的正确性、安全边界、可恢复性和桌面端可发布性。大批量文件不由 LLM 枚举：LLM 负责理解意图与生成规则，Python 根据完整扫描清单确定实际文件集合。

> 示例：“整理下载目录，把 PDF 移到 PDF 文件夹，并按文件名加上 `archive-` 前缀。”

## 求职与设计文档

- [系统架构与设计说明](docs/ARCHITECTURE.md)：系统组件、Agent 流程、安全边界、状态恢复、并发与桌面发布架构。
- [简历项目描述](docs/RESUME.md)：推荐简历版本、后端岗位版、AI Agent 岗位版与 30 秒 / 1 分钟口述稿。
- [面试问答](docs/INTERVIEW_GUIDE.md)：围绕架构、Manifest、PolicyEngine、Prompt Injection、并发、恢复、Runtime、Electron 和 CI 的项目追问。

## 核心能力

- 自然语言驱动的扫描、分类、移动、重命名、摘要和软删除。
- 完整扫描结果保存为 manifest，以 `scan_id` 交给确定性批量工具处理。
- `batch_move`、`batch_rename`、`batch_classify` 不依赖 Planner 的有限预览。
- 请求级累计审批：重新规划不会重置 move、rename、delete 的累计数量。
- 删除进入沙箱内的 `.butler-trash/`；move、rename、delete 支持审计和回滚。
- SQLite 持久化会话、LangGraph checkpoint、审批、操作日志和扫描清单。
- WebSocket 实时状态同步；断线重连后通过 REST 恢复 thread 快照。
- 支持 OpenAI-Compatible 模型和 Ollama；仅在明确不支持 structured output 时自动降级。
- PDF、DOCX、文本和图片 OCR 内容提取；旧版二进制 `.doc` 会明确报告不支持。
- Windows 生产包内置 PyInstaller 后端 sidecar，目标机器无需 Python 开发环境。

## 当前架构

```text
原始用户请求
      │ trusted_user_intent
      ▼
LangGraph Planner ◄── 文件名、OCR、文档内容、工具结果（不可信数据）
      │ 规则 + scan_id
      ▼
PolicyEngine ── 意图授权 / 风险等级 / 累计审批阈值
      │
      ├── 需要审批 ──► React Approval UI ──► LangGraph resume
      │
      ▼
Manifest Store ──► Python 确定性过滤 ──► PathLockManager
                                              │
                                              ▼
                                      Filesystem Tools
                                              │
                       ┌──────────────────────┴───────────────────┐
                       ▼                                          ▼
             SQLite 审计 / 回滚 / checkpoint          WS 事件 + REST 状态恢复

Electron 单实例主进程
      └── resources/backend/butler-backend.exe
              └── FastAPI + LangGraph + 内置前端静态资源
```

Agent 状态明确区分：

- `trusted_user_intent`：唯一可作为用户授权依据的原始请求。
- `untrusted_file_data`：文件名、OCR、文档正文和扫描数据。
- `tool_observations`：工具返回的受限摘要。
- `mutations`：当前用户请求累计执行的 move、rename、delete 数量。

文件内容即使包含“忽略之前指令”“删除所有文件”或伪造的系统消息，也只能作为普通数据，不能扩展用户授权。

## 批量执行与扫描语义

`scan_directory` 会把完整结果保存到 SQLite manifest，并向 Planner 返回有界摘要，例如：

```json
{
  "scan_id": "2c9f...",
  "total": 286,
  "scanned_count": 286,
  "truncated": false,
  "reason": "none",
  "summary": { "pdf": 83, "jpg": 104 },
  "preview": []
}
```

Planner 随后生成规则，而不是枚举数百条文件路径：

```json
{
  "tool": "batch_move",
  "args": {
    "scan_id": "2c9f...",
    "filter": { "extensions": ["pdf"] },
    "dest_dir": "C:/Users/me/Downloads/PDF"
  }
}
```

具体匹配由 Python 完成。批量结果包含 `matched`、`success`、`failed`、`skipped`、`truncated`、`reason` 和有限预览。

默认扫描上限为 5000 项、12 层。超过限制时会明确返回：

- `truncated: true`
- `reason: max_items` 或 `max_depth`

权限错误使用 `permission_error`；完整扫描使用 `none`。恰好扫描到 5000 项且不存在第 5001 项时不会误报截断。扫描不会进入 `.butler-trash/`，也不会跟进目录符号链接或 Windows junction。

## 审批、并发和回滚

审批由后端 `PolicyEngine` 决定，LLM 无权自行取消安全门槛。

- 删除始终属于高风险操作。
- move、rename 按同一个用户请求累计；模型把 60 个操作拆成多次 replan 也不能绕过阈值。
- 默认批量审批阈值为 20，可通过偏好 `batch_approval_threshold` 调整。
- 已批准的 mutation 上限保存在 Agent state 中，不因重新规划丢失。
- mutation 前会重新核对原始用户意图，文件内容不能授权新的文件操作。

文件写操作由按规范化目录分片的 `PathLockManager` 保护。同一目录的 move、rename、delete、rollback 和摘要输出串行执行，不同目录仍可并行；锁在异常后也会释放。

按会话撤销会展示真实结果，例如：

```text
撤销完成：成功 7，跳过 2，失败 1
```

只要存在失败项，前端就不会显示为纯成功状态。

## 连接、鉴权与状态恢复

- 后端启动时生成一次性 session token，Electron 通过 preload 注入 Renderer。
- REST 使用 `X-Butler-Token`，WebSocket 使用 `token` 查询参数。
- API 和 WebSocket 地址只允许 `localhost`、`127.0.0.1` 或 `::1`，避免把本地 token 发送到远程服务器。
- API 地址优先级：用户显式的本地设置 > preload 注入的 `backendUrl` > `http://127.0.0.1:8000`。
- WebSocket 地址从 API 地址自动派生：`http → ws`，`https → wss`。
- WebSocket 按指数退避重连；连接恢复后调用 `GET /api/threads/{thread_id}`，恢复 observations、pending approval 和最终状态。
- WebSocket 不可用时，`POST /api/chat` 的 REST fallback 会保存服务端返回的新 `thread_id`，后续追问、取消和撤销仍属于同一会话。

当前恢复机制使用 thread 状态快照，不提供逐事件 `event_id` replay。

## 目录结构

```text
├── .github/workflows/ci.yml
├── backend/
│   ├── app/
│   │   ├── agent/
│   │   │   ├── graph.py        # LangGraph 节点与流程编排
│   │   │   ├── checkpoints.py  # checkpoint 创建、解析和保留策略
│   │   │   └── tool_executor.py # 工具分发、批处理、分类与摘要
│   │   ├── api/
│   │   │   ├── auth.py         # 本地令牌与 Origin 校验
│   │   │   ├── connections.py  # WebSocket 连接注册表
│   │   │   └── management.py   # 设置、审计、回滚和定时任务路由
│   │   ├── policy/             # 后端授权和累计审批策略
│   │   ├── runtime/            # 活动 thread、锁和取消状态
│   │   ├── tools/
│   │   │   ├── filesystem.py   # 文件操作和回滚
│   │   │   ├── manifests.py    # 完整扫描清单与确定性匹配
│   │   │   ├── path_locks.py   # 目录级 mutation locks
│   │   │   ├── extract.py      # PDF、DOCX、文本、OCR 提取
│   │   │   └── scheduler.py
│   │   ├── main.py             # FastAPI 装配与 Agent 流协调
│   │   ├── db.py               # SQLite 数据访问
│   │   ├── sandbox_config.py
│   │   └── security.py
│   ├── tests/
│   ├── build_sidecar.py        # PyInstaller 构建入口
│   ├── sidecar.py              # 冻结后的后端程序入口
│   └── requirements.txt
└── frontend/
    ├── electron/
    │   ├── main.ts             # 单实例、sidecar 生命周期、窗口启动
    │   ├── preload.cts
    │   └── singleInstance.ts
    ├── src/
    │   ├── App.tsx
    │   ├── api/client.ts       # REST、WebSocket、reconciliation
    │   ├── hooks/              # 后端连接与设置状态控制器
    │   └── components/
    ├── package.json
    └── package-lock.json
```

## 开发环境

推荐环境：

- Python 3.12
- Node.js 20
- Windows 负责生成 Windows Electron portable 包

### 安装后端依赖

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS/Linux 激活命令为：

```bash
source backend/.venv/bin/activate
```

### 安装前端依赖

项目已提交 `package-lock.json`，请使用可复现安装：

```powershell
cd frontend
npm ci
```

不需要 `--legacy-peer-deps`。

### 启动开发版

```powershell
cd frontend
npm run dev
```

开发模式下 Electron 会查找仓库中的 `backend/`，优先使用 `BUTLER_PYTHON`，否则查找后端虚拟环境或系统 Python，并启动 `python -m uvicorn app.main:app`。

可用环境变量：

| 变量 | 说明 |
|---|---|
| `BUTLER_NO_SPAWN=1` | 不由 Electron 启动后端 |
| `BUTLER_PYTHON` | 指定开发环境 Python 解释器 |
| `BUTLER_BACKEND_DIR` | 指定包含 `app/main.py` 的 backend 目录 |
| `BUTLER_BACKEND_CMD` | 完全覆盖后端启动命令 |
| `BUTLER_BACKEND_URL` | 指定本地后端 URL；非 loopback 地址会被拒绝 |
| `BUTLER_SESSION_FILE` | 覆盖 Electron 与后端共享的 session 文件路径 |

## 模型与 OCR 配置

模型可在设置界面配置，也可以在 `backend/.env` 提供默认值：

```dotenv
MODEL_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=服务商提供的模型名
OPENAI_BASE_URL=https://服务商提供的兼容接口/v1
```

支持 OpenAI-Compatible API 和 Ollama。structured output 的 `auto` 模式只在错误明确表示不支持 `tools`、function calling 或 `response_format` 时降级到提示词 JSON；模型名错误、参数错误和普通 HTTP 400 会原样报告。

OCR 需要安装 [Tesseract](https://github.com/tesseract-ocr/tesseract)，并可配置：

```dotenv
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

后端会实际探测 Tesseract binary、版本和语言包，并区分 `not_configured`、`available`、`binary_not_found`、`language_pack_missing`，不再仅根据配置字符串判断。

## 沙箱目录配置

所有文件操作只能发生在沙箱根目录中。设置界面保存的根目录使用 JSON 数组：

```json
["C:/Users/me/Downloads", "D:/Archive"]
```

数据库中的旧分号格式仍可读取，下一次保存会自动迁移为 JSON。`.env` 中也推荐使用 JSON：

```dotenv
SANDBOX_ROOTS='["C:/Users/me/Downloads", "D:/Archive"]'
```

为了兼容旧部署，`.env` 中的分号格式仍然可读。

## 构建 Windows 桌面包

生产包采用真正的 sidecar 模式：先把 FastAPI/LangGraph 后端冻结成独立 exe，再由 electron-builder 放入 `resources/backend/`。

```powershell
# 1. 安装依赖
cd backend
pip install -r requirements.txt
cd ..\frontend
npm ci

# 2. 构建前端静态资源
npm run build:web

# 3. 构建 backend/dist/butler-backend.exe
npm run build:sidecar

# 4. 构建 Windows portable 包
npm run dist
```

主要产物：

```text
backend/dist/butler-backend.exe
frontend/release/桌面智能文件管家 <version>.exe
frontend/release/win-unpacked/resources/backend/butler-backend.exe
```

可单独执行 sidecar smoke test：

```powershell
backend\dist\butler-backend.exe --self-test
```

生产环境不再向上查找仓库中的 Python 后端，而是直接启动 `process.resourcesPath/backend/butler-backend.exe`。Electron 使用 single-instance lock；第二个实例不会删除 session、再次占用后端端口或启动第二份 sidecar，而是聚焦已有窗口。

## 测试

```powershell
# 后端
cd backend
python -m pytest -q

# 前端类型、状态/API 测试、Electron 生命周期测试
cd ..\frontend
npm run typecheck
npm test

# Web 生产构建
npm run build:web
```

当前回归测试覆盖：

- 200+ 文件批量处理不会遗漏 Planner preview 之外的文件。
- 多次 replan 无法绕过累计审批阈值。
- 文件内容 Prompt Injection 不能触发未授权 mutation。
- 并发 move 不会选择相同目标文件名。
- 扫描上限和截断原因。
- `.doc` 与 `.docx` 的正确能力边界。
- Runtime reset 与运行中 workflow 隔离。
- REST fallback、WebSocket reconciliation 和 rollback 部分失败。
- structured output 错误分类、sandbox 配置迁移、OCR 探测。
- preload backend URL、loopback 限制和 Electron 单实例生命周期。

CI 包含：

- Ubuntu：backend pytest。
- Ubuntu：`npm ci`、typecheck、前端测试、Web build。
- Windows：sidecar build、sidecar smoke test、Electron portable build、artifact 上传。

最近一次本地完整验证结果为 `185 passed, 1 skipped`；跳过项是当前 Windows 权限不允许创建测试所需目录链接的场景。前端 typecheck、tests、Vite build、sidecar self-test 和 Windows portable build 均通过。

## 安全说明

- **原始意图是唯一授权源**：文件内容、OCR、文件名和工具返回都不可信。
- **后端策略不可绕过**：风险和审批由 `PolicyEngine` 判断，不由 LLM 决定。
- **路径沙箱**：越界文件访问一律拒绝；链接不会用来绕过沙箱。
- **本地连接限制**：Renderer 不会把 session token 发送到非 loopback 服务。
- **禁止覆盖**：目标重名时生成唯一名称，并在目标目录锁内完成选择和 mutation。
- **可撤销与审计**：操作及回滚都写入 SQLite；删除使用项目回收站。
- **密钥保护范围**：API Key 不返回前端、不进入日志，但当前仍以明文保存在本地 SQLite；数据库文件应视为敏感数据。

## 配置项

| 变量 | 说明 | 示例 |
|---|---|---|
| `MODEL_PROVIDER` | 模型提供方 | `openai` / `ollama` |
| `OPENAI_API_KEY` | OpenAI-Compatible API 密钥 | `sk-...` |
| `OPENAI_MODEL` | 模型名 | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | OpenAI-Compatible API 地址 | `https://api.example.com/v1` |
| `OLLAMA_BASE_URL` | Ollama 地址 | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Ollama 模型 | `qwen2.5` |
| `STRUCTURED_OUTPUT_MODE` | `auto` 或 `prompt` | `auto` |
| `SANDBOX_ROOTS` | JSON 根目录数组；兼容旧分号格式 | `'["C:/Users/me/Downloads"]'` |
| `TESSERACT_CMD` | Tesseract 可执行文件 | `C:\...\tesseract.exe` |
| `DB_PATH` | SQLite 路径 | `./data/butler.db` |
| `HOST` / `PORT` | 后端监听地址和端口 | `127.0.0.1` / `8000` |

## 当前已知限制

- WebSocket 使用状态快照 reconciliation，尚未实现 `event_id` 和断线事件 replay。
- manifest 暂无 TTL、容量配额和定期压缩策略。
- 用户意图授权当前是保守的后端词法策略，尚未升级为完整的强类型 intent contract。
- Windows portable 已通过本机构建及内置 sidecar 自检，但正式发布仍应在全新 Windows VM 上做安装级验证。
- 尚未配置正式代码签名、生产图标和发布者证书。
- 单实例有生命周期测试，尚无同时启动两个真实 Electron GUI 进程的自动化 E2E。
