import {createServer} from 'node:http';
import {scryptSync} from 'node:crypto';
import {afterEach, expect, it} from 'vitest';
import {createAuthentication} from './auth.mjs';

const salt = '0a'.repeat(16);
const hash = `${salt}:${scryptSync('test-password', Buffer.from(salt, 'hex'), 64).toString('hex')}`;
const services = [];
afterEach(async () => {await Promise.all(services.splice(0).map(server => new Promise(resolve => server.close(resolve))));});
async function fixture({secure = false, now = Date.now, required = true} = {}) {
  const auth = createAuthentication({VIMAX_AUTH_REQUIRED: required ? '1' : '0', VIMAX_AUTH_PASSWORD_HASH: hash, VIMAX_AUTH_SECURE_COOKIE: secure ? '1' : '0'}, now);
  const server = createServer(async (req, res) => {
    if (await auth.handle(req, res, new URL(req.url, 'http://' + req.headers.host))) return;
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({protected: true}));
  });
  services.push(server);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const login = (password = 'test-password', extra = {}) => fetch(base + '/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json', ...extra}, body: JSON.stringify({accessKey: password})});
  return {base, login};
}
it('requires private configuration when deployment authentication is enabled', () => {
  expect(() => createAuthentication({VIMAX_AUTH_REQUIRED: '1'})).toThrow(/configuration/);
});
it('serves the login shell and assets but protects both APIs without a browser auth challenge', async () => {
  const {base} = await fixture();
  for (const endpoint of ['/api/config', '/dialogue-api/simulations/test/export.xlsx']) {
    const response = await fetch(base + endpoint);
    expect(response.status).toBe(401); expect(response.headers.has('www-authenticate')).toBe(false);
    expect(await response.json()).not.toHaveProperty('protected');
  }
  const root = await fetch(base + '/?run=kept', {redirect: 'manual'});
  expect(root.status).toBe(302); expect(root.headers.get('location')).toBe('/login?next=%2F%3Frun%3Dkept');
  expect((await fetch(base + '/login')).status).toBe(200);
  expect((await fetch(base + '/assets/app.js')).status).toBe(200);
  expect(await (await fetch(base + '/auth/session')).json()).toMatchObject({required: true, authenticated: false});
});
it('issues an opaque secure cookie, rejects wrong access keys, and revokes sessions on logout', async () => {
  const {base, login} = await fixture({secure: true});
  expect((await login('wrong-password')).status).toBe(401);
  const response = await login(); expect(response.status).toBe(200);
  const setCookie = response.headers.get('set-cookie');
  expect(setCookie).toContain('__Host-vimax_session=');
  for (const value of ['HttpOnly', 'SameSite=Strict', 'Path=/', 'Secure', 'Max-Age=43200']) expect(setCookie).toContain(value);
  expect(setCookie).not.toContain('test-password');
  const cookie = setCookie.split(';')[0];
  expect(await (await fetch(base + '/auth/session', {headers: {cookie}})).json()).toMatchObject({authenticated: true});
  expect((await fetch(base + '/api/config', {headers: {cookie}})).status).toBe(200);
  const logout = await fetch(base + '/auth/logout', {method: 'POST', headers: {cookie}});
  expect(logout.status).toBe(200); expect(logout.headers.get('set-cookie')).toContain('Max-Age=0');
  expect((await fetch(base + '/api/config', {headers: {cookie}})).status).toBe(401);
});
it('expires sessions after 12 hours and rejects forged cookies', async () => {
  let current = 0;
  const {base, login} = await fixture({now: () => current});
  const cookie = (await login()).headers.get('set-cookie').split(';')[0];
  expect((await fetch(base + '/api/config', {headers: {cookie: cookie + 'x'}})).status).toBe(401);
  current = 12 * 60 * 60 * 1000;
  expect((await fetch(base + '/api/config', {headers: {cookie}})).status).toBe(401);
});
it('rejects cross-site mutations and limits repeated access key attempts', async () => {
  const {base, login} = await fixture();
  expect((await login('test-password', {Origin: 'https://other.example'})).status).toBe(403);
  expect((await fetch(base + '/auth/logout', {method: 'POST', headers: {'Sec-Fetch-Site': 'cross-site'}})).status).toBe(403);
  for (let index = 0; index < 30; index++) expect((await login('wrong')).status).toBe(401);
  const limited = await login(); expect(limited.status).toBe(429); expect(Number(limited.headers.get('retry-after'))).toBeGreaterThan(0);
});
it('retains local workbench access when authentication is not enabled', async () => {
  const {base} = await fixture({required: false});
  expect((await fetch(base + '/api/config')).status).toBe(200);
  expect(await (await fetch(base + '/auth/session')).json()).toMatchObject({required: false, authenticated: true});
});
