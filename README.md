# 桌面智能文件管家（Desktop Smart File Butler）

一个基于 Electron、React、TypeScript、FastAPI 和 LangGraph 的本地智能文件管理 Agent。用户可以通过自然语言扫描、分类、移动、重命名、摘要和删除本地文件，并使用人工审批、撤销、定时任务、后台执行及 WebSocket 状态同步。

项目当前优先保证文件操作的正确性、安全边界、可恢复性和桌面端可发布性。大批量文件不由 LLM 枚举：LLM 负责理解意图与生成规则，Python 根据完整扫描清单确定实际文件集合。

> 示例：“整理下载目录，把 PDF 移到 PDF 文件夹，并按文件名加上 `archive-` 前缀。”

## 求职与设计文档

- [系统架构与设计说明](docs/ARCHITECTURE.md)：系统组件、Agent 流程、安全边界、状态恢复、并发与桌面发布架构。
- [Agent 固定场景清单](docs/AGENT_SCENARIOS.md)：31 个可重复验证场景（初始文件 → 用户输入 → 预期行为 → 最终断言 → 对应测试）。
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
- 设置界面直接配置模型、授权目录、默认管理目录和 OCR 工具路径（含原生目录/文件选择器），保存后按新配置重新检测，无需重启应用。
- Windows 生产包内置 PyInstaller 后端 sidecar，目标机器无需 Python 开发环境。

### 批量操作前置能力（侧边栏入口）

| 入口 | 能力 | 边界 |
|---|---|---|
| 操作预览 | 为移动/重命名/分类/删除生成**逐文件**计划，可翻页、取消勾选、改分类目标，确认后只执行选中项 | 生成预览全程只读；执行前重新核验源文件与目标冲突，失效即拒绝并要求刷新 |
| 重复文件 | 大小分组 → 流式内容哈希 → 重复组/保留建议/可回收空间；清理走软删除与回滚 | 不自动删除；清理前重新计算哈希；跳过回收站、符号链接、目录联接；硬链接不重复计空间 |
| 内容搜索 | 本地正文索引 + 中文关键词检索（FTS5 单字短语或子串回退），结果可定位 | 需要先建立索引；只返回当前授权目录内的文件；截断/失败/未索引状态明确标记 |
| 自动整理 | 保存规则后监听目录，文件写入稳定再按类型/内容移动到类别目录 | **仅应用运行期间生效**；只处理直接子文件；超过审批阈值不自动执行 |
| 回收站 | 查看软删除文件的原路径、删除时间、大小、所属任务并恢复 | 恢复不覆盖同名文件；本版不提供永久删除 |

自动整理与内容分类都复用确定性规则，不会每次调用模型重新规划；内容分类只在真的读到正文且模型给出类别时标记为“按内容”，其余情况明确回退到扩展名规则。


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
- `status`：流程在哪终止；`outcome`：目标是否真正完成（两者独立）。
- `run_trace`：运行编号、节点耗时、模型/工具调用次数与停止原因。

文件内容即使包含“忽略之前指令”“删除所有文件”或伪造的系统消息，也只能作为普通数据，不能扩展用户授权。

## 工具契约与完成判定

所有工具的副作用登记在 `backend/app/tools/contracts.py`：参数模型（Pydantic，拒绝未知字段）、副作用类别与权限语义都在这里，`PolicyEngine` 只从这份登记推导。

- `batch_classify` 实际会移动文件，因此登记为 `move`，与 `batch_move` 共用移动授权与请求级累计审批。
- 未知工具、缺少契约、参数无效一律显式失败，绝不默认放行。
- 删除始终需要人工审批；`move`/`rename`/`batch_classify` 按整个用户请求累计，重规划不会清零。

流程终止（`status`）与目标完成（`outcome`）分开表达。模型可以提出结束，但“是否成功”由 `backend/app/agent/outcome.py` 只读校验已确认计划与真实工具结果后决定：

| outcome | 含义 | 用户看到的统计 |
|---|---|---|
| `success` | 所有可验证后置条件通过 | 成功 N / 失败 0 |
| `partial` | 部分完成、扫描截断、目标要求未做，或仍有未执行步骤 | 成功 8 / 失败 2，并说明原因 |
| `rejected` | 用户拒绝了关键审批 | 未执行，文件未变 |
| `cancelled` | 用户取消 | 已完成操作保留，可撤销 |
| `budget_exhausted` | 重规划或运行预算耗尽 | 说明未完成内容与停止原因 |
| `needs_clarification` | 需要用户补充信息 | 零文件变更 |
| `unknown` | 空计划/无可验证后置条件 | 不宣称完成 |

统计口径分开：`ok/failed/skipped` 是工具调用次数；`files_ok/files_failed/files_skipped/matched` 是文件级结果。校验动作只读元数据，不会产生新的文件变更；后置条件在工具刚执行完时记录，收尾时再复核最终状态，因此 `A→B` 再 `B→C` 不会被误判，而“结果在任务结束前消失”也不会被缓存掩盖。

目标要求来自用户原话：用户说“移动”却只做了扫描时，`stop_reason=goal_unmet` 并给出 `unmet_effects`，不会被算作完成。否定句（“不要移动”）不算授权。

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

回灌给规划器的扫描摘要会保留后续执行必需的字段（`scan_id`、`total`、`scanned_count`、`truncated`、`reason`、完整扩展名统计），只截断样本明细；样本统计不会被冒充为完整统计（摘要里的 `statistics_source` 标明来源）。

## 操作预览、重复文件、搜索、自动整理与回收站

这些能力共用同一套安全链路：路径沙箱（`resolve_in_sandbox`）、目录锁（`PathLockManager`）、SQLite 审计与回滚。界面入口在左侧导航，Agent 也可以调用只读的 `preview_batch` / `search_content`。

### 操作预览（P0）

预览由 Python 根据完整 manifest 与确定性规则生成，LLM 只提供规则，不枚举文件：

1. `POST /api/previews` 生成计划（只读），返回 `preview_id`、逐文件 `source/target/status/reason` 与 `digest`；
2. 用户翻页、取消勾选、（分类时）修改目标类别；
3. `POST /api/previews/{id}/confirm` 把**这次选中的条目集合**绑定成一次性 `approval_id`；删除或超过批量阈值时必须显式 `approve=true`，否则返回 409 `approval_required`；
4. `POST /api/previews/{id}/execute` 带 `confirm_token` 执行。

执行语义：

- 选择在确认后发生变化（增加/替换条目）一律 409 `selection_changed`，旧确认不会授权更大的操作；
- 执行前逐项复核源文件的大小/修改时间与目标冲突，任何一项不一致就整体拒绝（409 `stale_preview`，附带失效清单），不会静默执行“看起来差不多”的结果；
- 单个文件失败不中断整批，结果逐项返回；批次记录在 `preview-<id>` 线程下，可撤销；
- 生成预览不创建目录、不移动文件，`preview_batch` 工具在工具契约中登记为 `read`。

### 重复文件（P1）

`POST /api/duplicates/scan` 在后台线程执行（不阻塞事件循环），`GET /api/duplicates/{scan_id}` 读取进度/结果，`POST .../cancel` 取消。算法：

- 先按文件大小分组，再用首 64 KiB 粗筛，最后流式全量 SHA-256（内存占用与文件大小无关）；
- 文件名不同但内容相同会被识别；同名不同内容不会；
- 跳过 `.butler-trash/`、符号链接与 Windows 目录联接；硬链接按 `(st_dev, st_ino)` 识别，可回收空间只按不同 inode 计算；
- 哈希前后各取一次签名，变化时重算一次，仍然变化则标记 `changed` 并排除，结果带 `stale=true`；
- 保留建议取修改时间最新的一份（并列取路径最短），用户可改选。

清理：`POST /api/duplicates/{scan_id}/cleanup` 必须 `confirm=true`；每组重新计算保留项与被删项内容哈希，不一致就跳过；删除走 `delete_file`（回收站 + 审计 + 可按 `dedupe-<scan_id>` 撤销）。

### 内容搜索（P2）

- `POST /api/index/scan` 建/更新索引（后台任务，`GET /api/index/jobs/{id}` 查进度、`POST .../cancel` 取消），`GET /api/index/stats` 看统计；
- `POST /api/search` 检索，返回文件名、路径、命中片段、是否截断。

中文检索方案：FTS5 可用时把正文按**单个汉字**切分写入索引，查询时把中文片段转成相邻短语（`报告` → `"报 告"`），因此是真正的子串语义；FTS5 不可用时回退 `LIKE '%关键词%'`，中文同样正确。两条路径都有测试覆盖。

增量更新：按 `path + size + mtime_ns` 判断是否需要重新提取；遍历中消失的文件会从索引删除；`move_file`/`rename_file`/`delete_file` 会同步更新索引路径或删除记录。

授权：搜索时对每条结果重新做 `is_within_sandbox` 与索引根目录校验，授权收缩后旧索引不会再返回该文件内容。

为语义检索预留 `SearchBackend` 协议与后端注册表，当前只注册真实可用的 `keyword`；未实现的后端会返回 422，而不是空壳结果。

### 批量分类（P3）

`batch_classify` 新增 `mode`：

- `extension`：扩展名规则（原有行为）；
- `content`：读取正文 + 模型判定，结果写入 `classification_cache`（键含 size/mtime，文件一变即失效），并有本次运行预算；预算耗尽、模型未配置、格式不支持、提取失败都会回退扩展名规则，返回 `fallback` 数量与逐条 `fallback_items.reason`。

预览与批量工具都展示“依据”（按类型 / 按内容 / 按内容（缓存）），不编造置信度；预览里每个条目可改目标类别，执行时校验为单层安全目录名。

### 高级重命名（P4）

`batch_rename` 与预览共用 `app/tools/naming.py`：前缀、后缀、查找替换（可区分大小写）、序号（起始/步长/位数/前置或后置）、日期模板（`{name}{seq}{date}{ext}`）、日期来源（修改时间/创建时间/当前时间/正文明确日期）。

始终保留扩展名；非法字符、Windows 保留名、结尾空格/点、同批次重名（大小写不敏感）都会被标记为跳过或冲突。日期取不到时跳过该文件并说明原因，不猜测日期或金额。预览与执行使用同一函数，测试会比对两者产出的文件名是否一致。

### 自动整理（P5）

`GET/POST /api/rules`、`PUT/DELETE /api/rules/{id}`、`POST /api/rules/{id}/run`。

- 轮询线程每 2 秒检查一次；文件必须连续两次观察签名一致且间隔 ≥ `settle_seconds` 才处理；
- 已处理事件写入 `watch_events`（rule_id + path + signature），因此重复触发、进程重启都不会重复搬同一文件；重启后未处理过的文件会被补扫；
- 只处理规则目录的直接子文件，跳过回收站、链接、临时文件与目标目录子树，避免自我移动循环；
- 一批新文件超过 `batch_approval_threshold` 时不自动执行，记录原因并在规则状态中展示，交由「操作预览」确认；
- **自动整理依赖应用进程运行**：应用退出后轮询线程结束，不会再处理任何文件（界面已明确提示）。

### 回收站（P6）

`GET /api/trash?query=&limit=&offset=` 返回软删除记录（原路径、回收站路径、删除时间、大小、所属任务、原位置是否被占用）；`POST /api/trash/restore` 逐项恢复并返回每项结果。

恢复复用 `restore_operation`：原位置被占用时自动改名，**绝不覆盖**；回收站文件已被外部清理时该项标记为跳过。本版不提供永久删除。

## 多轮对话与记忆

- 同一 thread 的追问保留 `conversation` 与 `last_scan`，因此“刚才那些 PDF”可以复用上一轮的 `scan_id`，不需要重新枚举文件。
- 每次运行重新开始 `mutations` / `approved_mutation_limit`：历史审批不会自动授权新请求。
- 引用旧扫描时，执行前会核验 manifest 与扫描目录是否仍有效；失效时明确失败并提示重新扫描。
- thread 之间不共享扫描与对话；偏好是全局设置，可在「偏好」页查看、更新和删除，`__` 前缀保留键不可写也不可删。
- 信息不足或对象有歧义时，规划器调用 `request_clarification`，图以 `needs_clarification` 暂停，等待用户补充后再继续。

默认扫描上限为 5000 项、12 层。超过限制时会明确返回：

- `truncated: true`
- `reason: max_items` 或 `max_depth`

权限错误使用 `permission_error`；完整扫描使用 `none`。恰好扫描到 5000 项且不存在第 5001 项时不会误报截断。扫描不会进入 `.butler-trash/`，也不会跟进目录符号链接或 Windows junction。

## 审批、并发和回滚

审批由后端 `PolicyEngine` 决定，LLM 无权自行取消安全门槛。

- 删除始终属于高风险操作。
- move、rename 和分类移动（`batch_classify`）按同一个用户请求累计；模型把 60 个操作拆成多次 replan 也不能绕过阈值。
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

## 运行追踪与预算

`done` 事件与 `GET /api/threads/{id}` 都会返回 `outcome`、`stop_reason`、`run_id`、`usage` 与结构化 `summary`。运行追踪只保存聚合数字，不记录提示词或文件内容。

可用偏好键调整预算（设置接口 `/api/preferences/{key}` 写入）：

| 偏好键 | 默认 | 说明 |
|---|---|---|
| `agent_max_model_calls` | 120 | 单次运行模型调用上限 |
| `agent_max_tool_calls` | 100 | 单次运行工具调用上限 |
| `agent_execution_budget_seconds` | 600 | 执行时间预算（按节点耗时累计，等待审批/补充信息不计入） |
| `agent_model_timeout_seconds` | 60 | 单次模型请求超时 |
| `batch_approval_threshold` | 20 | 请求级累计审批阈值 |

供应商提供 Token usage 时记录到 `usage`；不提供时标记 `source: unknown`，不会伪造成 0。`model_calls` 也包含摘要/分类工具内部的模型调用。取消与超时只在节点/步骤边界生效；已完成的步骤保留，失败的工具调用不会自动重试。

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
│   │   │   ├── features.py     # 预览、重复文件、索引/搜索、规则、回收站
│   │   │   └── management.py   # 设置、审计、回滚和定时任务路由
│   │   ├── policy/             # 后端授权和累计审批策略
│   │   ├── runtime/            # 活动 thread、锁和取消状态
│   │   ├── tools/
│   │   │   ├── filesystem.py   # 文件操作和回滚
│   │   │   ├── manifests.py    # 完整扫描清单与确定性匹配
│   │   │   ├── path_locks.py   # 目录级 mutation locks
│   │   │   ├── extract.py      # PDF、DOCX、文本、OCR 提取
│   │   │   ├── naming.py       # 确定性重命名（预览与执行共用）
│   │   │   ├── preview.py      # 逐文件预览、确认绑定、失效重检
│   │   │   ├── duplicates.py   # 大小分组 + 流式哈希重复检测
│   │   │   ├── indexer.py      # 内容索引（FTS5/回退）与关键词检索
│   │   │   ├── classify.py     # 按内容分类（缓存/预算/回退）
│   │   │   ├── watcher.py      # 目录监听与自动整理规则
│   │   │   ├── trash.py        # 回收站查看与恢复
│   │   │   └── scheduler.py
│   │   ├── main.py             # FastAPI 装配与 Agent 流协调
│   │   ├── db.py               # SQLite 数据访问
│   │   ├── llm_config.py       # 模型配置（数据库覆盖 > .env）
│   │   ├── sandbox_config.py   # 授权目录，即安全边界
│   │   ├── workspace_config.py # 默认管理目录，必须落在授权目录内
│   │   ├── external_tools_config.py # 外部工具路径（Tesseract 等）
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
    │   ├── features.ts         # 预览/重复/搜索/回收站的纯函数与格式化
    │   ├── hooks/              # 后端连接、会话与设置状态控制器
    │   ├── components/         # 会话、审批、设置、预览、重复文件、搜索、规则、回收站
    │   └── types.ts            # 前后端共享的显式类型
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

## 设置界面与配置优先级

设置界面（React → `useSettingsController` → `ApiClient` → `/api/settings/*` → 专用配置服务 → SQLite / `.env`）面向不编辑 `.env` 的普通用户。组件不直接访问数据库，Renderer 不接触 Node API，配置服务也不反向依赖 Agent。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` / `PUT` | `/api/settings/llm` | 模型提供方与密钥 |
| `POST` | `/api/settings/llm/models` | 拉取可用模型列表 |
| `GET` / `PUT` | `/api/settings/sandbox` | 授权目录 |
| `GET` / `PUT` | `/api/settings/workspace` | 默认管理目录 |
| `GET` / `PUT` | `/api/settings/tools` | 外部工具路径（当前为 Tesseract） |

统一优先级为 **SQLite 运行时配置 > `.env` 默认值 > 系统默认**。读取路径不做缓存，因此保存后立即生效；除 LLM 配置外都不需要重建 Agent runtime，也都不需要重启应用。`PUT /api/settings/tools` 在保存成功后会立刻用新配置重新执行一次能力检测，响应里带回最新结果，因此前端「保存并检测」是一次往返。

外部工具配置集中在 `external_tools_config.py`，用 `ToolSpec` 注册表描述「哪个键、属于哪个工具、是文件还是目录」，新增 ffmpeg、pandoc 等只需在注册表里加一行。写入前校验路径长度、非法字符、`is_file` / `is_dir`，失败返回 **422** 和中文提示（例如「Tesseract 可执行文件不存在：…」）；空字符串表示**清除该项覆盖并回退到 `.env`**，而不是错误。

## 模型与 OCR 配置

模型可在设置界面配置，也可以在 `backend/.env` 提供默认值：

```dotenv
MODEL_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=服务商提供的模型名
OPENAI_BASE_URL=https://服务商提供的兼容接口/v1
```

支持 OpenAI-Compatible API 和 Ollama。structured output 的 `auto` 模式只在错误明确表示不支持 `tools`、function calling 或 `response_format` 时降级到提示词 JSON；模型名错误、参数错误和普通 HTTP 400 会原样报告。

OCR 需要安装 [Tesseract](https://github.com/tesseract-ocr/tesseract)。两项配置都能在设置界面填写，也能用 `.env` 提供默认值（界面上的值优先）：

```dotenv
# ===== OCR =====
# Tesseract 可执行文件，可留空
# 前端设置中的值优先于该环境变量
TESSERACT_CMD=
# Tesseract 语言包目录，例如：
# C:\Program Files\Tesseract-OCR\tessdata
TESSDATA_DIR=
```

`TESSDATA_DIR` 留空时使用 Tesseract 自带默认值。设置界面用原生文件/目录选择器取值，也可以直接粘贴路径。

后端会实际探测二进制、版本和语言包，而不是根据配置字符串推断能力。`GET /api/settings/tools` 的 `ocr` 字段结构稳定（键恒存在），`status` 取值为：

| `status` | `available` | 含义 |
|---|---|---|
| `available` | `true` | 二进制、版本和 `eng` + `chi_sim` 语言包都就绪 |
| `language_pack_missing` | `true` | 二进制可用，但缺少必需语言包，`missing_languages` 列出缺哪些 |
| `not_configured` | `false` | 未安装 `pytesseract` |
| `binary_not_found` | `false` | 配置的路径与系统 PATH 都找不到可执行文件 |
| `invalid_tessdata_dir` | `false` | 配置的语言包目录不存在或不是目录 |
| `error` | `false` | 探测过程本身失败，`message` 带原因 |

OCR 是软依赖：所有失败都收敛成状态值，不会抛异常，也不会阻止后端启动或影响非 OCR 的文件操作。

**Windows 含空格路径**（例如 `C:\Program Files\Tesseract-OCR\tessdata`）：pytesseract 内部用 `shlex.split(posix=False)` 解析配置串，在 Windows 上会**保留引号**，因此带引号的 `--tessdata-dir "C:\Program Files\..."` 反而会失败。后端按平台分流：POSIX 用带引号参数；Windows 且路径无空白时直接用裸路径；Windows 且路径含空白时改用 `TESSDATA_PREFIX` 环境变量，并且只清理进程自己写入的值，不覆盖用户已有的 `TESSDATA_PREFIX`。该行为由真实二进制端到端测试双向锁定（无空格与含空格目录各跑一次）。

## 授权目录与默认管理目录

两个概念必须区分：

- **授权目录（授权根目录，`sandbox_roots`）**：安全边界，即最大可访问范围。Agent 不能读写这些目录（及其子目录）之外的文件，删除也只会进入其内的 `.butler-trash/`。
- **默认管理目录（`default_managed_root`）**：普通用户填写的「没指定路径时用哪个目录」。它**必须位于某个授权目录之内**，否则保存会被拒绝并返回明确错误。它只是默认值，不是权限捷径。

智能体感知（`_perceive`）会同时拿到 `allowed_roots` 和 `default_managed_root`，提示词约定：用户明确指定目录 → 用用户的目录（仍受沙箱校验）；否则若存在默认管理目录 → 直接使用，不追问、不猜测；两者都没有 → 不生成任何文件操作步骤，而是要求用户指定目录或在设置里配置。任何路径最终都必须通过 `resolve_in_sandbox()`。

默认管理目录保存在 SQLite 的保留键 `__default_managed_root__` 下。保留键（`__` 前缀）对普通偏好接口不可见也不可写，因此 REST 的 `PUT /api/preferences/{key}` 和 Agent 的 `set_preference` 都无法改动这类安全相关配置。

读取侧还有第二道防御：`effective_default_root()` 只返回「位于授权目录内 **且** 当前确实存在的目录」，所以授权目录被收缩、或目录被删除后，越界的旧值会被忽略并自动剪枝，接口用 `stored_default_managed_root` 与 `valid` / `message` 告诉界面「已保存的值已失效」。

授权目录在设置界面保存为 JSON 数组：

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
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd ..\frontend
npm ci

# 2. 构建前端静态资源
npm run build:web

# 3. 构建 backend/dist/butler-backend.exe
npm run build:sidecar

# 4. 构建 Windows portable 包
npm run dist
```

> `npm run build:sidecar` 调用的是裸 `python`，若该解释器没装 PyInstaller，脚本会自动改用
> `backend/.venv` 里的解释器重跑；也可以用 `PYTHON=D:\path\to\python.exe npm run build:sidecar`
> 显式指定。两步都通过后 `frontend/release/` 下才会出现可分发 exe。

如果第 4 步报 `EPERM ... win-unpacked.tmp -> win-unpacked` 或
`safe-delete ... BULK_CONFIRM_REQUIRED`，说明当前环境（如受文件守卫/杀毒监控的目录）
锁住了解包出的 `*.asar`，electron-builder 无法清理或改名。把输出目录换到工作区之外再打包，
再把 exe 拷回来即可：

```powershell
npx electron-builder --win portable --config.directories.output=D:/butler-build
copy "D:\butler-build\桌面智能文件管家 0.1.0.exe" "release\"
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
- `batch_classify` 与 `batch_move` 共用移动授权与累计审批；超过阈值时首次变更前暂停。
- 工具契约：未知工具、缺失参数、多余字段都在文件系统访问前显式失败。
- 扫描摘要保留 `scan_id`/`scanned_count`/`truncated`/`reason`/完整统计；截断原因同时到达规划器与用户。
- 完成判定：10 项成功 8 项时返回 `partial` 而非全部成功；校验只读、不产生新变更；重规划上限说明未完成内容。
- 空计划返回 `unknown` 而不是成功，且不回显模型编造的“全部完成”；`A→B→C` 连续操作与 `batch_move → batch_classify` 批量链不会因中间路径被消费而判为部分完成；
  结果在任务结束前消失仍会被复核发现。
- 澄清续问只回目录时仍沿用上一轮授权；澄清中改口、换任务或只要求“扫描/查看”时不继承旧授权；已完成请求不会为下一条消息背书。
- 只读新任务覆盖英文日常说法（`show / check / find / search / what's in / tell me`），不会因为词表只有 `scan/list` 就继承上一轮移动授权。
- 意图词表包含只读动作（扫描/查看/浏览/清单），因此“只扫描一下”不会被当成没有新操作；ASCII 词按词边界匹配（`remove` 不算 `move`）；匹配前先剥离路径，`D:\scan`、`D:\list`、`D:\My Documents\scan` 这类目录名不会被当成指令，而 `整理/归档`、`downloads/report 整理一下`、`D:\scan删除`、`D:\scan delete` 里真正的动词不会被路径规则吃掉。
- 摘要/分类工具内部的模型调用计入运行预算，且在调用前检查剩余额度。
- 用户要求“移动”而只做了扫描时返回 `goal_unmet` 与 `unmet_effects`，不算完成；明确要求“新建目录”却没建成同样不算完成（混合请求也校验，定语用法除外）。
- 大批量链式操作（`batch_move → batch_classify`）的 `sources` 不截断，超过 200 项也不会被误判为部分完成。
- 拒绝一次累计审批后，同一请求的后续移动步骤不再反复弹窗，文件一个都不动。
- 断线重连会恢复待澄清状态并显示待回答的问题，不会永久停在“处理中”。
- 多轮：澄清、引用上一轮扫描、修改目标、跨会话隔离、旧扫描失效重新核验；历史审批不授权新请求。
- 运行追踪与预算：节点耗时/模型调用/停止原因、Token unknown 标记、模型调用上限、等待审批不耗预算。
- 文件内容 Prompt Injection 不能触发未授权 mutation。
- 并发 move 不会选择相同目标文件名。
- 扫描上限和截断原因。
- `.doc` 与 `.docx` 的正确能力边界。
- Runtime reset 与运行中 workflow 隔离。
- REST fallback、WebSocket reconciliation 和 rollback 部分失败。
- structured output 错误分类、sandbox 配置迁移、OCR 探测。
- 外部工具配置：数据库覆盖优先于 `.env`、空值清除并回退、未知键忽略、非法路径被拒、旧数据库升级、`tool_config` 表缺失时降级为空配置。
- OCR 运行时：未安装依赖、找不到二进制、缺少 `chi_sim`、非法 tessdata 目录、探测异常降级、配置更新后无需清缓存即生效、用户已有的 `TESSDATA_PREFIX` 不被覆盖；另有一个由 `BUTLER_TEST_TESSERACT_CMD` 开关的真实二进制端到端用例。
- 默认管理目录：授权目录内可保存、越界被拒、授权目录收缩后自动剪枝、目录被删除后视为失效、保留键对普通偏好接口和 Agent 的 `set_preference` 都不可写、且不会成为绕过 `resolve_in_sandbox()` 的捷径。
- 偏好可查看、更新、删除；保留键不可删除。
- 新增设置接口的 GET / PUT / DELETE、422 中文错误消息、清除语义，以及会话令牌校验。
- preload backend URL、loopback 限制和 Electron 单实例生命周期。
- 操作预览：生成预览不创建目录/不移动文件；只执行勾选条目；确认后扩大选择被拒；源文件变化或目标被占用时整体拒绝并列出失效项；删除/超阈值必须显式确认；冲突项不可选中；分页正确。
- 重复文件：同名不同内容不算重复、异名同内容成组；跳过回收站与符号链接；硬链接不重复计可回收空间；哈希期间文件变化标记失效；清理前重新校验内容并进入回收站；未确认的清理请求被拒。
- 内容搜索：中文关键词（FTS5 单字短语）与 FTS5 不可用时的中文子串回退均命中；英文检索与分页；修改/移动/重命名/删除后的索引增量与清理；收缩授权目录后旧索引不再返回内容；不支持格式与提取失败被标记；截断被标记；未知检索后端被拒绝。
- 批量分类：按内容使用模型并展示类别统计；缓存命中不再调用模型；模型未配置/预算耗尽时回退扩展名并说明原因；预览显示依据且允许修改类别；非法类别被拒绝。
- 高级重命名：查找替换（大小写敏感/不敏感）、序号前缀与补零、日期来源与模板、正文日期可用/不可用、非法名、同批次重名；批量工具与预览产出的文件名一致。
- 自动整理：规则 CRUD 与启停、写入稳定等待与二次观察、签名变化重新计时、已处理事件去重与重启补扫、目标目录不自我循环、临时文件忽略、超阈值不自动执行、操作归属 `rule-<id>` 可回滚、监听线程启停。
- 回收站：列表元数据与所属任务、恢复、原位置冲突自动改名不覆盖、逐项结果、筛选、已被清理的条目跳过。

CI 包含：

- Ubuntu：backend pytest。
- Ubuntu：`npm ci`、typecheck、前端测试、Web build。
- Windows：sidecar build、sidecar smoke test、Electron portable build、artifact 上传。

最近一次本地完整验证结果为 `547 passed, 1 skipped`（后端）。跳过项是真实 Tesseract 端到端用例（需要 `BUTLER_TEST_TESSERACT_CMD` 指向本机二进制）；Windows 未开启开发者模式时无法创建目录链接的用例会自动跳过。前端 `typecheck`、`npm test`（状态 / API / 新能力纯函数 / Electron 生命周期）与 Vite 生产构建均通过。真实模型的多轮对话评测需要配置模型，本次未运行。

> Windows 上若 `%TEMP%` 不可写，pytest 的 `tmp_path` 会失败；可用 `python -m pytest -q --basetemp=D:/butler-pytest-tmp` 指定可写目录。

## 安全说明

- **原始意图是唯一授权源**：文件内容、OCR、文件名和工具返回都不可信。
- **后端策略不可绕过**：风险和审批由 `PolicyEngine` 从 `tools/contracts.py` 的副作用登记推导，不由 LLM 决定；未知工具或参数无效直接拒绝。
- **路径沙箱**：越界文件访问一律拒绝；链接不会用来绕过沙箱。
- **默认目录不扩权**：默认管理目录只是「没指定路径时用哪个目录」，保存时必须位于授权目录内，读取时二次校验，越界或已失效的值会被忽略并剪枝；它始终不能替代 `resolve_in_sandbox()`。
- **安全相关配置不是普通偏好**：`__` 前缀的保留键对 `/api/preferences/{key}`（含 DELETE）和 Agent 的 `set_preference` / `delete_preference` 都不可写、不可删，避免模型自行放宽边界。
- **外部工具只配置路径**：OCR 设置只接受可执行文件路径和语言包目录，不接受任何命令行参数；调用时以参数列表交给 `subprocess`，不经过 shell。工具路径也不是 Agent 的文件操作路径，不受沙箱约束。
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
| `TESSERACT_CMD` | Tesseract 可执行文件；设置界面的值优先 | `C:\...\tesseract.exe` |
| `TESSDATA_DIR` | Tesseract 语言包目录；设置界面的值优先 | `C:\...\tessdata` |
| `DB_PATH` | SQLite 路径 | `./data/butler.db` |
| `HOST` / `PORT` | 后端监听地址和端口 | `127.0.0.1` / `8000` |

## 当前已知限制

- WebSocket 使用状态快照 reconciliation，尚未实现 `event_id` 和断线事件 replay。
- manifest 暂无 TTL、容量配额和定期压缩策略。
- 用户意图授权当前是保守的后端词法策略，尚未升级为完整的强类型 intent contract；否定句（如「不要移动」）仍依赖模型不生成该步骤，后端只能约束“是否出现操作”。
- 运行追踪的模型/工具调用计数在节点边界统计；如果模型在一次调用内部多次重试（例如结构化输出修复），`model_calls` 只记 1 次。
- Token 用量依赖供应商返回 usage；本地 Ollama 等场景通常标记为 `unknown`。
- 真实 Tesseract 端到端用例由环境变量开关控制，CI 环境无二进制，因此上游回归只能靠不依赖真实二进制的那部分用例兜住。
- `TESSDATA_PREFIX` 的语义随 Tesseract 版本变化（5.x 要求指向 `tessdata` 目录本身，更早版本期望其父目录）；当前按 5.x 实现并已在本机 Tesseract 5.0 实测。
- 外部工具配置目前只在 `/api/settings/tools` 暴露，尚未纳入 `GET /api/config` 概览，也没有统一的「所有配置来源」视图。
- Windows portable 已通过本机构建及内置 sidecar 自检，但正式发布仍应在全新 Windows VM 上做安装级验证。
- 尚未配置正式代码签名、生产图标和发布者证书。
- 单实例有生命周期测试，尚无同时启动两个真实 Electron GUI 进程的自动化 E2E。
- 真实模型评测（多轮指代、提示注入的实际模型表现）需要模型配置，不属于确定性测试范围；本次未运行。
- 操作预览的“确认”是后端一次性令牌 + 显式 `approve` 标志，无法证明用户是否真的看过每一行；它保证的是“选择集合与确认绑定、执行前重新核验”，而不是法律意义上的逐条签收。
- 重复文件扫描结果保存在内存中（1 小时 TTL），应用重启后需要重新扫描；大目录的首轮哈希仍可能耗时数分钟。
- 内容索引只保存提取出的正文（上限 20 万字符），超出会标记为截断；扫描版 PDF 需要本机安装 Tesseract 才能索引文字。
- 搜索为关键词检索（无向量/语义），单字查询召回面较大；语义检索只预留了 `SearchBackend` 接口，尚未实现。
- 按内容批量分类是逐文件串行调用模型，只有整体预算与最终回退统计，没有逐条实时进度；预览面板会展示逐项依据与回退原因。
- 自动整理是进程内轮询（间隔 2 秒 + `settle_seconds`），应用退出后停止；跨盘移动期间该规则的轮询线程会阻塞，其它规则不受影响。
- 回收站只展示“操作日志中仍存在软删除记录”的条目，不提供永久删除或清空回收站。
- 界面已通过类型检查、纯函数单测和生产构建验证；本轮未执行真实 Electron GUI 的自动化点击 E2E。
