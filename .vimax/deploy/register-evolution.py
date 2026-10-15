from pathlib import Path
import argparse,hashlib,json,os,uuid,re
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/自然群聊-20261015'
def atomic(path,value):
 payload=json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8');temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp');temp.write_bytes(payload);os.replace(temp,path)
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
p=argparse.ArgumentParser();p.add_argument('--version',type=int,required=True);p.add_argument('--revision-label');p.add_argument('--verdict',choices=['candidate','needs_revision','alternative'],required=True);p.add_argument('--summary',required=True);args=p.parse_args()
assert 4<=args.version<=40
v=f'V{args.version:02d}'+('-'+args.revision_label if args.revision_label else '');folder=OUT/v
r=json.loads((folder/'run.json').read_text(encoding='utf-8'))
assert r['status']!='running' and len(r['messages'])==150
fields=('seq','sender','content','reply_to','delay','stage','type','image_code','image_mode','replace_date')
hash_=hashlib.sha256(json.dumps([{k:x.get(k) for k in fields} for x in r['messages']],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
record=next(x for x in r['candidate_versions'] if x['hash']==hash_)
reports=[folder/'reviews/R000-primary-review.md',folder/'reviews/R000-subagent-review.md']
assert all(p.is_file() and hash_ in p.read_text(encoding='utf-8').lower() for p in reports)
for archive in (folder/'snapshots').iterdir():
 if not archive.is_dir():continue
 m=json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
 for name,sha_ in m['files'].items():assert digest(archive/name)==sha_
index_path=OUT/'versions.json';versions=json.loads(index_path.read_text(encoding='utf-8'))
entry={'version':v,'parent':r['version_record'].get('parent_version') or next(x['version'] for x in versions if x.get('run_id')==r['version_record']['parent_run']),'run_id':r['id'],'messages':150,'status':r['status'],'human_verdict':args.verdict,'review':args.summary,'message_sha256':hash_,'run_sha256':digest(folder/'run.json'),'generation_code':r['version_record']['code'],'method':r['version_record']['method'],'model':'gpt-6-sol','reasoning_effort':'high','artifacts':v+'/','program_review_deferred':bool(r.get('program_review_deferred')),'program_review_executed':bool(r.get('program_review_history')),'review_mode':r['version_record'].get('review_mode','full'),'quality_review':r.get('quality_review'),'review_reports':[str(p.relative_to(OUT)).replace(chr(92),'/') for p in reports],'tag':f'dialogue-evolution-{v.lower()}-20261015'}
old=next((x for x in versions if x['version']==v),None)
assert old is None or old==entry,'Registered version differs; keep original and append reassessment separately'
if old is None:versions.append(entry);atomic(index_path,versions)
checks={str(p.relative_to(folder)).replace(chr(92),'/'):digest(p) for p in folder.rglob('*') if p.is_file() and '.runtime' not in p.parts and p.name not in ['hashes.json','review-index.json']}
atomic(folder/'review-index.json',{'version':v,'message_sha256':hash_,'verdict':args.verdict,'summary':args.summary,'program_review_deferred':entry['program_review_deferred'],'reports':entry['review_reports']})
checks['review-index.json']=digest(folder/'review-index.json');atomic(folder/'hashes.json',checks)
plan_path=OUT/'evolution-plan.json';plan=json.loads(plan_path.read_text(encoding='utf-8'));fingerprints=set();numbered=set()
for x in versions:
 match=re.fullmatch(r'V(\d+)(?:-R\d{3})?',x['version'])
 if not match or not 4<=int(match[1])<=40:continue
 payload=json.loads((OUT/x['version']/'run.json').read_text(encoding='utf-8'))
 fingerprints.add(hashlib.sha256(json.dumps([{k:m.get(k) for k in ('sender','content','type','image_code','image_mode')} for m in payload['messages']],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest());numbered.add(int(match[1]))
count=len(fingerprints);plan.update(completed_new_versions=count,completed_numbered_families=len(numbered),registered_full_payloads=len([x for x in versions if re.fullmatch(r'V(?:0[4-9]|[123]\d|40)(?:-R\d{3})?',x['version'])]),latest_registered=v,status='complete' if count>=37 and len(numbered)==37 else 'running');atomic(plan_path,plan)
print(json.dumps({'version':v,'id':r['id'],'verdict':args.verdict,'program_status':r['status'],'hash':hash_,'completed_new_versions':count,'code':entry['generation_code'],'tag':entry['tag']},ensure_ascii=False))
