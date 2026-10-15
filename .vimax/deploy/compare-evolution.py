from pathlib import Path
import json,argparse,hashlib
p=argparse.ArgumentParser();p.add_argument('--version',type=int,required=True);p.add_argument('--revision-label');a=p.parse_args();out=Path(__file__).resolve().parents[2]/'outputs/自然群聊-20261015'
label=f'V{a.version:02d}'+('-'+a.revision_label if a.revision_label else '')
r=json.loads((out/label/'run.json').read_text(encoding='utf-8')); assert r['status']!='running' and len(r['messages'])==150
pid=r['version_record']['parent_run']; parents=[]
for f in out.glob('V*/run.json'):
 x=json.loads(f.read_text(encoding='utf-8'))
 if x['id']==pid:parents.append((f,x))
assert len(parents)==1
f,old=parents[0];assert len(old['messages'])==150
diff=[];same=[]
for x,y in zip(old['messages'],r['messages']):
 assert x['seq']==y['seq']
 if x==y:same.append(y['seq'])
 else:diff.append({'seq':y['seq'],'before':x,'after':y})
print(json.dumps({'version':a.version,'parent':str(f),'unchanged_count':len(same),'changed_sequences':[x['seq'] for x in diff],'total':len(same)+len(diff)},ensure_ascii=False))
for d in diff:
 x,y=d['before'],d['after'];print(f"{y['seq']:03d} {x['sender']}→{y['sender']} [{x.get('reply_to')}→{y.get('reply_to')}] {x['content']}\n => {y['content']}")
 print('other changes:',json.dumps({k:[x.get(k),y.get(k)] for k in set(x)|set(y) if k not in ('content','sender','seq','reply_to') and x.get(k)!=y.get(k)},ensure_ascii=False))
