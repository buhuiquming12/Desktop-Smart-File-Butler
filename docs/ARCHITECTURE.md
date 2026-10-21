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

用户拒绝一次累计审批后，`batch_rejected` 会让同一请求里后续的 move/rename/classify 步骤直接记为 rejected，不再逐个弹窗。这个收敛判断必须早于“请求审批”：拒绝后累计变更数没有获批，策略层仍会返回 `requires_approval`，先弹窗就会对同一请求反复要审批。

删除始终属于高风险操作。

工具副作用不再是散落在各处的字符串映射：`tools/contracts.py` 是唯一登记处，记录每个工具的参数模型、处理器、副作用（read/create/write/move/rename/delete/preference/schedule）与权限语义。策略层从这份登记推导，因此：

- `batch_classify` 与实际行为一致地登记为 move，参与移动授权与累计审批；
- 未知工具、缺少契约、参数校验失败都显式失败（fail closed），不会“默认放行”；
- 参数在进入文件系统前由 Pydantic 校验，未知字段直接拒绝。

## 6. 完成判定与运行追踪

流程终止与目标完成是两件事，状态里用不同字段分别表达：

- `status`：流程在哪终止（completed / failed / cancelled / needs_clarification / waiting_approval）。
- `outcome`：目标完成度（success / partial / failed / rejected / cancelled / budget_exhausted / needs_clarification）。
- `stop_reason`：为什么停（goal_verified / model_done / replan_limit / user_rejected / budget_exhausted / tool_failed / exception）。

模型可以提出 `done`，但“是否成功”由 `agent/outcome.py` 的确定性校验决定：它只读取已确认计划与真实工具结果，检查文件是否存在、批量计数是否自洽、扫描是否截断、是否还有未执行步骤。校验只读元数据，不产生新的文件变更；任何部分完成、拒绝、预算耗尽都会覆盖模型“全部成功”的措辞。

两个边界值得单独说明：

- 后置条件在**工具刚执行完时**记录到 observation（`verification`），收尾时再复核最终状态；只有被后续成功变更消费掉的路径（`A→B` 再 `B→C` 的 B）才跳过复核。这样既不会把合法链路判成失败，也不会让“文件在任务结束前消失”被缓存结果掩盖。批量工具的参数里只有 `scan_id`，因此结果额外记录被消费的源路径 `sources`，`batch_move → batch_classify` 这类链同样按正常链路处理。`sources` **不能截断**：链式判断按全部匹配项比对，截断会让大批量链的后半段被误判成“结果消失”；其长度上界由扫描清单本身决定（`filesystem._MAX_SCAN_ITEMS`）。
- 空计划（规划器没有生成任何步骤）没有可验证的后置条件，结论是 `unknown`，不是 `success`；纯问答也按此处理。模型在空计划里给出的“全部完成”措辞不会作为正文，正文使用后端生成的“未执行任何文件操作，无法验证目标”。
- 目标要求来自用户原话：`intent_effects()` 从 `trusted_user_intent` 推导用户要求的副作用（移动/重命名/删除/摘要/偏好/定时），并跳过被否定的词（“不要移动”）。**扫描不算完成“移动”**：用户要求移动而只做了扫描时，`outcome=partial`、`stop_reason=goal_unmet`，并列出 `unmet_effects`。
- `create` 平时是移动/整理的从属授权（移动前可能要建目标目录，`move_file` 自己也会建），因此默认不算目标；但请求里**明确要求**“新建目录/文件夹”时它是独立目标（`explicit_intent_effects()`），混合请求（“新建一个备份文件夹，然后把 PDF 移动进去”）同样要求 `make_dir` 真的成功。定语用法（“移动到新建的 out 文件夹”）不算目标；`创建摘要文件` 属于写入，也不算建目录目标。

统计口径分开：`ok/failed/skipped` 是工具调用次数，`files_ok/files_failed/files_skipped/matched` 是文件级结果。

运行追踪（`run_trace`）记录运行编号、节点耗时、模型调用次数、工具调用次数与停止原因；供应商提供 usage 时记录 Token，否则标记 `unknown`。预算可配置：

| 偏好键 | 默认 | 含义 |
|---|---|---|
| `agent_max_model_calls` | 120 | 单次运行的模型调用上限 |
| `agent_max_tool_calls` | 100 | 单次运行的工具调用上限 |
| `agent_execution_budget_seconds` | 600 | 单次运行的执行时间预算（按节点耗时累计，等待审批/用户输入不计入） |
| `agent_model_timeout_seconds` | 60 | 单次模型请求超时（支持的 provider） |
| `batch_approval_threshold` | 20 | 请求级累计审批阈值 |

取消与超时的边界一致：只在节点/步骤边界生效，已完成的步骤保留，其后不再产生文件变更；失败的工具调用不会自动重试，避免重复写入。

`model_calls` 除了规划与反思，也计入工具内部发起的模型调用（`write_summary` 的分段摘要、`classify_file` 的分类），按线程记账，避免并发会话串数。工具内部调用**在调用前**检查剩余额度：一次 `write_summary` 可能要循环几十次，只在节点边界拦会让单次调用大幅超预算。超额时该工具以明确原因失败，运行在下一个节点边界停止。

澄清是同一目标的延续：只有上一轮停在 `needs_clarification`、且这轮既没有改口标记（`算了/不用了/取消/换个…`）也没有自带操作动词时，新一轮的 `trusted_user_intent` 才会合并上一轮意图。已完成/失败/取消的请求不会为下一条消息背书；改口或新任务也不会继承旧授权。

“自带操作动词”包含**只读**动作：`scan / list / browse / view / 扫描 / 查看 / 浏览 / 清单…` 都登记在 `intent_effects()` 的 `read` 类别里。英文不能只收 `scan/list`：`show / check / display / find / search / inspect / peek / count / print / what's in / tell me` 等日常说法同样属于只读任务，中文也补了 `看看 / 看下 / 列出来 / 有什么 / 统计 / 清点`。否则“show me the files”这类英文只读新任务会被当成单纯补充信息，继承上一轮的移动授权。只读类别同时被完成判定排除（`unmet_effects` 只统计会改变文件的副作用）。

ASCII 词按词边界匹配：`remove` 里的 `move`、`already` 里的 `read` 都不算操作意图。中文没有词边界，仍按子串匹配，并用“词前 3 字符”的否定窗口排除“不要移动”。

**路径是参数，不是指令**：`intent_effects()` 在匹配前先调用 `strip_paths()`。整条消息就是一个路径时（含 `D:\My Documents\scan` 这类带空格、或带引号的写法）整段剥掉；混合消息里再剥盘符路径、UNC 路径、POSIX 绝对路径和 `downloads\scan` 这类相对路径。反过来，剥路径不能吞掉真正的指令：

- `整理/归档` 这类动作枚举不算路径，路径后面的“整理一下”也不会被跨空格的路径规则吃掉。
- 紧贴路径的动作同样要保留：`D:\scan删除`、`把D:\scan里的PDF移动到out` 里的 `删除`/`移动` 不能被当成目录名的一部分。
- 路径后面单独出现的英文动作（`D:\scan delete`、`D:\scan move`）按指令处理；`scan`/`list` 仍是常见目录名（`D:\My scan`、`D:\scan list` 都可能是真实目录），单独出现时仍按路径处理，这是有意的取舍：只读新任务通常把动词放在最前（`list D:\scan`），那条路径不受影响。

否则用户只回答 `D:\scan` 时，路径里的 `scan` 会命中只读词表，被误判成“这轮自带新操作”，把上一轮的整理授权清空；`D:\list`、`D:\清单` 同理。

## 7. 并发与 TOCTOU

简单的“先判断目标不存在，再移动”存在检查与使用之间的竞争窗口。

项目使用 PathLockManager：

- 按规范化后的目录路径建立进程内锁。
- 同目录 mutation 串行。
- 不同目录可以并行。
- 多目录锁按照排序后的 key 获取，降低死锁风险。
- 异常离开上下文后自动释放。

受保护的场景包括 move、rename、delete、rollback 和摘要输出命名。

## 8. 状态恢复

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

## 9. Runtime 生命周期

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

## 10. SQLite 与持久化

SQLite 负责：

- operation_log：操作审计与 thread rollback。
- preferences：用户偏好。
- llm_config：模型配置覆盖。
- scheduled_jobs：定时任务。
- scan_manifests：完整扫描清单。

LangGraph checkpoint 使用独立 SQLite saver 并启用 WAL。

普通业务数据库使用独立短连接，并配置 busy timeout 和 synchronous NORMAL，以适应 FastAPI 与 APScheduler 的多线程访问。

## 11. 桌面端发布架构

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

## 12. CI

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

## 13. 主要模块职责

| 模块 | 职责 |
|---|---|
| agent/graph.py | LangGraph 节点、状态流转、审批与流程编排 |
| agent/tool_executor.py | 工具分发、批量执行、分类、摘要 |
| agent/checkpoints.py | Checkpoint 创建与保留策略 |
| agent/outcome.py | 确定性完成判定：文件级计数、后置条件校验、outcome/stop_reason |
| agent/tracing.py | 运行追踪：Token 用量采集（不可获得时标记 unknown） |
| policy/engine.py | 后端授权与累计 mutation 策略 |
| tools/contracts.py | 工具契约：参数模型、副作用、权限登记（唯一来源） |
| tools/manifests.py | 完整扫描清单与确定性过滤 |
| tools/path_locks.py | 目录级 mutation 锁 |
| tools/filesystem.py | 文件操作、软删除、回滚 |
| tools/naming.py | 确定性重命名（预览与批量执行共用同一函数） |
| tools/preview.py | 逐文件预览、确认令牌、执行前失效重检 |
| tools/duplicates.py | 大小分组 + 流式哈希、硬链接与变化检测 |
| tools/indexer.py | 内容索引（FTS5 单字短语 / LIKE 回退）与检索 |
| tools/classify.py | 按内容分类：缓存、预算、明确回退 |
| tools/watcher.py | 目录轮询、稳定判定、去重与自动整理 |
| tools/trash.py | 回收站列表与恢复 |
| api/auth.py | session token 与 Origin 校验 |
| api/connections.py | WebSocket 连接管理 |
| api/features.py | 预览、重复文件、索引/搜索、规则、回收站 API |
| api/management.py | 设置、日志、回滚、定时任务 API |
| runtime/threads.py | thread 锁、活动状态和取消注册 |
| frontend/src/api/client.ts | REST、WebSocket、状态 reconciliation |
| hooks/useBackendConfig.ts | backend endpoint 与首次能力检查 |
| hooks/useSettingsController.ts | 设置页数据与操作状态 |

## 14. 关键设计取舍

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

## 15. 当前边界与下一步

1. WebSocket 尚未实现 event sequence replay。
2. scan manifest 需要 TTL、容量配额和清理策略。
3. 原始意图授权仍是保守词法策略，可演进为强类型 Intent Contract。
4. 已建立统一的工具契约登记（`tools/contracts.py`）；后续可把处理器注册表本身也迁入该模块。
5. main.py、graph.py、App.tsx 已开始拆分，但仍可继续抽出 workflow service、Agent transport 和 conversation controller。
6. API Key 当前不回传 Renderer，但本地 SQLite 仍是明文；正式产品可接系统 Keychain 或 Credential Manager。
7. 正式发布还需要代码签名、品牌图标与全新 Windows VM 的安装级验证。
7.1 内容检索目前是关键词后端（中文按单字短语匹配），语义检索只预留 `SearchBackend` 接口；重复扫描结果与索引任务状态保存在进程内存，重启后需重新扫描/建索引；自动整理是进程内轮询，应用退出即停止。
8. 长期偏好目前是键值列表（可查看/更新/删除），还没有按语义检索；跨会话只隔离 thread 状态，不隔离偏好（偏好本就是全局设置）。
