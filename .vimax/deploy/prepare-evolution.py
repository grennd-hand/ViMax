from pathlib import Path
import argparse,json,hashlib,os,uuid
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'outputs/自然群聊-20261015'
p=argparse.ArgumentParser();p.add_argument('--version',type=int,required=True);p.add_argument('--revision-label');p.add_argument('--parent',required=True);p.add_argument('--issues',type=Path,required=True);p.add_argument('--experiment',default='受控表达比较');args=p.parse_args()
assert 4<=args.version<=40
folder=OUT/args.parent;r=json.loads((folder/'run.json').read_text(encoding='utf-8'));assert len(r['messages'])==150 and r['status']!='running'
fields=('seq','sender','content','reply_to','delay','stage','type','image_code','image_mode','replace_date')
hash_=hashlib.sha256(json.dumps([{k:m.get(k) for k in fields} for m in r['messages']],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest();sha=hashlib.sha256((folder/'run.json').read_bytes()).hexdigest()
reports={k:folder/'reviews'/name for k,name in [('main','R000-primary-review.md'),('subagent','R000-subagent-review.md')]}
assert all(p.is_file() and hash_ in p.read_text(encoding='utf-8').lower() for p in reports.values()),'Both real full-read reports must exist for this exact parent'
issues=json.loads(args.issues.read_text(encoding='utf-8'));assert isinstance(issues,list) and issues
value={'issues':issues,'reviews':{k:{'full_read':True,'message_count':150,'run_sha256':sha,'summary':args.parent+'完整150条已由对应审阅者全文阅读；具体结论见已完成报告'+str(path.relative_to(OUT)).replace(chr(92),'/'),'report':str(path.relative_to(OUT)).replace(chr(92),'/')} for k,path in reports.items()},'experiment':args.experiment,'reviewed_best_parent':args.parent}
if '不是声称又从头逐字重读' in reports['main'].read_text(encoding='utf-8'):
 value['reviews']['main'].update(full_read=False,coverage_mode='verified_parent_and_all_changes',all_changes_read=True,parent_full_read_before=True,comparison_total=150,summary=args.parent+'主审以已读父稿逐条核对150条，实际读全部变化及前后文；独立subagent从头全文阅读。主审方法见'+str(reports['main'].relative_to(OUT)).replace(chr(92),'/'))
label=f'V{args.version:02d}'+('-'+args.revision_label if args.revision_label else '')
target=OUT/f'evolution-inputs/{label}-issues.json';assert not target.exists(),'Input evidence already exists; keep it immutable'
tmp=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,target)
print(json.dumps({'version':f'V{args.version:02d}','parent':args.parent,'parent_sha256':sha,'input':str(target)},ensure_ascii=False))
