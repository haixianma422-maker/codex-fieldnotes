import json
import shutil
import subprocess
import threading
import queue
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from journal_core import BASE, put_json, now, redact

cli = r'C:\Users\13412\AppData\Local\OpenAI\Codex\bin\9691020b546a15b2\codex.exe'
p = subprocess.Popen([cli, '-c', 'windows.sandbox=mxc', 'app-server', '--stdio'], cwd=BASE / 'demo-project',
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
q = queue.Queue()
def read():
    for line in iter(p.stdout.readline, b''):
        try:
            q.put(json.loads(line))
        except ValueError:
            pass
threading.Thread(target=read, daemon=True).start()
def send(obj):
    p.stdin.write((json.dumps(obj)+'\n').encode('utf-8'))
    p.stdin.flush()
def request(i, method, params):
    send({'id':i,'method':method,'params':params})
    for _ in range(100):
        obj=q.get(timeout=25)
        if obj.get('id') == i:
            return obj
    raise RuntimeError('Too many notifications')
try:
    init=request(1,'initialize', {'clientInfo':{'name':'journal_hook_diagnostic','version':'1.0.0'},'capabilities':{'experimentalApi':True}})
    send({'method':'initialized','params':{}})
    hooks=request(2,'hooks/list',{'cwds':[str(BASE / 'demo-project')]})
    req=request(3,'configRequirements/read',{})
    requirements=req.get('result',{}).get('requirements') or {}
    result={'observed_at':now(),'initialize_error':init.get('error'), 'hooks':hooks,
        'managed_hooks_only':requirements.get('allowManagedHooksOnly',requirements.get('allow_managed_hooks_only'))}
    result=redact(result)
    previous = BASE / 'docs/hook-diagnostic-runtime.json'
    if previous.exists():
        shutil.copy2(previous, BASE / 'docs' / ('hook-diagnostic-before-' + now().replace(':', '').replace('+', '_') + '.json'))
    put_json(previous,result)
    print(json.dumps(result, ensure_ascii=True))
finally:
    p.terminate()
    try: p.wait(timeout=10)
    except subprocess.TimeoutExpired: p.kill()
