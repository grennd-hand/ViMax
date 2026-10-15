import {useCallback, useEffect, useRef, useState} from 'react';
import {Files, FolderPlus, LogOut, Menu, Moon, PanelLeftClose, PanelLeftOpen, Save, Settings, Sun, X} from 'lucide-react';
import {getAgentConfig, getSessions, saveAgentConfig} from './api';
import DialogueWorkspace from './DialogueWorkspace';
import './dialogue.css';
import {applyTheme, resolveTheme, THEME_STORAGE_KEY, type Theme} from './theme';
import type {AgentConfig, ConfigSection, SessionSummary} from './types';

type WorkspaceView = 'dialogue' | 'settings';

export default function App({authenticated, onLogout}: {authenticated?: boolean; onLogout?: () => Promise<void>}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [dialogueRunId, setDialogueRunId] = useState(new URLSearchParams(location.search).get('run') || '');
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>('dialogue');
  const [freshProject, setFreshProject] = useState(false);
  const [projectVersion, setProjectVersion] = useState(0);
  const [selectionVersion, setSelectionVersion] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [theme, setTheme] = useState<Theme>(() => resolveTheme(document.documentElement.dataset.theme, false));
  const [loadError, setLoadError] = useState('');
  const workspaceContent = useRef<HTMLDivElement>(null);

  useEffect(() => {
    applyTheme(theme);
    try { window.localStorage.setItem(THEME_STORAGE_KEY, theme); } catch { /* Theme remains usable. */ }
  }, [theme]);

  const refreshSessions = useCallback(async () => {
    const state = await getSessions();
    const projects = state.sessions.filter((item) => item.dialogueRunId);
    setSessions(projects);
    return {state, projects};
  }, []);

  useEffect(() => {
    let cancelled = false;
    void refreshSessions().then(({state, projects}) => {
      if (cancelled || new URLSearchParams(location.search).get('run')) return;
      const savedId = window.localStorage.getItem('vimax-dialogue-run-id');
      const previous = projects.find((item) => item.dialogueRunId === savedId)
        || projects.find((item) => item.sessionId === state.activeSessionId) || projects[0];
      if (previous?.dialogueRunId) setDialogueRunId(previous.dialogueRunId);
    }).catch((error) => !cancelled && setLoadError(String(error.message || error)));
    return () => { cancelled = true; };
  }, [refreshSessions]);

  function openProject(id: string) {
    workspaceContent.current?.scrollTo({top: 0, behavior: 'auto'});
    setSelectionVersion(value => value + 1);
    setDialogueRunId(id);
    setFreshProject(false);
    setWorkspaceView('dialogue');
    setMobileSidebarOpen(false);
    window.history.replaceState(null, '', `/?run=${encodeURIComponent(id)}`);
  }

  function newProject() {
    setDialogueRunId('');
    setFreshProject(true);
    setProjectVersion((value) => value + 1);
    setWorkspaceView('dialogue');
    setMobileSidebarOpen(false);
    window.localStorage.removeItem('vimax-dialogue-run-id');
    window.history.replaceState(null, '', '/');
  }

  function runChanged(id: string) {
    setDialogueRunId(id);
    window.history.replaceState(null, '', `/?run=${encodeURIComponent(id)}`);
    void refreshSessions().catch((error) => setLoadError(String(error.message || error)));
  }

  return <div className={`app-shell ${sidebarOpen ? 'sidebar-open' : 'sidebar-collapsed'}`}>
    {mobileSidebarOpen && <button className="sidebar-scrim" onClick={() => setMobileSidebarOpen(false)} aria-label="关闭导航" />}
    <aside className={`sidebar ${sidebarOpen ? 'is-open' : 'is-collapsed'} ${mobileSidebarOpen ? 'is-mobile-open' : ''}`}>
      <div className="sidebar-brand">
        <strong>ViMax</strong>
        <button className="icon-button sidebar-collapse desktop-only" onClick={() => setSidebarOpen(false)} aria-label="收起导航"><PanelLeftClose size={17} /></button>
        <button className="icon-button mobile-only" onClick={() => setMobileSidebarOpen(false)} aria-label="关闭导航"><X size={18} /></button>
      </div>
      <nav className="primary-nav" aria-label="主导航">
        <button className={workspaceView === 'dialogue' ? 'is-active' : ''} onClick={() => { setWorkspaceView('dialogue'); setMobileSidebarOpen(false); }}><Files size={17} /><span>群聊剧本</span></button>
        <button onClick={newProject}><FolderPlus size={17} /><span>新建剧本</span></button>
        <button className={workspaceView === 'settings' ? 'is-active' : ''} onClick={() => { setWorkspaceView('settings'); setMobileSidebarOpen(false); }}><Settings size={17} /><span>模型设置</span></button>
      </nav>
      <div className="session-section">
        <div className="section-label"><span>剧本记录</span><span>{sessions.length}</span></div>
        <div className="session-list">
          {sessions.map((session) => <div key={session.sessionId} className={`session-item ${session.dialogueRunId === dialogueRunId ? 'is-selected' : ''}`}>
            <button className="session-open" onClick={() => openProject(session.dialogueRunId!)}>
              <span className="session-copy"><strong>{session.projectName || '母本群聊'}</strong><small>{relativeTime(session.updatedAt)} · {stageLabel(session.stage)}</small></span>
            </button>
          </div>)}
          {!sessions.length && <span className="empty-list">暂无剧本</span>}
        </div>
      </div>
      <div className="sidebar-footer"><span className="avatar">V</span><div><strong>群聊创作工作台</strong><small>{authenticated ? '访问密钥已验证' : '本地 · ViMax'}</small></div></div>
    </aside>
    <main className="workspace-main">
      <header className="workspace-header">
        <div className="workspace-title-row">
          <button className="icon-button mobile-only" onClick={() => setMobileSidebarOpen(true)} aria-label="展开导航"><Menu size={18} /></button>
          {!sidebarOpen && <button className="icon-button desktop-only" onClick={() => setSidebarOpen(true)} aria-label="展开导航"><PanelLeftOpen size={18} /></button>}
          <strong>{workspaceView === 'settings' ? '模型设置' : '群聊剧本'}</strong>
        </div>
        <div style={{display: 'flex', gap: 10}}><ThemeToggle theme={theme} onToggle={() => setTheme(theme === 'dark' ? 'light' : 'dark')} />
          {onLogout && <button className="icon-button" aria-label="退出登录" title="退出登录" onClick={() => void onLogout().catch(error => setLoadError(String(error.message || error)))}><LogOut size={18} /></button>}
        </div>
      </header>
      {loadError && <div className="load-error" role="alert">{loadError}<button onClick={() => setLoadError('')} aria-label="关闭错误提示"><X size={15} /></button></div>}
      <div ref={workspaceContent} key={projectVersion} style={{display: workspaceView === 'dialogue' ? undefined : 'none', flex: 1, minHeight: 0, overflow: 'auto'}}>
        <DialogueWorkspace runId={dialogueRunId} startFresh={freshProject} selectionVersion={selectionVersion} onRunChanged={runChanged} />
      </div>
      {workspaceView === 'settings' && <SettingsView />}
    </main>
  </div>;
}

const CONFIG_SECTIONS: Array<{key: 'llm'; title: string; description: string}> = [
  {key: 'llm', title: '创作语言模型', description: '用于母本分析、人物设置、群聊编写和检查'},
];

function stageLabel(stage: string) {
  const labels: Record<string, string> = {created: '已创建', running: '生成中', paused: '已暂停', completed: '已完成', failed: '失败', early_end: '已结束'};
  return labels[stage] || stage || '已创建';
}

function ThemeToggle({theme, onToggle}: {theme: Theme; onToggle: () => void}) {
  const dark = theme === 'dark';
  return (
    <button
      type="button"
      className="icon-button theme-toggle"
      onClick={onToggle}
      aria-label={dark ? '使用浅色模式' : '使用深色模式'}
      aria-pressed={dark}
      title={dark ? '浅色模式' : '深色模式'}
    >
      {dark ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  );
}

function SettingsView() {
  const [config, setConfig] = useState<AgentConfig>();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState('');

  useEffect(() => {
    let cancelled = false;
    void getAgentConfig()
      .then((payload) => !cancelled && setConfig(payload))
      .catch((error) => !cancelled && setStatus(error instanceof Error ? error.message : String(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, []);

  function update(section: keyof AgentConfig['sections'], field: keyof ConfigSection, value: string) {
    setStatus('');
    setConfig((current) => current ? {
      sections: {
        ...current.sections,
        [section]: {...current.sections[section], [field]: value},
      },
    } : current);
  }

  async function save() {
    if (!config || saving) return;
    setSaving(true);
    setStatus('');
    try {
      setConfig(await saveAgentConfig({sections: {llm: config.sections.llm}}));
      setStatus('已保存');
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="settings-loading">正在读取配置…</div>;
  if (!config) return <div className="settings-loading is-error">{status || '无法读取配置'}</div>;
  return (
    <section className="settings-view">
      <header>
        <div><span>本地配置</span><h1>设置</h1></div>
        <div className="settings-save-group">
          {status && <span className={status === '已保存' ? 'is-saved' : 'is-error'}>{status}</span>}
          <button className="settings-save" onClick={() => void save()} disabled={saving}>
            <Save size={15} />{saving ? '正在保存…' : '保存'}
          </button>
        </div>
      </header>
      <div className="settings-sections">
        {CONFIG_SECTIONS.map((definition) => (
          <ConfigSectionEditor
            key={definition.key}
            definition={definition}
            value={config.sections[definition.key]}
            onChange={(field, value) => update(definition.key, field, value)}
          />
        ))}
      </div>
    </section>
  );
}

function ConfigSectionEditor({definition, value, onChange}: {
  definition: {title: string; description: string};
  value: ConfigSection;
  onChange: (field: keyof ConfigSection, value: string) => void;
}) {
  return (
    <section className="config-section">
      <header><h2>{definition.title}</h2><p>{definition.description}</p></header>
      <div className="config-fields">
        {value.model_provider !== undefined && (
          <label><span>模型供应商</span><input value={value.model_provider} onChange={(event) => onChange('model_provider', event.target.value)} /></label>
        )}
        <label><span>模型</span><input value={value.model} onChange={(event) => onChange('model', event.target.value)} /></label>
        <label><span>推理强度</span><select value={value.reasoning_effort || ''} onChange={(event) => onChange('reasoning_effort', event.target.value)}><option value="">模型默认</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label>
        <label className="config-field-wide"><span>接口地址</span><input value={value.base_url} onChange={(event) => onChange('base_url', event.target.value)} inputMode="url" /></label>
        <label className="config-field-wide">
          <span>API 密钥 <i className={value.has_api_key ? 'is-configured' : ''}>{value.has_api_key ? '已配置' : '未配置'}</i></span>
          <input type="password" value={value.api_key} onChange={(event) => onChange('api_key', event.target.value)} placeholder={value.has_api_key ? '留空则沿用当前密钥' : '输入 API 密钥'} autoComplete="off" />
        </label>
      </div>
    </section>
  );
}

function relativeTime(value: string) {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return '最近';
  const delta = Math.max(0, Date.now() - timestamp);
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  return `${Math.floor(hours / 24)}天前`;
}
