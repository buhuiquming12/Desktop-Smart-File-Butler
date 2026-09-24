import { useCallback, useEffect, useState } from 'react';

import type { ApiClient } from '../api/client';
import type { SettingsTab } from '../components/Settings';
import type {
  LLMModelsRequest,
  LLMModelsResponse,
  LLMSettings,
  LLMSettingsUpdate,
  OperationLog,
  Preference,
  SandboxSettings,
  ScheduledJob,
  ToolSettings,
  ToolSettingsUpdate,
  WorkspaceSettings,
  WorkspaceSettingsUpdate,
} from '../types';

export function useSettingsController(
  api: ApiClient,
  refreshSetupCheck: () => Promise<void>,
) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<SettingsTab>('general');
  const [preferences, setPreferences] = useState<Preference[]>([]);
  const [llmSettings, setLLMSettings] = useState<LLMSettings | null>(null);
  const [sandboxSettings, setSandboxSettings] = useState<SandboxSettings | null>(null);
  const [workspaceSettings, setWorkspaceSettings] = useState<WorkspaceSettings | null>(null);
  const [toolSettings, setToolSettings] = useState<ToolSettings | null>(null);
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [logs, setLogs] = useState<OperationLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const results = await Promise.allSettled([
      api.getPreferences(),
      api.getJobs(),
      api.getOperations(),
      api.getLLMSettings(),
      api.getSandboxSettings(),
      api.getWorkspaceSettings(),
      api.getToolSettings(),
    ]);
    const [
      preferenceResult,
      jobResult,
      logResult,
      llmResult,
      sandboxResult,
      workspaceResult,
      toolResult,
    ] = results;
    if (preferenceResult.status === 'fulfilled') setPreferences(preferenceResult.value);
    if (jobResult.status === 'fulfilled') setJobs(jobResult.value);
    if (logResult.status === 'fulfilled') setLogs(logResult.value);
    if (llmResult.status === 'fulfilled') setLLMSettings(llmResult.value);
    if (sandboxResult.status === 'fulfilled') setSandboxSettings(sandboxResult.value);
    if (workspaceResult.status === 'fulfilled') setWorkspaceSettings(workspaceResult.value);
    if (toolResult.status === 'fulfilled') setToolSettings(toolResult.value);
    const rejected = results.find((result) => result.status === 'rejected');
    if (rejected?.status === 'rejected') {
      setError(rejected.reason instanceof Error ? rejected.reason.message : '部分数据加载失败');
    }
    setLoading(false);
  }, [api]);

  useEffect(() => {
    if (open) void load();
  }, [load, open]);

  const show = useCallback((nextTab: SettingsTab): void => {
    setTab(nextTab);
    setOpen(true);
  }, []);

  const saveLLM = useCallback(async (update: LLMSettingsUpdate): Promise<void> => {
    setError(null);
    try {
      const saved = await api.updateLLMSettings(update);
      setLLMSettings(saved);
      void refreshSetupCheck();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存模型配置失败');
      throw reason;
    }
  }, [api, refreshSetupCheck]);

  const fetchModels = useCallback(
    (request: LLMModelsRequest): Promise<LLMModelsResponse> => api.listLLMModels(request),
    [api],
  );

  const saveSandbox = useCallback(async (roots: string[]): Promise<void> => {
    setError(null);
    try {
      const saved = await api.updateSandboxSettings(roots);
      setSandboxSettings(saved);
      // 授权目录收紧可能让默认管理目录越界（后端会就地清除），这里同步最新状态。
      try {
        setWorkspaceSettings(await api.getWorkspaceSettings());
      } catch {
        // 默认目录状态拿不到不影响授权目录已保存这一事实，交给下次 load() 兜底。
      }
      void refreshSetupCheck();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存沙箱目录失败');
      throw reason;
    }
  }, [api, refreshSetupCheck]);

  const saveWorkspace = useCallback(async (update: WorkspaceSettingsUpdate): Promise<void> => {
    setError(null);
    try {
      const saved = await api.updateWorkspaceSettings(update);
      setWorkspaceSettings(saved);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存默认管理目录失败');
      throw reason;
    }
  }, [api]);

  /** 保存 OCR / 外部工具路径；返回体已带保存后重新检测的能力结果。 */
  const saveTools = useCallback(async (update: ToolSettingsUpdate): Promise<void> => {
    setError(null);
    try {
      const saved = await api.updateToolSettings(update);
      setToolSettings(saved);
      void refreshSetupCheck();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存外部工具配置失败');
      throw reason;
    }
  }, [api, refreshSetupCheck]);

  /** 重新检测 OCR 能力（不修改配置）。 */
  const refreshTools = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      setToolSettings(await api.getToolSettings());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重新检测 OCR 失败');
      throw reason;
    }
  }, [api]);

  const savePreference = useCallback(async (preference: Preference): Promise<void> => {
    setError(null);
    try {
      const saved = await api.updatePreference(preference);
      setPreferences((current) => [
        ...current.filter((item) => item.key !== saved.key),
        saved,
      ]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存偏好失败');
      throw reason;
    }
  }, [api]);

  const createJob = useCallback(async (job: Omit<ScheduledJob, 'job_id'>): Promise<void> => {
    setError(null);
    try {
      const saved = await api.createJob(job);
      setJobs((current) => [...current, saved]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建定时任务失败');
      throw reason;
    }
  }, [api]);

  const deleteJob = useCallback(async (jobId: string): Promise<void> => {
    setError(null);
    try {
      await api.deleteJob(jobId);
      setJobs((current) => current.filter((item) => item.job_id !== jobId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除定时任务失败');
    }
  }, [api]);

  const rollbackOperation = useCallback(async (opId: number): Promise<void> => {
    setError(null);
    try {
      await api.rollbackOperation(opId);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '撤销失败');
      throw reason;
    }
  }, [api, load]);

  return {
    open,
    tab,
    preferences,
    llmSettings,
    sandboxSettings,
    workspaceSettings,
    toolSettings,
    jobs,
    logs,
    loading,
    error,
    close: () => setOpen(false),
    show,
    load,
    saveLLM,
    fetchModels,
    saveSandbox,
    saveWorkspace,
    saveTools,
    refreshTools,
    savePreference,
    createJob,
    deleteJob,
    rollbackOperation,
  };
}
