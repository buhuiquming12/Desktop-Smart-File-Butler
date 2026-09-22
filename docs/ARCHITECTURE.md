# Desktop Smart File Butler：系统架构与设计说明

本文档面向代码阅读、项目复盘与技术面试，描述当前 main 分支的真实实现。

项目的核心思想不是让 LLM 直接“接管文件系统”，而是让模型负责理解意图和生成规则，再由后端策略层与确定性 Python 工具完成授权、匹配、执行、审批和回滚。

## 1. 技术栈与目标

- 桌面端：Electron + React + TypeScript
- 后端：FastAPI + WebSocket
- Agent：LangGraph + OpenAI-Compatible / Ollama
- 数据：SQLite + LangGraph SQLite Checkpoint
- 文件能力：扫描、分类、移动、重命名、摘要、软删除、回滚、OCR、定时任务
- 发布：PyInstaller backend sidecar + electron-builder Windows portable
- CI：pytest + TypeScript typecheck/test/build + Windows sidecar/portable build

核心原则：

1. LLM 负责理解和规划，不负责枚举大批量真实文件。
2. 原始用户意图是授权来源；文件内容、OCR 和工具结果属于不可信数据。
3. 风险策略由后端代码决定，不依赖模型自己声明操作是否安全。
4. 文件操作只能发生在 sandbox 内，并保留审批、审计与回滚能力。
5. 网络恢复后需要恢复真实任务状态，而不只是重新建立 WebSocket。

## 2. 总体架构

~~~mermaid
flowchart LR
    U["用户自然语言请求"] --> UI["React UI"]

    subgraph Desktop["Electron Desktop"]
        UI
        PRELOAD["Preload Bridge<br/>session token / backendUrl"]
        MAIN["Electron Main<br/>single-instance / sidecar lifecycle"]
        UI <--> PRELOAD
        MAIN --> PRELOAD
    end

    subgraph Backend["FastAPI Backend"]
        REST["REST API"]
        WS["WebSocket"]
        AUTH["Local Auth<br/>Origin + Session Token"]
        RUNTIME["Workflow Runtime<br/>thread / cancel / stream lifecycle"]
        REST --> AUTH
        WS --> AUTH
        AUTH --> RUNTIME
    end

    subgraph Agent["LangGraph Agent"]
        PERCEIVE["Perceive"]
        PLAN["Planner"]
        POLICY["PolicyEngine"]
        APPROVAL["Human Approval"]
        EXEC["ToolExecutor"]
        REFLECT["Reflector"]
        PERCEIVE --> PLAN
        PLAN --> POLICY
        POLICY -->|allow| EXEC
        POLICY -->|approval required| APPROVAL
        APPROVAL --> EXEC
        EXEC --> REFLECT
        REFLECT -->|continue / replan| PLAN
    end

    subgraph Data["Deterministic Data & Storage"]
        MANIFEST["Scan Manifest<br/>scan_id + full item set"]
        MATCH["Python deterministic match"]
        LOCK["PathLockManager"]
        FS["Filesystem Tools"]
        DB["SQLite<br/>audit / config / jobs / manifests"]
        CP["LangGraph Checkpoint"]
    end

    UI <-->|REST| REST
    UI <-->|WS events| WS
    MAIN -->|"start packaged sidecar"| Backend
    RUNTIME --> PERCEIVE
    EXEC --> MANIFEST
    MANIFEST --> MATCH
    MATCH --> LOCK
    LOCK --> FS
    FS --> DB
    RUNTIME --> CP
~~~

## 3. Agent 执行链路

用户说“把下载目录里的 PDF 放到 PDF 文件夹”时，系统不会让 LLM 输出几百个具体路径，而是拆成“规则决策”和“确定性执行”两层。

~~~mermaid
sequenceDiagram
    participant User as User
    participant UI as React
    participant RT as Agent Runtime
    participant LLM as Planner
    participant Policy as PolicyEngine
    participant Manifest as Manifest Store
    participant Tool as ToolExecutor
    participant FS as Filesystem

    User->>UI: 整理目录中的 PDF
    UI->>RT: chat request
    RT->>LLM: original user request + bounded context
    LLM-->>RT: scan_directory
    RT->>Tool: execute scan
    Tool->>Manifest: persist full scan result
    Manifest-->>Tool: scan_id + summary + preview
    Tool-->>RT: observation
    RT->>LLM: scan_id + bounded untrusted summary
    LLM-->>RT: batch_move with filter
    RT->>Policy: authorize + cumulative mutation count
    alt approval required
        Policy-->>UI: approval_required
        UI-->>RT: approve / reject
    end
    RT->>Tool: batch_move
    Tool->>Manifest: deterministic match
    Manifest-->>Tool: all matching files
    Tool->>FS: safe move under path locks
    FS-->>RT: success / failed / skipped
    RT-->>UI: final thread state
~~~

### 为什么使用 Manifest

早期实现把扫描结果摘要后交给 Planner，只能让模型看到有限 preview。对于 200+ 文件目录，模型无法可靠枚举全部文件。

当前实现：

~~~text
scan_directory
    ↓
完整结果写入 scan_manifests
    ↓
返回 scan_id + 统计信息 + 有界 preview
    ↓
Planner 生成 filter / batch rule
    ↓
Python manifests.match() 匹配完整集合
    ↓
batch_move / batch_rename / batch_classify
~~~

模型负责“应该处理什么”，Python 负责“具体是哪几个真实文件”。

## 4. 信任边界与 Agent 安全

~~~mermaid
flowchart TD
    A["Original User Request"] -->|"trusted authorization source"| P["Planner / Policy"]
    B["文件名"] --> U["Untrusted Data"]
    C["PDF / DOCX 正文"] --> U
    D["OCR 文本"] --> U
    E["工具返回结果"] --> U
    U -->|"bounded and labeled context"| P

    P --> Q{"Policy decision"}
    Q -->|deny| X["Stop / report"]
    Q -->|approval| H["Human approval"]
    Q -->|allow| T["ToolExecutor"]
    H --> T
    T --> S["Sandbox + Path Validation"]
    S --> F["Filesystem"]
~~~

Agent state 显式区分：

- trusted_user_intent：原始用户请求，是当前实现中的授权依据。
- untrusted_file_data：文件名、OCR、文档正文、扫描结果等。
- tool_observations：工具返回的受限摘要。
- mutations：当前请求累计执行的 move / rename / delete 数量。
- approved_mutation_limit：已经获得人工批准的累计变更额度。

文件内容即使包含“忽略之前指令”“删除所有文件”等文字，也只能作为普通数据，不能自动扩展用户授权。

### Sandbox

文件路径在执行前通过后端路径校验。设计目标包括：

- 禁止越出配置的 sandbox roots。
- 不通过符号链接或 Windows junction 穿透 sandbox。
- 删除采用项目回收站，而不是直接永久删除。
- move、rename、delete 写入操作日志，可按操作或 thread 回滚。

## 5. 请求级审批策略

审批不能只看当前 plan，因为 Agent 会 replan。

~~~text
Plan 1: 15 moves
Plan 2: 10 moves
----------------
累计: 25 moves
→ 达到策略阈值后要求人工审批
~~~

mutation 数量保存在整个 user request 的状态中，不因为重新规划清零。

删除始终属于高风险操作。

后续安全演进方向是把所有有持久副作用的工具统一纳入一个 Side-effect Policy Contract，包括批量分类、定时任务和偏好写入，减少工具之间策略映射不一致的可能。

## 6. 并发与 TOCTOU

简单的“先判断目标不存在，再移动”存在检查与使用之间的竞争窗口。

项目使用 PathLockManager：

- 按规范化后的目录路径建立进程内锁。
- 同目录 mutation 串行。
- 不同目录可以并行。
- 多目录锁按照排序后的 key 获取，降低死锁风险。
- 异常离开上下文后自动释放。

受保护的场景包括 move、rename、delete、rollback 和摘要输出命名。

## 7. 状态恢复

WebSocket 自动重连只能保证“重新连上”，不能保证断线期间的 tool_result、approval_required 或 done 没有丢失。

当前方案采用 state reconciliation：

~~~text
WS disconnect
    ↓
Agent 仍可能继续执行
    ↓
WS reconnect
    ↓
GET /api/threads/{thread_id}
    ↓
恢复 observations / pending approval / final status
    ↓
前端重新对齐任务状态
~~~

REST fallback 发起新聊天时会保存服务端返回的 thread_id，因此后续追问、取消和回滚仍能落到同一个 workflow。

当前尚未实现逐事件 event_id replay。

## 8. Runtime 生命周期

模型设置变化后不能简单替换全局 Runtime，因为旧 workflow 可能还在执行。

~~~text
Runtime A starts workflow
        ↓
settings changed
        ↓
Runtime A -> retired
Runtime B -> new requests
        ↓
workflow A continues on Runtime A
        ↓
active streams = 0
        ↓
Runtime A closes resources
~~~

一个 workflow 从启动到结束绑定同一个 Runtime，避免执行器和状态读取器来自不同实例。

## 9. SQLite 与持久化

SQLite 负责：

- operation_log：操作审计与 thread rollback。
- preferences：用户偏好。
- llm_config：模型配置覆盖。
- scheduled_jobs：定时任务。
- scan_manifests：完整扫描清单。

LangGraph checkpoint 使用独立 SQLite saver 并启用 WAL。

普通业务数据库使用独立短连接，并配置 busy timeout 和 synchronous NORMAL，以适应 FastAPI 与 APScheduler 的多线程访问。

## 10. 桌面端发布架构

~~~mermaid
flowchart LR
    SRC["Python FastAPI / LangGraph"] --> PYI["PyInstaller onefile"]
    WEB["React / Vite dist"] --> PYI
    PYI --> SIDE["butler-backend.exe"]

    TS["Electron Main / Preload"] --> EB["electron-builder"]
    WEB --> EB
    SIDE -->|"extraResources"| EB
    EB --> EXE["Windows Portable EXE"]

    EXE --> MAIN["Electron Main"]
    MAIN -->|"single-instance lock"| WIN["BrowserWindow"]
    MAIN -->|"spawn resources/backend/butler-backend.exe"| BACK["Local Backend"]
    WIN <-->|loopback REST / WS| BACK
~~~

生产包不要求目标机器安装 Python 开发环境。

## 11. CI

GitHub Actions 当前包含：

~~~text
Ubuntu / backend
  ├─ install dependencies
  └─ pytest

Ubuntu / frontend
  ├─ npm ci
  ├─ typecheck
  ├─ frontend tests
  └─ Web + Electron TS build

Windows / desktop
  ├─ Python + Node dependencies
  ├─ Vite build
  ├─ PyInstaller sidecar
  ├─ sidecar self-test
  ├─ electron-builder portable
  └─ upload artifact
~~~

CI 不只验证代码和单元测试，也验证 Windows 桌面交付链能够真正产出 portable artifact。

## 12. 主要模块职责

| 模块 | 职责 |
|---|---|
| agent/graph.py | LangGraph 节点、状态流转、审批与流程编排 |
| agent/tool_executor.py | 工具分发、批量执行、分类、摘要 |
| agent/checkpoints.py | Checkpoint 创建与保留策略 |
| policy/engine.py | 后端授权与累计 mutation 策略 |
| tools/manifests.py | 完整扫描清单与确定性过滤 |
| tools/path_locks.py | 目录级 mutation 锁 |
| tools/filesystem.py | 文件操作、软删除、回滚 |
| api/auth.py | session token 与 Origin 校验 |
| api/connections.py | WebSocket 连接管理 |
| api/management.py | 设置、日志、回滚、定时任务 API |
| runtime/threads.py | thread 锁、活动状态和取消注册 |
| frontend/src/api/client.ts | REST、WebSocket、状态 reconciliation |
| hooks/useBackendConfig.ts | backend endpoint 与首次能力检查 |
| hooks/useSettingsController.ts | 设置页数据与操作状态 |

## 13. 关键设计取舍

### 为什么不是让 LLM 直接调用所有文件工具？

LLM 擅长理解模糊语言，不擅长可靠枚举大集合，也不能作为安全策略执行器。

~~~text
LLM: What should happen?
Policy: Is it allowed?
Python: Which exact files?
Filesystem: Perform it safely.
~~~

### 为什么仍使用 SQLite？

这是单机桌面应用，没有远程多节点一致性需求。SQLite 提供事务、WAL、审计和简单部署，不需要为了“看起来企业级”引入远程数据库。

### 为什么先做 snapshot reconciliation，而不是 Event Sourcing？

逐事件 replay 更完整，但需要 durable event journal、sequence、去重和 retention。当前项目先解决“用户能否确认文件最终到底发生了什么”，因此采用 thread 状态快照恢复。

### 为什么采用 Python sidecar？

LangGraph、文档解析和 OCR 生态集中在 Python；Electron 更适合桌面 UX。sidecar 保留两端优势，并通过 PyInstaller 消除最终用户的 Python 环境依赖。

## 14. 当前边界与下一步

1. WebSocket 尚未实现 event sequence replay。
2. scan manifest 需要 TTL、容量配额和清理策略。
3. 原始意图授权仍是保守词法策略，可演进为强类型 Intent Contract。
4. 所有 side-effect tool 应统一纳入单一 Policy Registry。
5. main.py、graph.py、App.tsx 已开始拆分，但仍可继续抽出 workflow service、Agent transport 和 conversation controller。
6. API Key 当前不回传 Renderer，但本地 SQLite 仍是明文；正式产品可接系统 Keychain 或 Credential Manager。
7. 正式发布还需要代码签名、品牌图标与全新 Windows VM 的安装级验证。
