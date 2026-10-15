"""One read-only exact review request; retain content/finish evidence, never credentials or reasoning text."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8'))
    config = yaml.safe_load((ROOT / '.vimax/deploy/runtime-model.yaml').read_text(encoding='utf-8'))['llm']
    args.out.mkdir(exist_ok=False)
    started = time.monotonic()
    proof = {'model': 'gpt-6-sol', 'reasoning_effort': 'high', 'diagnostic_only': True,
             'request_source': args.request.as_posix(), 'status': 'running', 'events': [], 'content': ''}
    def save():
        (args.out / 'result.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    body = {'model': 'gpt-6-sol', 'reasoning_effort': 'high', 'temperature': 0.1, 'stream': True,
            'messages': [{'role': 'system', 'content': request['system']},
                         {'role': 'user', 'content': json.dumps(request['payload'], ensure_ascii=False)}]}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=360) as client:
            async with client.stream('POST', config['base_url'].rstrip('/') + '/chat/completions',
                                     headers={'Authorization': 'Bearer ' + config['api_key']}, json=body) as response:
                proof['http_status'] = response.status_code
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        proof['events'].append({'kind': 'DONE', 'elapsed_seconds': round(time.monotonic()-started, 2)})
                        continue
                    chunk = json.loads(data)
                    metadata = {'kind': chunk.get('object'), 'keys': list(chunk)}
                    if 'error' in chunk:
                        metadata['error_type'] = str(chunk['error'].get('type', 'upstream error')) if isinstance(chunk['error'], dict) else 'upstream error'
                    metadata['choices'] = []
                    for choice in chunk.get('choices', []):
                        delta = choice.get('delta', {})
                        content = delta.get('content')
                        if isinstance(content, str):
                            proof['content'] += content
                        metadata['choices'].append({'index': choice.get('index'), 'finish_reason': choice.get('finish_reason'),
                                                    'delta_keys': list(delta), 'content_length': len(content or '')})
                    proof['events'].append(metadata)
                    save()
        proof['status'] = 'received'
        try:
            proof['parsed_content'] = json.loads(proof['content'])
        except ValueError:
            proof['valid_json_content'] = False
        else:
            proof['valid_json_content'] = True
    except Exception as exc:
        proof.update(status='failed', error_type=type(exc).__name__)
    finally:
        proof['elapsed_seconds'] = round(time.monotonic()-started, 2)
        save()
    print(json.dumps({k:v for k,v in proof.items() if k not in {'content', 'events', 'parsed_content'}}, ensure_ascii=False))


asyncio.run(main())
