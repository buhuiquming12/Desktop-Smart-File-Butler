# 桌面智能文件管家 (Desktop Smart File Butler)

通过自然语言指令，让 AI Agent 自动扫描、分类、重命名、归档、摘要你的文件。

> 例："整理我的下载文件夹，把图片按月份归档，PDF 提取摘要后放到文档区。"

## 架构总览

```
┌───────────────────────────────┐      同源 HTTP / WebSocket     ┌──────────────────────────────┐
│  Electron 前端 (React + TS)    │  ◄──────────────────────────► │  Python 后端 (FastAPI)         │
│  - 对话界面 ChatPanel          │   (生产:前端由后端同源托管)     │  - Agent 循环 (LangGraph)      │
│  - 任务看板 TaskBoard          │                                │  - 工具集 (fs/extract/分类)    │
│  - 审批弹窗 ApprovalModal      │                                │  - SQLite (记忆/历史/审批快照) │
│  - 设置页 Settings             │                                │  - APScheduler (定时整理)      │
└───────────────────────────────┘                                └──────────────────────────────┘
```

- **同源托管（生产）**：打包后 FastAPI 用 `StaticFiles` 托管 `frontend/dist`，Electron `loadURL` 到后端，
  前端与 `/api`、`/ws` 同源，从根源避免 CORS 与 opaque(`null`) origin 问题。开发模式仍用 Vite dev server。
- **会话令牌鉴权**：后端启动生成一次性令牌写入会话文件，Electron 经 preload 注入渲染进程；REST 校验
  `X-Butler-Token` 头、WebSocket 校验 `token` 查询参数，且都要求本机 `Origin`。防止本机任意网页驱动 Agent
  或篡改模型 `base_url` 外泄已保存的 API Key。

Agent 循环（LangGraph StateGraph）：

```
perceive(感知) → plan(规划) → act(工具调用) → reflect(反思) ─┐
     ▲                            │                          │
     └──────── 继续/重规划 ───────┴──── 需审批? ──► approval(interrupt) ┘
```

高危操作在 `approval` 节点触发 LangGraph `interrupt()`，图状态被**持久化到 SQLite**，等待前端弹窗确认后用
`Command(resume=...)` 恢复。需审批的场景：

- **删除**：删除不做物理删除，而是移入沙箱内 `.butler-trash/`，因此可撤销。
- **批量移动/重命名**：一次计划中 move/rename 步数超过阈值（默认 20，偏好 `batch_approval_threshold` 可配）
  时统一审批一次，弹窗展示 diff 摘要；拒绝则一个文件都不动。

## 目录结构

```
├── README.md
├── .github/workflows/ci.yml     # CI：后端 pytest + 前端 tsc typecheck
├── backend/
│   ├── requirements.txt
│   ├── .env.example
│   └── app/
│       ├── main.py              # FastAPI 入口：REST + WebSocket + 令牌鉴权 + 静态托管
│       ├── config.py            # 环境变量配置
│       ├── llm_config.py        # 模型配置：DB 覆盖 .env
│       ├── sandbox_config.py    # 沙箱根目录：DB 覆盖 .env（界面可配）
│       ├── models.py            # Pydantic 数据模型
│       ├── db.py                # SQLite 访问层（WAL）
│       ├── security.py          # 路径沙箱校验
│       ├── logging_conf.py
│       ├── agent/
│       │   ├── state.py         # 图状态定义
│       │   ├── llm.py           # LLM 工厂 (OpenAI / Ollama)
│       │   ├── prompts.py
│       │   └── graph.py         # LangGraph Agent 循环（含 LLM 分类、map-reduce 摘要）
│       └── tools/
│           ├── filesystem.py    # 扫描/移动/重命名/建夹/删除(回收站)/回滚
│           ├── extract.py       # PDF/Word/TXT/图片 OCR（分段 + 超时）
│           ├── categories.py    # 扩展名粗分类（LLM 不可用时的回退）
│           └── scheduler.py     # APScheduler 定时任务
│       └── tests/               # pytest（沙箱/审批/回滚/递归/鉴权/摘要/分类等）
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── electron/
    │   ├── main.ts              # Electron 主进程：拉起后端 sidecar + 令牌注入 + 窗口
    │   └── preload.ts
    └── src/
        ├── App.tsx
        ├── api/client.ts        # REST + WebSocket 客户端（自动携带令牌）
        └── components/          # ChatPanel / TaskBoard / ApprovalModal / Settings
```

## 模型与目录：可在设置界面配置

`.env` 提供默认值，**设置界面的改动保存到 SQLite 并覆盖 .env、立即生效**：

- **模型配置**：provider / Base URL / 模型名 / API Key，可拉取可用模型列表后选择。API Key 仅保存在本地后端，
  不回传前端、不显示明文，界面只标记是否已配置。
- **可操作的文件夹（沙箱根目录）**：用 Electron 原生目录选择器添加/移除。这是权限变更，界面有明确警告。

## 使用外部大模型 API

默认通过 `langchain-openai` 调用，支持 OpenAI 兼容协议。可在设置界面配置，或在 `backend/.env` 写默认值：

```dotenv
MODEL_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=服务商提供的模型名
OPENAI_BASE_URL=https://服务商提供的兼容接口/v1
```

`OPENAI_BASE_URL` 留空时使用 OpenAI 官方端点。兼容服务必须支持结构化输出能力；不支持时规划阶段会快速失败并显示原因。
分类与文档摘要也复用该模型（分类改用 LLM 直接给中文类别；长文档走 map-reduce 分段摘要，不再只摘开头）。

## 安装与运行

### 1. 后端

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt

# 配置环境变量
cp .env.example .env      # Windows: copy .env.example .env
# 至少填入 SANDBOX_ROOTS；模型可在此填默认值，也可之后在设置界面配置
```

OCR 需系统安装 [Tesseract](https://github.com/tesseract-ocr/tesseract)，并在 `.env` 设置 `TESSERACT_CMD`
（Windows 例：`C:\Program Files\Tesseract-OCR\tesseract.exe`）。

> 后端依赖含 `langgraph-checkpoint-sqlite`，用于把会话/待审批状态持久化到 `data/checkpoints.sqlite`，重启不丢。

### 2. 前端（开发）

```bash
cd frontend
npm install
npm run dev
```

Electron 主进程会**自动拉起后端 sidecar**（默认用 `backend/.venv` 的 `python -m uvicorn app.main:app`），
无需再手动开 uvicorn。若你想自己管理后端进程，设 `BUTLER_NO_SPAWN=1` 后自行启动即可。

sidecar 相关环境变量：

| 变量 | 说明 |
|------|------|
| `BUTLER_NO_SPAWN=1` | 不自动拉起后端（你自行启动） |
| `BUTLER_PYTHON` | 指定 Python 解释器路径 |
| `BUTLER_BACKEND_DIR` | 指定后端目录（默认向上自动查找 `backend/app/main.py`） |
| `BUTLER_BACKEND_CMD` | 完全自定义启动命令（可指向 PyInstaller 打出的独立 exe） |

### 3. 打包

```bash
cd frontend
npm run build
# 产物：release/win-unpacked/桌面智能文件管家.exe
```

启动时先显示启动页，后端就绪后再加载界面；若 30s 内后端未就绪会显示可读的错误页（而非白屏）。

> ⚠ **分发限制**：打包产物只含前端，**不内嵌 Python 运行时与后端代码**。在本机能运行是因为 sidecar 向上找到了
> 仓库里的 `backend/` 和 `.venv`。要在无 Python 环境的机器上分发，还需用 PyInstaller 把后端打成独立 exe，
> 并通过 `BUTLER_BACKEND_CMD` 指向它——这部分尚未实现。

### 4. 测试

```bash
cd backend && python -m pytest -q      # 后端测试
cd frontend && npm run typecheck        # 前端类型检查
```

## 安全说明

- **路径沙箱**：所有文件操作仅限沙箱根目录（`.env` 的 `SANDBOX_ROOTS` 或设置界面配置），越界一律拒绝。
- **会话令牌 + 来源校验**：REST/WebSocket 均需本机 origin + 一次性令牌；跨源网页无法取得令牌，无法驱动 Agent。
- **人工审批**：删除、超阈值批量移动/重命名需前端弹窗确认后才执行；移动/重命名默认自动改名，禁止覆盖。
- **可撤销**：删除进回收站；move/rename/delete 均可按单条或按会话回滚，回滚也写审计日志。
- **审计日志**：每次文件操作记录到 SQLite `operation_log` 表与 `logs/butler.log`；API Key 不入日志。

## 兼容性与密钥提示

- `llm_config` 为本地 SQLite 配置表，API Key 目前按明文保存。请将数据库文件视为敏感文件；生产环境建议迁移到操作系统凭据管理器，并限制数据目录权限。API Key 不会出现在日志、公开配置接口或 WebSocket 事件中。
- 当前依赖版本（TypeScript 7、Vite 8、Electron 44）经过项目现有构建链验证，存在较新的 Node/Electron API 兼容性约束。除非有专门的兼容性验证，不主动升级主版本。

## 配置项（.env）

| 变量 | 说明 | 示例 |
|------|------|------|
| `MODEL_PROVIDER` | 模型提供方 | `openai` / `ollama` |
| `OPENAI_API_KEY` | OpenAI 密钥 | `sk-...` |
| `OPENAI_MODEL` | 外部 API 模型名 | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址（可选） | `https://api.example.com/v1` |
| `OLLAMA_BASE_URL` | Ollama 地址（可选备用） | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Ollama 模型 | `qwen2.5` |
| `SANDBOX_ROOTS` | 允许操作的根目录（`;` 分隔），也可在设置界面覆盖 | `C:\Users\me\Downloads;C:\Users\me\Desktop` |
| `TESSERACT_CMD` | Tesseract 可执行路径 | `C:\...\tesseract.exe` |
| `DB_PATH` | SQLite 路径 | `./data/butler.db` |

> 模型配置与沙箱根目录也可在设置界面修改，保存后覆盖 `.env` 默认值并立即生效。
