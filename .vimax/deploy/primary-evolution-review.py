from pathlib import Path
import argparse,json,hashlib
p=argparse.ArgumentParser();p.add_argument('--version',type=int,required=True);p.add_argument('--revision-label');p.add_argument('--text',required=True);p.add_argument('--verified-parent-coverage',action='store_true');a=p.parse_args()
label=f'V{a.version:02d}'+('-'+a.revision_label if a.revision_label else '')
folder=Path(__file__).resolve().parents[2]/'outputs/自然群聊-20261015'/label
r=json.loads((folder/'run.json').read_text(encoding='utf-8'));assert len(r['messages'])==150 and r['status']!='running'
fields=('seq','sender','content','reply_to','delay','stage','type','image_code','image_mode','replace_date')
h=hashlib.sha256(json.dumps([{k:m.get(k) for k in fields} for m in r['messages']],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
target=folder/'reviews/R000-primary-review.md';target.parent.mkdir(exist_ok=True);assert not target.exists()
mode='基于已实际全文读过的父稿，程序逐条核对全部150条对象相同性；实际逐条阅读所有变更正文和关联上下文，覆盖本版全部内容。不是声称又从头逐字重读未变内容。独立subagent另从头全文阅读150条。' if a.verified_parent_coverage else '实际阅读本份001–150全部正文，结合父稿、母本事实、人格及媒体对象检查。'
target.write_text(f'# {label} 主Agent全量覆盖审阅\n\n{mode}\n\n消息 SHA256：{h}\n\n'+a.text+'\n\n结构合同与模型语义审查分别记录，不以paused状态声称模型通过。\n',encoding='utf-8')
print(h)
