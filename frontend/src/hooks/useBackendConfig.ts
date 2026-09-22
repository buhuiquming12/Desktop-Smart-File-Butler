import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  ApiClient,
  defaultApiBase,
  normalizeApiBase,
  normalizeWsBase,
  wsBaseFromApi,
} from '../api/client';
import type { ToastKind } from '../components/ToastStack';
import type { BackendConfig, SetupHints } from '../types';

type Notify = (content: string, kind?: ToastKind) => void;

function initialApiBase(): string {
  const fallback = defaultApiBase();
  const stored = localStorage.getItem('file-butler-api-base');
  if (!stored) return fallback;
  try { return normalizeApiBase(stored); } catch { return fallback; }
}

function initialWsBase(apiBase: string): string {
  const stored = localStorage.getItem('file-butler-ws-base');
  if (stored) {
    try { return normalizeWsBase(stored); } catch { /* derive from REST below */ }
  }
  return wsBaseFromApi(apiBase);
}

function ensureClientId(): string {
  const current = localStorage.getItem('file-butler-client-id');
  if (current) return current;
  const random = Math.random().toString(36).slice(2, 10);
  const value = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `client-${Date.now().toString(36)}-${random}`;
  localStorage.setItem('file-butler-client-id', value);
  return value;
}

export function useBackendConfig(notify: Notify) {
  const clientId = useMemo(ensureClientId, []);
  const [apiBase, setApiBase] = useState(initialApiBase);
  const [wsBase, setWsBase] = useState(() => initialWsBase(initialApiBase()));
  const [setupHints, setSetupHints] = useState<SetupHints>({
    checked: false,
    backendOk: false,
    modelOk: null,
    sandboxOk: null,
    ocrOk: null,
  });
  const api = useMemo(() => new ApiClient(apiBase), [apiBase]);

  const refreshSetupCheck = useCallback(async () => {
    let backendOk = false;
    try {
      const health = await api.getHealth();
      backendOk = health?.status === 'ok';
    } catch {
      backendOk = false;
    }
    let config: BackendConfig | null = null;
    try {
      config = await api.getConfig();
    } catch {
      config = null;
    }
    setSetupHints({
      checked: true,
      backendOk,
      modelOk: config === null
        ? null
        : config.model_provider === 'ollama'
          ? Boolean(config.ollama_model)
          : Boolean(config.openai_model) && config.openai_api_key_set !== false,
      sandboxOk: config === null
        ? null
        : Array.isArray(config.sandbox_roots) && config.sandbox_roots.length > 0,
      ocrOk: config === null ? null : Boolean(config.ocr_enabled),
    });
  }, [api]);

  useEffect(() => {
    void refreshSetupCheck();
  }, [refreshSetupCheck]);

  const saveEndpoints = useCallback((newApiBase: string, newWsBase: string): void => {
    if (!newApiBase || !newWsBase) return;
    try {
      const apiEndpoint = normalizeApiBase(newApiBase);
      const wsEndpoint = normalizeWsBase(newWsBase);
      localStorage.setItem('file-butler-api-base', apiEndpoint);
      localStorage.setItem('file-butler-ws-base', wsEndpoint);
      setApiBase(apiEndpoint);
      setWsBase(wsEndpoint);
      notify('连接地址已保存，正在重连…');
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : '连接地址格式错误', 'error');
    }
  }, [notify]);

  return {
    api,
    apiBase,
    wsBase,
    clientId,
    setupHints,
    refreshSetupCheck,
    saveEndpoints,
  };
}
