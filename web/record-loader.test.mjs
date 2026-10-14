import {expect,it,vi} from 'vitest';
import {createRecordLoader} from './record-loader.mjs';

function payload(status='completed') {
  return {id:'saved',status,config:{target_messages:150},messages:[{seq:1,content:'same content'}],template_id:'mother',template:{id:'mother',source:{messages:[{source_row:2,sender:'坐席',content:'source'}],accounts:[]},analysis:{evidence_rows:[2]}}};
}
it('opens a history in one request, reuses it within its lifetime, and refreshes on expiry or clear',async()=>{
  let current=0;
  const request=vi.fn(async()=>payload());
  const loader=createRecordLoader(request,{now:()=>current});
  const first=await loader.load('saved');
  expect(first.run.messages).toEqual(payload().messages);expect(first.template.evidence).toHaveLength(1);
  expect(request).toHaveBeenCalledTimes(1);
  expect(await loader.load('saved')).toBe(first);expect(request).toHaveBeenCalledTimes(1);
  current=60_001;await loader.load('saved');expect(request).toHaveBeenCalledTimes(2);
  loader.clear();await loader.load('saved');expect(request).toHaveBeenCalledTimes(3);
});
it.each(['running','paused'])('does not reuse mutable %s histories',async(status)=>{
  const request=vi.fn(async()=>payload(status));const loader=createRecordLoader(request);
  await loader.load('saved');await loader.load('saved');expect(request).toHaveBeenCalledTimes(2);
});
it('does not retain a response after switching away and aborting, even when transport finishes later',async()=>{
  let finish;
  const request=vi.fn(()=>new Promise(resolve=>{finish=resolve;}));
  const loader=createRecordLoader(request);const controller=new AbortController();
  const pending=loader.load('saved',{signal:controller.signal});
  controller.abort();finish(payload());await expect(pending).rejects.toMatchObject({name:'AbortError'});
  request.mockImplementation(async()=>payload());await loader.load('saved');expect(request).toHaveBeenCalledTimes(2);
});
it('propagates missing-history errors and permits a subsequent retry',async()=>{
  const request=vi.fn().mockRejectedValueOnce(new Error('找不到模拟')).mockResolvedValue(payload());
  const loader=createRecordLoader(request);await expect(loader.load('saved')).rejects.toThrow('找不到模拟');
  expect((await loader.load('saved')).run.id).toBe('saved');
});
it('can read a previous server response without an embedded template',async()=>{
  const value=payload();const {template,...run}=value;
  const request=vi.fn().mockResolvedValueOnce(run).mockResolvedValueOnce(template);
  expect((await createRecordLoader(request).load('saved')).template.id).toBe('mother');expect(request).toHaveBeenCalledTimes(2);
});
