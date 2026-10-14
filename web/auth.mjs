import {randomBytes, scrypt, timingSafeEqual} from 'node:crypto';
import {promisify} from 'node:util';

const derive = promisify(scrypt);
const lifetime = 12 * 60 * 60 * 1000;

export function createAuthentication(env = process.env, now = Date.now) {
  const required = env.VIMAX_AUTH_REQUIRED === '1';
  const username = env.VIMAX_ACCESS_USER || '';
  const [salt, encoded] = (env.VIMAX_AUTH_PASSWORD_HASH || '').split(':');
  if (required && (!username || !/^[a-f0-9]{32}$/.test(salt || '') || !/^[a-f0-9]{128}$/.test(encoded || ''))) {
    throw new Error('Authentication is required but its private configuration is missing or invalid');
  }
  const sessions = new Map();
  const attempts = new Map();
  const secure = env.VIMAX_AUTH_SECURE_COOKIE === '1';
  const cookieName = secure ? '__Host-vimax_session' : 'vimax_session';
  const cookie = (token, maxAge) => `${cookieName}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAge}${secure ? '; Secure' : ''}`;
  const json = (response, status, payload) => {
    response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
    response.end(JSON.stringify(payload));
  };
  const tokenFrom = (request) => (request.headers.cookie || '').split(';').map(part => part.trim()).find(part => part.startsWith(`${cookieName}=`))?.slice(cookieName.length + 1);
  const authenticated = (request) => {
    if (!required) return true;
    const token = tokenFrom(request);
    if (!token) return false;
    const expires = sessions.get(token);
    if (!expires || expires <= now()) { sessions.delete(token); return false; }
    return true;
  };
  const sameOrigin = (request) => {
    if (request.headers['sec-fetch-site'] === 'cross-site') return false;
    if (!request.headers.origin) return true;
    try {
      const origin = new URL(request.headers.origin);
      return origin.host === request.headers.host && (!secure || origin.protocol === 'https:');
    } catch { return false; }
  };

  return {
    async handle(request, response, url) {
      if (url.pathname === '/auth/session' && request.method === 'GET') {
        json(response, 200, {required, authenticated: authenticated(request), username: authenticated(request) && required ? username : ''});
        return true;
      }
      if (!required) return false;
      if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method) && !sameOrigin(request)) {
        json(response, 403, {error: '请在本站页面中操作。'}); return true;
      }
      if (url.pathname === '/auth/login' && request.method === 'POST') {
        if (!(request.headers['content-type'] || '').startsWith('application/json')) {
          json(response, 415, {error: '请使用登录表单提交。'}); return true;
        }
        for (const [key, value] of attempts) if (value.until <= now()) attempts.delete(key);
        const peer = request.socket.remoteAddress || 'unknown';
        const current = attempts.get(peer) || {count: 0, until: now() + 15 * 60 * 1000};
        if (current.count >= 30 || attempts.size >= 1000 && !attempts.has(peer)) {
          response.setHeader('Retry-After', String(Math.max(1, Math.ceil((current.until - now()) / 1000))));
          json(response, 429, {error: '尝试次数过多，请稍后再试。'}); return true;
        }
        let body;
        try {
          const chunks = []; let size = 0;
          for await (const chunk of request) {
            size += chunk.length;
            if (size > 4096) { json(response, 413, {error: '登录信息过长。'}); return true; }
            chunks.push(chunk);
          }
          body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (!body || typeof body.username !== 'string' || typeof body.password !== 'string' || body.password.length > 1024) throw new Error();
        } catch { json(response, 400, {error: '请填写账号和密码。'}); return true; }
        // Count pending attempts too, so parallel requests cannot bypass the limit.
        current.count += 1; attempts.set(peer, current);
        const actual = await derive(body.password, Buffer.from(salt, 'hex'), 64);
        if (!timingSafeEqual(actual, Buffer.from(encoded, 'hex')) || body.username !== username) {
          json(response, 401, {error: '账号或密码不正确，请重新输入。'}); return true;
        }
        for (const [key, expires] of sessions) if (expires <= now()) sessions.delete(key);
        if (sessions.size >= 1000) { json(response, 503, {error: '当前登录人数过多，请稍后再试。'}); return true; }
        attempts.delete(peer);
        const token = randomBytes(32).toString('hex'); sessions.set(token, now() + lifetime);
        response.setHeader('Set-Cookie', cookie(token, lifetime / 1000));
        json(response, 200, {ok: true}); return true;
      }
      if (url.pathname === '/auth/logout' && request.method === 'POST') {
        sessions.delete(tokenFrom(request));
        response.setHeader('Set-Cookie', cookie('', 0));
        json(response, 200, {ok: true}); return true;
      }
      const publicPage = request.method === 'GET' || request.method === 'HEAD';
      if (publicPage && (url.pathname === '/login' || url.pathname.startsWith('/assets/') || url.pathname === '/favicon.ico')) return false;
      if (authenticated(request)) { response.setHeader('Cache-Control', 'no-store'); return false; }
      if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/dialogue-api/') || url.pathname.startsWith('/auth/')) {
        json(response, 401, {error: '登录已过期，请重新登录。'});
      } else {
        const next = `${url.pathname}${url.search}`;
        response.writeHead(302, {Location: '/login?next=' + encodeURIComponent(next), 'Cache-Control': 'no-store'}); response.end();
      }
      return true;
    },
  };
}
