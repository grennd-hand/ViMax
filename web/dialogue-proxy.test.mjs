import {createServer} from 'node:http';
import {afterEach,expect,it} from 'vitest';
import {proxyDialogue} from './dialogue-proxy.mjs';

const services=[];
const previous=process.env.VIMAX_DIALOGUE_URL;
afterEach(async()=>{
  if(previous===undefined)delete process.env.VIMAX_DIALOGUE_URL;else process.env.VIMAX_DIALOGUE_URL=previous;
  await Promise.all(services.splice(0).map(server=>new Promise(resolve=>server.close(resolve))));
});
async function listen(handler){
  const server=createServer(handler);services.push(server);
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return `http://127.0.0.1:${server.address().port}`;
}
it('proxies the original API path and binary upload without changing its contents',async()=>{
  let observed;
  process.env.VIMAX_DIALOGUE_URL=await listen(async(req,res)=>{
    const chunks=[];for await(const chunk of req)chunks.push(chunk);
    observed={path:req.url,body:Buffer.concat(chunks)};
    res.writeHead(200,{'Content-Type':'application/json'});res.end('{"ok":true}');
  });
  const front=await listen((req,res)=>proxyDialogue(req,res,new URL(req.url,'http://localhost')));
  const data=Buffer.from([0,255,42,13,10]);
  const response=await fetch(front+'/dialogue-api/templates/upload?source=test',{method:'POST',body:data});
  expect(await response.json()).toEqual({ok:true});
  expect(observed.path).toBe('/api/templates/upload?source=test');
  expect(observed.body).toEqual(data);
});
it('preserves event-stream responses for live dialogue progress',async()=>{
  process.env.VIMAX_DIALOGUE_URL=await listen((req,res)=>{
    res.writeHead(200,{'Content-Type':'text/event-stream'});res.end('data: {"cursor":12}\n\n');
  });
  const front=await listen((req,res)=>proxyDialogue(req,res,new URL(req.url,'http://localhost')));
  const response=await fetch(front+'/dialogue-api/simulations/test/events');
  expect(response.headers.get('content-type')).toBe('text/event-stream');
  expect(await response.text()).toBe('data: {"cursor":12}\n\n');
});
