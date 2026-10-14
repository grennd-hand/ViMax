import type {AgentConfig, SessionSummary} from './types';

export async function getSessions() {
  return request<{activeSessionId: string; sessions: SessionSummary[]}>('/api/sessions');
}

export async function getAgentConfig() {
  return request<AgentConfig>('/api/config');
}

export async function saveAgentConfig(config: {sections: Partial<AgentConfig['sections']>}) {
  return request<AgentConfig>('/api/config', {method: 'PUT', body: JSON.stringify(config)});
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {'Content-Type': 'application/json', ...(init.headers || {})},
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求未完成（HTTP ${response.status}）`);
  return payload as T;
}
