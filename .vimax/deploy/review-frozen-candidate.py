"""Read-only ViMax whole-dialogue diagnostic; original run never changes.
Includes reply_to/delay evidence omitted by the current production review payload.
This is a separately labelled diagnostic, not a passed program-review state.
"""
import argparse,asyncio,hashlib,importlib.util,json,re,subprocess,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('evolution_review_base',HERE/'evolve-natural.py');base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
p=argparse.ArgumentParser();p.add_argument('--version',required=True);p.add_argument('--label',required=True);a=p.parse_args()
assert re.fullmatch(r'V\d{2}(?:-R\d{3})?',a.version) and re.fullmatch(r'[a-z0-9-]+',a.label)
source=base.ROOT/'outputs/自然群聊-20261015'/a.version;raw=(source/'run.json').read_bytes();run=json.loads(raw);template=json.loads((source/'template.json').read_bytes());assert len(run['messages'])==150 and run['status']!='running'
out=source.parent/'comparative-reviews'/f'{a.version}-{a.label}';out.mkdir(exist_ok=False)
base.configure_environment(out)
from app import main as api
original=api.model_json
original_stream=api.stream_model_content
stream_attempts=[]
async def traced_stream(*args,**kwargs):
 started=time.monotonic()
 try:
  response,content=await original_stream(*args,**kwargs)
  stream_attempts.append({'elapsed_seconds':round(time.monotonic()-started,2),'http_status':response.status_code,'content_length':len(content or '')})
  return response,content
 except Exception as exc:
  stream_attempts.append({'elapsed_seconds':round(time.monotonic()-started,2),'error_type':type(exc).__name__,'error':str(exc)[:400]})
  raise
 finally:base.save_json(out/'stream-diagnostics.json',stream_attempts)
api.stream_model_content=traced_stream
async def recorded(system,payload,**kwargs):
 byseq={m['seq']:m for m in run['messages']}
 if 'messages' in payload:
  payload={**payload,'messages':[{**m,'reply_to':byseq[m['seq']].get('reply_to'),'delay':byseq[m['seq']].get('delay')} for m in payload['messages']]}
 base.save_json(out/'request.json',{'system':system,'payload':payload,'settings':kwargs,'model':'gpt-6-sol','reasoning_effort':'high','reply_context_augmented':True})
 result=await original(system,payload,**kwargs);base.save_json(out/'raw-response.json',result);return result
api.model_json=recorded
async def main():
 before=base.json_bytes(run);token=api._run_reasoning_effort.set('high')
 proof={'version':a.version,'source_run_sha256':hashlib.sha256(raw).hexdigest(),'message_sha256':base.dialogue_fingerprint(run),'model':'gpt-6-sol','reasoning_effort':'high','original_status':run['status'],'diagnostic_only':True,'reply_context_augmented':True,'reviewer_source_sha256':hashlib.sha256(Path(api.__file__).read_bytes()).hexdigest()}
 base.save_json(out/'result.json',{**proof,'status':'running'})
 try:
  result=await api.review_dialogue(template,run)
  assert base.json_bytes(run)==before and (source/'run.json').read_bytes()==raw
  base.save_json(out/'result.json',{**proof,'status':'completed','review':result})
  print(json.dumps({'version':a.version,'diagnostic':True,'review':result},ensure_ascii=False))
 except Exception as exc:
  assert base.json_bytes(run)==before and (source/'run.json').read_bytes()==raw
  base.save_json(out/'result.json',{**proof,'status':'failed','error_type':type(exc).__name__,'error':str(exc)[:400],'stream_attempts':stream_attempts})
  raise
 finally:api._run_reasoning_effort.reset(token)
asyncio.run(main())
