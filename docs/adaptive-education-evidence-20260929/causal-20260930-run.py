"""Bounded synthetic A/B/C diagnostic; never opens or migrates the study database."""
import asyncio
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict

ROOT = Path.cwd()
OUT = Path('/tmp/lyra-causal-20260930')
TEMP = Path(tempfile.mkdtemp(prefix='lyra-causal-profile-'))
os.environ['LYRA_DATA_DIR'] = str(TEMP)
os.environ['LYRA_DB_PATH'] = str(TEMP / 'lyra.db')
os.environ['LYRA_CACHE_DIR'] = str(TEMP / 'cache')
os.environ['LYRA_MODELS_DIR_OVERRIDE'] = str(TEMP / 'models')
sys.path.insert(0, str(ROOT))
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from backend.api import routes_agent_chat as route
from backend.core import app_settings, sessions
from backend.core.errors import LyraError
from backend.llm import client as llm, tools
from backend.storage import database
from scripts import eval_tutor as evaluator

logging.disable(logging.CRITICAL)
def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

# Only the configured settings and already-authorized credential are read. No study rows.
source = Path('/Users/ofhd/Library/Application Support/Lyra/lyra.db')
with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as source_conn:
    source_conn.row_factory = sqlite3.Row
    config = app_settings.resolve_tutor_config(source_conn)
    remote_ack = app_settings.get_settings_row(source_conn)['remote_ack']
assert hashlib.sha256(config.endpoint_url.encode()).hexdigest() == '0850d34c3024f015b5ff6bf4687d788f0d302b57e2f0b64119dbc02ed0fa4fb8'
meta = {'source_sha': subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        'source_status_at_start': subprocess.check_output(['git','status','--porcelain'], text=True),
        'endpoint_sha256': hashlib.sha256(config.endpoint_url.encode()).hexdigest(),
        'configured_model': config.model, 'context_window': config.context_window,
        'stored_tools_supported': config.tools_supported, 'response_limit':16,
        'corpus_version':'2.1.0','contract_version':'4',
        'server_template_overrides':'not observable through configured OpenAI-compatible API',
        'files': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in [
            'backend/api/routes_agent_chat.py','backend/llm/prompts.py','backend/llm/client.py',
            'backend/llm/tools.py','scripts/eval_tutor.py','scripts/eval_corpora/tutor_semantic.json']}}
async def probe():
    async with llm._client(llm.PROBE_TIMEOUT, config.api_key, None) as c:
        try:
            r = await c.get(config.endpoint_url.rstrip('/') + '/models')
            r.raise_for_status()
            return {'status':r.status_code,'model_ids':[x.get('id') for x in r.json().get('data',[])]}
        except Exception as exc:
            return {'error_type':type(exc).__name__}
meta['models_probe'] = asyncio.run(probe())
write('meta.json',meta)
print(json.dumps(meta['models_probe']),flush=True)
if 'error_type' in meta['models_probe']:
    sys.exit(2)
assert config.model in meta['models_probe']['model_ids']

# Settings URL stays in this disposable database, never in retained artifacts. Secret stays in memory.
conn = database.connect(TEMP/'lyra.db'); database.migrate(conn)
conn.execute('update settings set endpoint_url=?, model=?, context_window=?, tools_supported=?, vision_supported=?, remote_ack=? where id=1',
             (config.endpoint_url,config.model,config.context_window,config.tools_supported,config.vision_supported,remote_ack))
class_id=conn.execute('insert into classes (name,code) values (?,?)',(evaluator.EVAL_CLASS_NAME,evaluator.EVAL_CLASS_CODE)).lastrowid
conn.commit()
original_credential=app_settings.credential_for_row
app_settings.credential_for_row=lambda row: config.api_key if row['endpoint_url']==config.endpoint_url else None

def db_dependency():
    db=database.connect(TEMP/'lyra.db')
    try: yield db
    finally: db.close()
app=FastAPI(); app.include_router(route.router); app.dependency_overrides[database.get_db]=db_dependency
@app.exception_handler(LyraError)
async def error_handler(request: Request, exc: LyraError):
    return JSONResponse(status_code=exc.status,content={'detail':exc.message,**(exc.extra or {})})

payloads=json.loads((OUT/'payloads.json').read_text()) if (OUT/'payloads.json').exists() else {}
records=json.loads((OUT/'runs.json').read_text()) if (OUT/'runs.json').exists() else []
active=None
original_client=llm._client
original_collect=llm._collect_tool_stream
async def request_hook(request):
    if request.url.path.endswith('/chat/completions'):
        body=json.loads(request.content)
        # No URL, headers or credential retained; all message content is synthetic.
        ident=sha(body); payloads[ident]=body
        active['request_hashes'].append(ident)
        if len(active['request_hashes'])==1 and active.get('expected_first'):
            assert ident==active['expected_first'], 'Unmatched B/C payload before send'
def observed_client(timeout,api_key,transport):
    c=original_client(timeout,api_key,transport)
    c.event_hooks['request'].append(request_hook)
    return c
async def observed_collect(response,on_delta):
    details={'model_ids':[],'finish_reasons':[]}
    original_lines=response.aiter_lines
    async def lines():
        async for line in original_lines():
            if line.startswith('data:') and line[5:].strip()!='[DONE]':
                frame=json.loads(line[5:])
                if frame.get('model') and frame['model'] not in details['model_ids']: details['model_ids'].append(frame['model'])
                for choice in frame.get('choices',[]):
                    if choice.get('finish_reason'): details['finish_reasons'].append(choice['finish_reason'])
            yield line
    response.aiter_lines=lines
    result=await original_collect(response,on_delta)
    details.update({'answer':result.content,'tool_calls':[asdict(x) for x in result.tool_calls],'truncated':result.truncated})
    active['provider_rounds'].append(details)
    return result
llm._client=observed_client; llm._collect_tool_stream=observed_collect

corpus=json.loads((ROOT/'scripts/eval_corpora/tutor_semantic.json').read_text())
ids=['vector-space-definition','vector-space-scalar-followup','vector-space-start-heldout','show-convolution-worked']
cases={x['id']:x for x in corpus['cases'] if x['id'] in ids}
minimal='You are a helpful education tutor. Answer the latest question directly and concisely at the requested depth. For problem-start help, give a useful next move and leave work for the student; provide a complete solution when explicitly requested.'
seeds={r['case']:r['request_hashes'][0] for r in records if r['arm']=='C' and r.get('stopped')=='completed'}
def prepare(case):
    sid=int(sessions.create_session(conn,class_id)['id'])
    for turn in case['history']: sessions.add_message(conn,sid,turn['role'],turn['content'])
    conn.commit()
    assembly=route.assemble_class_chat_turn(conn,class_id,sid,config,content=case['user'])
    return sid,assembly
async def raw_round(body):
    async with llm._client(llm.TOOL_TIMEOUT,config.api_key,None) as c:
        async with c.stream('POST',config.endpoint_url.rstrip('/')+'/chat/completions',json=body) as r:
            llm._check_tools_status(r)
            return await llm._collect_tool_stream(r,lambda delta:None)
async def direct(case,arm,assembly):
    body=copy.deepcopy(payloads[seeds[case['id']]])
    if arm=='A':
        body['messages']=[{'role':'system','content':minimal},*case['history'],{'role':'user','content':case['user']}]
        body.pop('tools',None)
        result=await raw_round(body)
        assert not result.tool_calls
        return result.content, 'output_limit' if result.truncated else 'completed', []
    original_complete=tools.complete_with_tools
    count=0
    async def replay_first(*args,**kwargs):
        nonlocal count
        count+=1
        if count==1: return await raw_round(body)
        return await original_complete(*args,**kwargs)
    tools.complete_with_tools=replay_first
    try:
        result=await tools.run_tool_loop(config.endpoint_url,config.api_key,config.model,
            copy.deepcopy(body['messages']),registry=dict(assembly.registry),context_budget=assembly.context_budget,
            on_delta=lambda delta:None)
        return result.content,result.stopped,[asdict(x) for x in result.calls]
    finally: tools.complete_with_tools=original_complete

schedule=[(cid,arm,1) for cid in ids for arm in ['C','B','A']]
schedule += [('vector-space-scalar-followup',arm,2) for arm in ['A','B','C']]
schedule += [('vector-space-definition','A',2)]
with TestClient(app) as http:
    for cid,arm,repeat in schedule:
        if any((r['case'],r['arm'],r['repeat'])==(cid,arm,repeat) for r in records): continue
        case=cases[cid]; sid,assembly=prepare(case)
        active={'case':cid,'arm':arm,'repeat':repeat,'request_hashes':[],'provider_rounds':[]}
        if arm=='B' or (arm=='C' and repeat==2): active['expected_first']=seeds[cid]
        start=time.monotonic()
        print(f'START {len(records)+1}/16 {cid} {arm}{repeat}',flush=True)
        try:
            if arm=='C':
                response=http.post(f'/api/classes/{class_id}/sessions/{sid}/agent-chat',headers={'Accept':'text/event-stream'},json={'content':case['user']})
                frames=[json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
                final=frames[-1]
                assert response.status_code==200 and final['type']=='result', final
                answer=final['result']['content']
                saved=sessions.list_messages(conn,sid)[-1]['content']
                active['route_result']=final['result']
                active['provider_equals_route_equals_saved']=active['provider_rounds'][-1]['answer'].strip()==answer==saved
                assert active['provider_equals_route_equals_saved']
                stopped='completed'
                if repeat==1: seeds[cid]=active['request_hashes'][0]
                planned=llm._chat_body(config.model,list(assembly.messages),stream=True,tools=list(assembly.tools),max_tokens=assembly.context_budget.generation_reserve,temperature=tools.DETERMINISTIC_TEMPERATURE)
                active['pre_route_planner_matches_wire']=sha(planned)==active['request_hashes'][0]
            else:
                answer,stopped,calls=asyncio.run(direct(case,arm,assembly));active['tool_calls']=calls
            active.update(answer=answer,stopped=stopped,words=len(answer.split()))
        except Exception as exc:
            # Exception text can contain transport URLs. Keep bounded type only.
            active.update(error_type=type(exc).__name__)
            records.append(active);write('runs.json',records);write('payloads.json',payloads)
            print('STOP '+type(exc).__name__,flush=True)
            raise RuntimeError('Diagnostic stopped; inspect safe retained fields') from None
        active['seconds']=round(time.monotonic()-start,2)
        records.append(active);write('runs.json',records);write('payloads.json',payloads)
        print(f"DONE {arm}{repeat} words={active['words']} rounds={len(active['request_hashes'])} stopped={stopped}",flush=True)
conn.close()
meta['completed_responses']=len(records)
meta['harness_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
write('meta.json',meta)
