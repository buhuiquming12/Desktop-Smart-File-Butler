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
    ]);
    const [preferenceResult, jobResult, logResult, llmResult, sandboxResult] = results;
    if (preferenceResult.status === 'fulfilled') setPreferences(preferenceResult.value);
    if (jobResult.status === 'fulfilled') setJobs(jobResult.value);
    if (logResult.status === 'fulfilled') setLogs(logResult.value);
    if (llmResult.status === 'fulfilled') setLLMSettings(llmResult.value);
    if (sandboxResult.status === 'fulfilled') setSandboxSettings(sandboxResult.value);
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
      void refreshSetupCheck();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存沙箱目录失败');
      throw reason;
    }
  }, [api, refreshSetupCheck]);

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
    savePreference,
    createJob,
    deleteJob,
    rollbackOperation,
  };
}
