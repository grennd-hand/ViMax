import {request as httpRequest} from 'node:http';

export function proxyDialogue(request, response, url) {
  const backend = new URL(process.env.VIMAX_DIALOGUE_URL || 'http://127.0.0.1:9014');
  const pathname = url.pathname.replace(/^\/dialogue-api(?=\/|$)/, '/api');
  const outgoing = httpRequest(new URL(pathname + url.search, backend), {
    method: request.method,
    headers: {...request.headers, host: backend.host, connection: 'close'},
    agent: false,
  }, (incoming) => {
    response.writeHead(incoming.statusCode || 502, incoming.headers);
    incoming.pipe(response);
  });
  outgoing.on('error', () => {
    if (response.destroyed || response.writableEnded) return;
    if (!response.headersSent) response.writeHead(503, {'Content-Type': 'application/json; charset=utf-8'});
    response.end(JSON.stringify({detail: '暂时无法连接群聊服务，请检查本地服务是否运行。'}));
  });
  response.on('close', () => outgoing.destroy());
  request.pipe(outgoing);
}
