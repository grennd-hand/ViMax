"""Exercise the public dialogue API with a caller-supplied workbook and goal."""
import argparse
import json
from pathlib import Path
import time
import httpx

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--goal',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--base-url',default='http://127.0.0.1:9014/api')
    p.add_argument('--participants',type=int,default=5)
    p.add_argument('--messages',type=int,default=150)
    p.add_argument('--model',default='gpt-5.5')
    p.add_argument('--run-id')
    a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    start=time.monotonic()
    with httpx.Client(timeout=240,trust_env=False) as c:
        def call(method,path,**kw):
            for attempt in range(4 if method=='GET' else 1):
                try:
                    r=c.request(method,a.base_url+path,**kw)
                    if r.status_code in {502,503,504} and method=='GET' and attempt<3:
                        time.sleep(2);continue
                    r.raise_for_status(); return r
                except httpx.TransportError:
                    if method!='GET' or attempt==3:raise
                    time.sleep(2)
        if a.run_id:
            run_id=a.run_id
        else:
            with a.source.open('rb') as f:
                imported=call('POST','/templates/upload',files={'file':(a.source.name,f,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}).json()
            (a.output/'source-import.json').write_text(json.dumps(imported,ensure_ascii=False,indent=2),encoding='utf-8')
            template_id=imported['id']
            goal=json.loads(a.goal.read_text(encoding='utf-8-sig'))
            confirmed=call('PUT',f'/templates/{template_id}/goal',json=goal).json()
            config={'template_id':template_id,'customer_count':a.participants,'target_messages':a.messages,
                    'allow_early_end':False,'language':'zh-CN','dialogue_model':a.model,
                    'memory_enabled':True,'story_world':'vimax-mother-20261014'}
            (a.output/'config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
            run_id=call('POST','/simulations',json=config).json()['id']
        (a.output/'run-id.txt').write_text(run_id,encoding='utf-8')
        previous=None
        while time.monotonic()-start < 1800:
            run=call('GET',f'/simulations/{run_id}').json()
            state=(run['status'],run['cursor'],run.get('review_progress',{}).get('phase'),len(run.get('vimax_calls',[])))
            if state!=previous:
                print(json.dumps({'id':run_id,'elapsed':round(time.monotonic()-start,1),'status':state[0],'count':state[1],'phase':state[2],'calls':state[3],'errors':run.get('errors',[])},ensure_ascii=False),flush=True)
                (a.output/'run.json').write_text(json.dumps(run,ensure_ascii=False,indent=2),encoding='utf-8')
                (a.output/'母本生成150条.md').write_text('\n\n'.join(f"{m['seq']:03d}　**{m['sender']}**：{m['content']}" for m in run['messages']),encoding='utf-8')
                previous=state
            if run['status'] not in {'running'}:
                if run['status']=='completed':
                    for fmt in ['xlsx','json']:
                        data=call('GET',f'/simulations/{run_id}/export/{fmt}').content
                        (a.output/f'母本生成150条.{fmt}').write_bytes(data)
                break
            time.sleep(5)
        else:
            call('POST',f'/simulations/{run_id}/pause')
            raise SystemExit('Acceptance timeout; run paused and retained')

if __name__=='__main__': main()
