import {useEffect, useState, type FormEvent} from 'react';
import {ArrowRight, Check, Eye, EyeOff, LockKeyhole, MessageSquare, Moon, Sun} from 'lucide-react';
import App from './App';
import {applyTheme, resolveTheme, THEME_STORAGE_KEY, type Theme} from './theme';
import './login.css';

type Session = {required: boolean; authenticated: boolean};

function nextPath() {
  const value = new URLSearchParams(location.search).get('next') || '/';
  try {
    const next = new URL(value, location.origin);
    return next.origin === location.origin && next.pathname !== '/login' ? next.pathname + next.search + next.hash : '/';
  } catch { return '/'; }
}

export default function AuthGate() {
  const [session, setSession] = useState<Session>();
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let disposed = false;
    void fetch('/auth/session', {cache: 'no-store'}).then(async response => {
      if (!response.ok) throw new Error('暂时无法连接工作台，请重试。');
      const result = await response.json() as Session;
      if (!disposed) {
        if (result.authenticated && location.pathname === '/login') window.history.replaceState(null, '', nextPath());
        setSession(result); setError('');
      }
    }).catch(() => !disposed && setError('暂时无法连接工作台，请重试。'));
    return () => { disposed = true; };
  }, [version]);

  useEffect(() => {
    if (!session?.required || !session.authenticated) return;
    const original = window.fetch;
    const wrapped: typeof fetch = async (input, init) => {
      const response = await original(input, init);
      const target = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin);
      if (response.status === 401 && target.origin === location.origin && (target.pathname.startsWith('/api/') || target.pathname.startsWith('/dialogue-api/'))) {
        setSession(current => current ? {...current, authenticated: false} : current);
      }
      return response;
    };
    window.fetch = wrapped;
    return () => { if (window.fetch === wrapped) window.fetch = original; };
  }, [session?.required, session?.authenticated]);

  async function loggedIn() {
    const response = await fetch('/auth/session', {cache: 'no-store'});
    if (!response.ok) throw new Error('无法确认登录状态，请重试。');
    const result = await response.json() as Session;
    if (!result.authenticated) throw new Error('登录状态未保存，请检查浏览器是否允许 Cookie。');
    if (location.pathname === '/login') window.history.replaceState(null, '', nextPath());
    setSession(result);
  }
  async function logout() {
    const response = await fetch('/auth/logout', {method: 'POST'});
    if (!response.ok) throw new Error('退出未完成，请重试。');
    window.history.replaceState(null, '', '/login');
    setSession(current => current ? {...current, authenticated: false} : current);
  }

  if (!session) return <div className="login-loading" role="status"><strong>ViMax</strong><span>{error || '正在连接工作台…'}</span>{error && <button onClick={() => {setError(''); setVersion(value => value + 1);}}>重新连接</button>}</div>;
  if (!session.authenticated) return <LoginPage onLogin={loggedIn} />;
  return <App authenticated={session.required} onLogout={session.required ? logout : undefined} />;
}

function LoginPage({onLogin}: {onLogin: () => Promise<void>}) {
  const [accessKey, setAccessKey] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [theme, setTheme] = useState<Theme>(() => resolveTheme(document.documentElement.dataset.theme, false));
  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next); applyTheme(next);
    try {localStorage.setItem(THEME_STORAGE_KEY, next);} catch { /* Theme works without storage. */ }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try {
      const response = await fetch('/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({accessKey})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '登录未完成，请重试。');
      await onLogin();
    } catch (cause) {setError(cause instanceof Error ? cause.message : '网络连接失败，请重试。');}
    finally {setBusy(false);}
  }
  return <main className="login-page">
    <section className="login-story" aria-label="ViMax 群聊创作">
      <a className="login-brand" href="/login" aria-label="ViMax 登录首页"><MessageSquare size={25} strokeWidth={1.8} /><strong>ViMax</strong><span>群聊创作工作台</span></a>
      <div className="login-story-content">
        <h1>让每个人物，<br />都有自己的话。</h1>
        <p className="login-story-intro">从一份母本开始，写出有来有往的群聊。</p>
        <div className="login-preview" aria-label="群聊创作示意">
          <div className="login-preview-heading"><span><i />群聊创作预览</span><MessageSquare size={16} /></div>
          <div className="login-chat"><span className="login-chat-avatar">小</span><div><small>小雨</small><p>刚进来，这个群是聊什么的呀？</p></div></div>
          <div className="login-chat"><span className="login-chat-avatar">阿</span><div><small>阿辰</small><p>我也刚来，先找个位置坐坐。</p></div></div>
          <div className="login-chat login-chat-self"><div><small>林林</small><p>哈哈，那我先把咖啡放下。</p></div><span className="login-chat-avatar">林</span></div>
          <div className="login-preview-foot"><Check size={14} /><span>每个角色，都有自己的节奏</span></div>
        </div>
      </div>
      <div className="login-story-footer"><span>母本导入</span><ArrowRight size={13} /><span>角色设置</span><ArrowRight size={13} /><span>群聊生成</span><ArrowRight size={13} /><span>检查与导出</span></div>
    </section>
    <section className="login-entry" aria-labelledby="login-title">
      <button className="login-theme" type="button" onClick={toggleTheme} aria-label={theme === 'dark' ? '使用浅色模式' : '使用深色模式'}>{theme === 'dark' ? <Sun size={19} /> : <Moon size={19} />}</button>
      <div className="login-form-wrap">
        <div className="login-mobile-brand"><MessageSquare size={24} /><strong>ViMax</strong></div>
        <span className="login-welcome">继续你的创作</span>
        <h2 id="login-title">欢迎回来</h2>
        <p className="login-description">输入访问密钥，打开你的群聊剧本工作台。</p>
        <form onSubmit={submit} className="login-form">
          <div className="login-field"><label htmlFor="login-password">访问密钥</label><div className="login-password"><input id="login-password" name="accessKey" type={visible ? 'text' : 'password'} autoComplete="current-password" placeholder="请输入访问密钥" required maxLength={1024} value={accessKey} onChange={event => setAccessKey(event.target.value)} disabled={busy} aria-describedby={error ? 'login-error' : undefined} /><button type="button" aria-label={visible ? '隐藏密钥' : '显示密钥'} aria-pressed={visible} onClick={() => setVisible(value => !value)}>{visible ? <EyeOff size={19} /> : <Eye size={19} />}</button></div></div>
          <div className="login-feedback" aria-live="polite">{error && <p id="login-error" role="alert">{error}</p>}</div>
          <button className="login-submit" type="submit" disabled={busy}>{busy ? '正在登录…' : '进入工作台'}{busy ? <span className="login-spinner" /> : <ArrowRight size={18} />}</button>
          <p className="login-session-note"><LockKeyhole size={14} />登录状态保留 12 小时，可随时退出。</p>
        </form>
      </div>
      <footer className="login-entry-footer">ViMax · 让对话自然发生</footer>
    </section>
  </main>;
}
