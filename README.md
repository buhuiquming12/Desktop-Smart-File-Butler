# 桌面智能文件管家 (Desktop Smart File Butler)

通过自然语言指令，让 AI Agent 自动扫描、分类、重命名、归档、摘要你的文件。

> 例："整理我的下载文件夹，把图片按月份归档，PDF 提取摘要后放到文档区。"

## 架构总览

```
┌───────────────────────────────┐        HTTP / WebSocket        ┌──────────────────────────────┐
│  Electron 前端 (React + TS)    │  ◄──────────────────────────► │  Python 后端 (FastAPI)         │
│  - 对话界面 ChatPanel          │                                │  - Agent 循环 (LangGraph)      │
│  - 任务看板 TaskBoard          │                                │  - 工具集 (fs/extract/vector)  │
│  - 审批弹窗 ApprovalModal      │                                │  - SQLite (记忆/历史/审批)     │
│  - 设置页 Settings             │                                │  - Chroma (内容向量)           │
└───────────────────────────────┘                                │  - APScheduler (定时整理)      │
                                                                  └──────────────────────────────┘
```

Agent 循环（LangGraph StateGraph）：

```
perceive(感知) → plan(规划) → act(工具调用) → observe(观察) → reflect(反思) ─┐
     ▲                                    │                                  │
     └──────────────── 继续 ──────────────┴──── 需审批? ──► approval(interrupt) ┘
```

危险操作（当前实现为删除；后续覆盖式操作也沿用同一机制）在 `approval` 节点触发 LangGraph `interrupt()`，
图状态被持久化，等待前端弹窗确认后用 `Command(resume=...)` 恢复。

## 目录结构

```
agent/
├── README.md
├── backend/
│   ├── requirements.txt
│   ├── .env.example
│   └── app/
│       ├── main.py              # FastAPI 入口：REST + WebSocket
│       ├── config.py            # 环境变量配置
│       ├── models.py            # Pydantic 数据模型
│       ├── db.py                # SQLite 访问层
│       ├── security.py          # 路径沙箱 + 审计日志
│       ├── logging_conf.py
│       ├── agent/
│       │   ├── state.py         # 图状态定义
│       │   ├── llm.py           # LLM 工厂 (OpenAI / Ollama)
│       │   ├── prompts.py
│       │   └── graph.py         # LangGraph Agent 循环
│       └── tools/
│           ├── filesystem.py    # 扫描/移动/重命名/建夹/删除(审批)
│           ├── extract.py       # PDF/Word/TXT/图片 OCR
│           ├── vectorstore.py   # Chroma 向量分类
│           └── scheduler.py     # APScheduler 定时任务
└── frontend/
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts
    ├── index.html
    ├── electron/
    │   ├── main.ts              # Electron 主进程（拉起后端 + 窗口）
    │   └── preload.ts
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── api/client.ts        # REST + WebSocket 客户端
        └── components/
            ├── ChatPanel.tsx
            ├── TaskBoard.tsx
            ├── ApprovalModal.tsx
            └── Settings.tsx
```

## 使用外部大模型 API

本项目默认通过 `langchain-openai` 调用外部 API，并支持 OpenAI 兼容协议。
在 `backend/.env` 中配置：

```dotenv
MODEL_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=服务商提供的模型名
OPENAI_BASE_URL=https://服务商提供的兼容接口/v1
```

`OPENAI_BASE_URL` 留空时使用 OpenAI 官方端点。API Key 只由 Python 后端读取，
不会通过 `/api/config` 返回，也不会存储在 Electron 渲染进程或用户偏好中。
兼容服务必须支持工具所需的结构化输出能力；不支持时规划阶段会快速失败并显示原因。

## 安装与运行

### 1. 后端

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt

# 配置环境变量
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，通常填入外部 OpenAI 兼容 API 的密钥、模型和地址：
# OPENAI_API_KEY=...
# OPENAI_MODEL=服务商提供的模型名
# OPENAI_BASE_URL=https://服务商地址/v1
# 本地 Ollama 仅作为可选备用；同时设置 SANDBOX_ROOTS

# 启动后端 (默认 http://127.0.0.1:8000)
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

OCR 需要系统安装 [Tesseract](https://github.com/tesseract-ocr/tesseract)，并在 `.env` 中
设置 `TESSERACT_CMD`（Windows 例：`C:\Program Files\Tesseract-OCR\tesseract.exe`）。

### 2. 前端

```bash
cd frontend
npm install

# 开发模式（先在另一个终端启动 Python 后端）
npm run dev

# 打包 Electron 前端。当前后端作为独立本地服务运行，打包产物不会内嵌 Python：
npm run build
# Windows 可执行文件位于 release/win-unpacked/桌面智能文件管家.exe
```

## 安全说明

- **路径沙箱**：所有文件操作仅限 `.env` 中 `SANDBOX_ROOTS` 指定的目录，越界一律拒绝。
- **二次确认**：删除操作必须经前端审批弹窗确认后才执行；移动/重命名默认自动改名，禁止覆盖已有文件。
- **审计日志**：每次文件操作记录到 SQLite `operation_log` 表与 `logs/butler.log`。

## 配置项（.env）

| 变量 | 说明 | 示例 |
|------|------|------|
| `MODEL_PROVIDER` | 模型提供方 | `openai` / `ollama` |
| `OPENAI_API_KEY` | OpenAI 密钥 | `sk-...` |
| `OPENAI_MODEL` | 外部 API 模型名 | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址（可选） | `https://api.example.com/v1` |
| `OLLAMA_BASE_URL` | Ollama 地址（可选备用） | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Ollama 模型 | `qwen2.5` |
| `SANDBOX_ROOTS` | 允许操作的根目录（`;` 分隔） | `C:\Users\me\Downloads;C:\Users\me\Desktop` |
| `TESSERACT_CMD` | Tesseract 可执行路径 | `C:\...\tesseract.exe` |
| `DB_PATH` | SQLite 路径 | `./data/butler.db` |
| `CHROMA_DIR` | Chroma 持久化目录 | `./data/chroma` |
"# Desktop-Smart-File-Butler" 
