# 简历项目描述

## 推荐项目名称

**桌面智能文件管家（Desktop Smart File Butler）**  
Electron + React + TypeScript + FastAPI + LangGraph + SQLite

## 一句话介绍

面向本地文件整理场景的桌面 AI Agent：用户通过自然语言下达任务，LLM 负责理解意图和生成规则，后端 PolicyEngine 与确定性 Python 工具负责权限校验、批量文件匹配、执行、审批、审计和回滚，并通过 Electron sidecar 形成可发布的 Windows 桌面应用。

## 推荐简历版

- 基于 **Electron + React + FastAPI + LangGraph** 实现本地文件管理 Agent，构建 Perceive → Plan → Act → Reflect 工作流，支持扫描、分类、移动、重命名、摘要、软删除、定时任务及人工审批。
- 针对大目录中 LLM 无法可靠枚举文件的问题，设计 **Scan Manifest + Deterministic Batch Tool**：完整扫描结果持久化为 scan_id，模型只生成过滤规则，由 Python 对完整集合执行 batch_move / batch_rename / batch_classify，回归测试覆盖 200+ 文件批处理场景。
- 设计后端 **PolicyEngine + Sandbox + 审批/回滚** 安全链路，将原始用户意图与文件、OCR、工具结果等不可信数据分离；累计 mutation 跨 replan 保留，并通过目录级 PathLockManager 解决并发目标命名的 TOCTOU 风险。
- 完善桌面端可靠性与交付链：实现 WebSocket 断线后的 thread 状态 reconciliation、Runtime 退休/延迟释放、Electron single-instance、PyInstaller backend sidecar；GitHub Actions 在 Linux/Windows 自动执行测试、sidecar smoke test 和 portable EXE 构建。

## 后端开发岗位版

- 使用 FastAPI + SQLite 搭建本地服务层，提供 REST / WebSocket 接口，设计 thread 级任务状态、审批、取消、操作日志、定时任务和回滚机制。
- 为大批量文件任务设计 Manifest 持久化与确定性过滤，将自然语言规划和真实文件集合计算解耦，避免模型 preview 截断导致漏处理。
- 针对并发文件 mutation 实现按目录分片的锁管理器，采用固定顺序获取多目录锁，解决目标文件命名阶段的 TOCTOU 与潜在死锁问题。
- 通过 SQLite WAL、busy timeout、独立短连接和 LangGraph checkpoint 保证 API 线程、调度线程及 workflow 状态的本地持久化与并发访问。
- 实现 WebSocket 断线重连后的服务端状态 reconciliation，并处理 REST fallback 的 thread_id 连续性，避免任务已完成但客户端状态丢失。
- 使用 GitHub Actions 建立后端测试、前端类型/状态测试、Windows PyInstaller sidecar 与 Electron portable 构建流水线。

### 后端面试关键词

REST、WebSocket、状态机、SQLite WAL、并发控制、TOCTOU、状态恢复、任务生命周期、审计日志、CI/CD

## AI Agent / LLM 应用岗位版

- 基于 LangGraph 构建可恢复的本地 Agent 工作流，将感知、规划、执行、审批和反思节点显式建模，并使用 SQLite checkpoint 保存 thread 状态。
- 将 LLM 从“文件执行器”降级为“意图与规则决策器”：完整文件清单保存在 Manifest Store，LLM 只产生 scan_id + filter，真实集合由确定性 Python 代码计算。
- 设计 trusted_user_intent / untrusted_file_data / tool_observations 数据边界，防止文件正文、OCR 或工具结果中的 Prompt Injection 自动扩展用户授权。
- 将风险决策放到后端 PolicyEngine，避免依赖模型输出的风险标签；大批量 mutation 使用请求级累计策略，replan 不会重置安全额度。
- 对 OpenAI-Compatible 服务的 structured output 能力做错误分类，仅在明确不支持 tools、function calling 或 response format 时降级。
- 实现 Runtime retire 机制，使模型配置热更新只影响新 workflow，运行中的 stream 继续绑定原 Runtime，结束后再释放资源。

## 超精简版

**桌面智能文件管家｜Electron / FastAPI / LangGraph**

- 实现支持自然语言扫描、分类、移动、摘要、删除和回滚的本地文件 Agent。
- 设计 Manifest + Python 确定性批处理，解决 LLM 在 200+ 文件目录中 preview 截断和枚举不可靠问题。
- 建立 Sandbox、PolicyEngine、累计审批、Prompt Injection 信任边界与目录级并发锁。
- 完成 WS 状态恢复、Runtime 生命周期、PyInstaller sidecar 及 Windows Electron portable CI 构建。

## 30 秒口述版

这是一个本地文件管理 Agent，桌面端用 Electron 和 React，后端是 FastAPI，Agent 流程用 LangGraph。这个项目我主要不是在做一个“LLM 调文件 API”的 Demo，而是在解决 Agent 真正执行本地操作时的工程问题。比如大目录不能让模型枚举几百个文件，所以我做了 Scan Manifest，让模型只输出规则，Python 负责确定性匹配；高风险操作由后端 PolicyEngine 和人工审批控制；文件内容属于不可信输入，不能反过来扩展用户授权。另外还做了并发文件锁、断线状态恢复、回滚、Runtime 生命周期以及 Electron + PyInstaller 的完整打包和 CI。

## 1 分钟口述版

项目是一个基于 Electron、React、FastAPI 和 LangGraph 的本地文件管理 Agent。用户可以用自然语言要求它整理、分类、移动、重命名、摘要或删除文件。

我在实现过程中重点解决了三个问题。第一是大批量文件。最开始把扫描结果直接给 LLM，会因为上下文限制只看到一部分文件，所以后来改成 Scan Manifest，完整列表保存在 SQLite，LLM 只生成过滤规则，Python 根据 scan_id 对完整集合做确定性匹配。

第二是安全。我把原始用户意图和文件内容、OCR、工具结果分成 trusted 和 untrusted 两类数据，文件中的 Prompt Injection 不能作为授权来源；真正的批量操作、删除和 sandbox 校验由后端策略和代码控制。

第三是可靠性。项目处理了并发目标文件名的 TOCTOU、WebSocket 断线后的状态 reconciliation、运行中 Runtime 的生命周期，以及最终 Electron 打包。现在 CI 会在 Windows 上真实构建 PyInstaller sidecar 和 portable EXE，而不是只跑单测。

## STAR：大批量文件重构

**Situation**  
早期 Agent 扫描目录后，只能把有限文件信息重新交给 Planner。小目录没问题，但文件数量达到几百时，Planner 实际只看到 preview。

**Task**  
保证“整理全部 PDF”不会因为上下文裁剪而只处理前几十个文件。

**Action**  
引入持久化 Scan Manifest：扫描器保存完整集合，只向模型返回 scan_id、数量、类型分布和少量 preview；增加确定性 batch tools，根据模型生成的 filter 由 Python 匹配完整集合并执行。

**Result**  
批量行为从“依赖 LLM 枚举文件”变为“LLM 决策规则 + Python 确定集合”，并增加 200+ 文件场景的自动化回归测试。

## STAR：WebSocket 状态恢复

**Situation**  
WebSocket 自动重连后，连接虽然恢复，但断线期间的 done 或审批事件可能已经丢失，界面无法确认文件最终是否真的被修改。

**Task**  
让客户端恢复的不是“连接状态”，而是“任务真实状态”。

**Action**  
保留 WebSocket 实时事件，同时在重连后通过 GET /api/threads/{thread_id} 获取服务端 checkpoint 快照，再将 observations、pending approval 和终态重新映射到前端状态；REST fallback 也保存后端返回的 thread_id。

**Result**  
客户端可以在网络抖动后重新对齐任务状态，避免界面永久 busy 或已完成任务没有结果。

## 不建议在简历中写

- “生产级 AI Agent”
- “绝对安全的 Prompt Injection 防护”
- “支持无限规模文件”
- “分布式任务调度”
- “完整 Event Sourcing”
- “Windows 全平台生产验证完成”

更准确的说法：

- 面向本地桌面场景的安全 Agent 架构
- 建立 Prompt Injection 信任边界与后端授权策略
- 支持有上限、显式截断的大目录扫描
- 基于 APScheduler 的本地定时任务
- 基于 thread snapshot 的断线状态 reconciliation
- CI 可构建 Windows portable artifact

## 项目关键词

~~~text
Electron / React / TypeScript
FastAPI / WebSocket
LangGraph / LLM Agent
SQLite / WAL / Checkpoint
Policy Engine
Human-in-the-loop
Prompt Injection Defense
Sandbox
Manifest
Deterministic Batch Processing
Concurrency / TOCTOU
Rollback / Audit Log
PyInstaller Sidecar
GitHub Actions
~~~
