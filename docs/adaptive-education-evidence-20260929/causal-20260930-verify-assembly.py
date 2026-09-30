import os
import sys
from pathlib import Path
ROOT = Path('/Users/ofhd/Developer/Lyra-pla574-461-chat')
OUT = Path('/tmp/lyra-causal-trace')
os.environ.update(LYRA_DATA_DIR=str(OUT/'data'), LYRA_DB_PATH=str(OUT/'data'/'eval.db'), LYRA_LOGS_DIR=str(OUT/'logs'), LYRA_CACHE_DIR=str(OUT/'cache'), PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring')
sys.path.insert(0, str(ROOT))
import socket
attempts = []
def blocked(*args, **kwargs):
    attempts.append('network_or_retrieval_attempt')
    raise AssertionError('Offline verification forbids network and live retrieval')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
import hashlib
import json
import sqlite3
import tempfile
from dataclasses import replace
from backend.api import routes_agent_chat as route
from backend.core import sessions
from backend.core.app_settings import TutorConfig
from backend.llm import prompts, tools
from backend.rag.retrieve import RetrievalResult, RetrievedChunk
from backend.storage import database
from scripts import eval_tutor as ev
route._retrieve_turn_context = blocked
OUT.joinpath('data').mkdir(exist_ok=True)
db_path = Path(tempfile.mkdtemp(prefix='assembly-', dir=OUT/'data'))/'eval.db'
conn = database.connect(db_path)
database.migrate(conn)
class_id = int(conn.execute("insert into classes (name, code) values ('Synthetic trace class', 'TRACE')").lastrowid)
session_id = int(sessions.create_session(conn, class_id)['id'])
markers = {'user_fact': 'SYNTHETIC_USER_FACT_7F82', 'class_fact': 'SYNTHETIC_CLASS_FACT_4B61', 'retrieval': 'SYNTHETIC_RETRIEVAL_0D93'}
for cid, label, value in [(None, 'User convention', markers['user_fact']), (class_id, 'Class convention', markers['class_fact'])]:
    conn.execute("insert into profile_facts (class_id, kind, label, value, confidence, confirmed) values (?, 'note', ?, ?, 'high', 1)", (cid,label,value))
history = ({'role':'user','content':'Synthetic earlier question about mappings.'}, {'role':'assistant','content':'A mapping associates an input with an output.'})
for entry in history:
    sessions.add_message(conn, session_id, entry['role'], entry['content'])
conn.commit()
case = ev.Case(id='synthetic-parity', mode='guide', user='What does a function mean?', history=history, context=({'content':markers['retrieval']+' A synthetic reference associates inputs with outputs.', 'filename':'synthetic-fixture.txt', 'page_number':7},), must=(), must_not=(), may=(), notes='Offline synthetic assembly only.')
retrieval = RetrievalResult(chunks=(RetrievedChunk(chunk_id=1, document_id=0, content=case.context[0]['content'], token_count=0, page_number=7, section_title=None, section_path=None, section_number=None, problem_number=None, part_index=None, filename='synthetic-fixture.txt', similarity=1.0, score=1.0),), trimmed=False, omitted_document_count=0)
config = TutorConfig(endpoint_url='http://127.0.0.1:1/v1', api_key=None, model='synthetic-no-provider', context_window=16384, tools_supported=True, vision_supported=False)
def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
results=[]
for support in (True, False):
    cfg=replace(config,tools_supported=support)
    assembly=ev.class_chat_assembly(conn,class_id,session_id,cfg,case)
    # Actual route planner reads persisted session history, unlike evaluator's override.
    plan=route._plan_agent_turn(conn,class_id,session_id,cfg,profile='agent',content=case.user,mode=case.mode,document_id=None,cached_retrieval=retrieval)
    assert list(assembly.messages)==plan.messages
    assert assembly.tools==tuple(tools.tool_schemas(plan.registry))
    assert assembly.toolless==plan.toolless
    system=str(assembly.messages[0]['content'])
    assert system.count(prompts._EDUCATION_PROMPT)==1
    for marker in markers.values():
        assert system.count(marker)==1
    indices={'education':system.index(prompts._EDUCATION_PROMPT), **{key:system.index(marker) for key,marker in markers.items()}}
    assert indices['education']<indices['user_fact']<indices['class_fact']<indices['retrieval']
    show=ev.class_chat_assembly(conn,class_id,session_id,cfg,replace(case,mode='show'))
    assert assembly.messages==show.messages and assembly.tools==show.tools
    results.append({'tools_supported':support,'toolless':assembly.toolless,'message_count':len(assembly.messages),'roles':[m['role'] for m in assembly.messages],'messages_sha256':sha(assembly.messages),'system_sha256':hashlib.sha256(system.encode()).hexdigest(),'tools_sha256':sha(assembly.tools),'tool_names':[s['function']['name'] for s in assembly.tools],'marker_counts':{key:system.count(marker) for key,marker in markers.items()},'character_positions':indices,'eval_vs_persisted_history_planner_messages_equal':True,'eval_vs_planner_tools_equal':True,'guide_show_equal':True})
counts={table:conn.execute(f'select count(*) from {table}').fetchone()[0] for table in ('documents','chunks','profile_facts','messages')}
assert counts['documents']==counts['chunks']==0
assert not attempts
report={'synthetic_database_path':str(db_path),'source_revision':'5548f4b14f7368ee515d5cf26725d9241adced1a','results':results,'synthetic_database_rows':counts,'network_attempts':len(attempts),'retrieval_source':'Explicit cached RetrievalResult from synthetic corpus fixture; no uploaded documents, ingestion, embeddings, or search exercised.','limitations':['Assembly parity only: no provider calls, output behavior, streaming, or final answer parsing tested.','No image attachments or image-tool continuation callbacks exercised.','Synthetic confirmed facts test rendering/order, not ingestion/extraction/confirmation workflows.','Fixed 16384 context window and short history; trimming pressure is not exercised.']}
conn.close()
OUT.joinpath('assembly-evidence.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
