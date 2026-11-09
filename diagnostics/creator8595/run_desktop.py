"""Run the real frozen sidecar in a disposable CI workspace, through HTTP."""
import argparse,json,os,pathlib,subprocess,time,urllib.request,urllib.error,shutil,traceback
p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--label',required=True);args=p.parse_args()
repo=pathlib.Path.cwd();resources=pathlib.Path(args.root).resolve();out=repo/'diagnostic-results'/args.label;out.mkdir(parents=True,exist_ok=True)
found=list(resources.rglob('qwenpaw-backend.exe'))
if not found: raise SystemExit('No frozen backend found under '+str(resources))
exe=min(found,key=lambda v:len(v.parts));runtime=out/'runtime';env=os.environ.copy()
for key in ('GITHUB_TOKEN','GH_TOKEN','TAURI_SIGNING_PRIVATE_KEY','TAURI_SIGNING_PRIVATE_KEY_PASSWORD'):
    env.pop(key,None)
for key,value in {'QWENPAW_WORKING_DIR':runtime/'working','QWENPAW_SECRET_DIR':runtime/'secret','QWENPAW_DEPENDENCY_STATE_DIR':runtime/'dependencies'}.items():env[key]=str(value.resolve())
env['PYTHONIOENCODING']='utf-8';env['QWENPAW_LOG_LEVEL']='debug';env['CREATOR_AUTO_INSTALL_BINARIES']='false'
for key in ('QWENPAW_TAURI_RESOURCE_DIR','QWENPAW_CHANNEL_RUNTIME_MANIFEST','QWENPAW_DESKTOP_PY_RUNTIME','QWENPAW_CHANNEL_SUPPORT_ROOT','QWENPAW_CHANNEL_DEPENDENCY_PATHS','PYTHONPATH'):
    env.pop(key,None)
work=pathlib.Path(env['QWENPAW_WORKING_DIR']);work.mkdir(parents=True,exist_ok=True)
summary={'label':args.label,'executable':str(exe),'resources':str(resources),'events':[]}
plugin=out/'diagnostic-plugin';plugin.mkdir(exist_ok=True)
(plugin/'plugin.json').write_text(json.dumps({'id':'creator-issue-diagnostic','name':'Temporary Creator diagnostics','version':'1.0.0','entry':{'backend':'main.py'},'dependencies':[]}),encoding='utf-8')
shutil.copyfile(repo/'diagnostics/creator8595/diagnostic_plugin.py',plugin/'main.py')
stdout=(out/'stdout.log').open('w',encoding='utf-8');stderr=(out/'stderr.log').open('w',encoding='utf-8')
proc=subprocess.Popen([str(exe)],cwd=exe.parent,env=env,stdout=stdout,stderr=stderr)
base=None

def request(method,path,data=None,timeout=90):
    req=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,method=method,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response: code=response.status;raw=response.read().decode('utf-8')
    except urllib.error.HTTPError as exc: code=exc.code;raw=exc.read().decode('utf-8',errors='replace')
    try: value=json.loads(raw)
    except ValueError: value=raw
    summary['events'].append({'method':method,'path':path,'status':code,'body':value})
    (out/'result.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(args.label,method,path,code,json.dumps(value,ensure_ascii=True)[:3000],flush=True)
    return code,value
try:
    until=time.monotonic()+240
    while time.monotonic()<until:
        if proc.poll() is not None: raise RuntimeError('Frozen backend exited with '+str(proc.returncode))
        marker=work/'desktop_port'
        if marker.exists():
            try:
                base='http://127.0.0.1:'+str(int(marker.read_text().strip()))
                with urllib.request.urlopen(base+'/api/version',timeout=2) as response:
                    if response.status==200:break
            except Exception: pass
        time.sleep(1)
    else: raise TimeoutError('Frozen backend did not become ready')
    request('POST','/api/plugins/install',{'source':str(plugin.resolve())},120)
    url='https://agentscope-platform-sg-bucket.oss-accelerate.aliyuncs.com/plugin-platform/qwenpaw-creator/2.0.4/qwenpaw-creator-2.0.4.zip'
    code,body=request('POST','/api/plugins/install',{'source':url},900)
    if code==409 and isinstance(body,dict) and isinstance(body.get('detail'),dict) and body['detail'].get('code')=='DEPENDENCY_UPGRADE_REQUIRED':
        operation=body['detail']['operation'];summary['operation']=operation
        request('POST','/api/plugins/dependency-upgrades/'+operation['id']+'/apply',{},900)
    request('GET','/api/plugins')
except BaseException as exc:
    summary['controller_exception']=''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))
    print(summary['controller_exception'],flush=True)
finally:
    if proc.poll() is None:
        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,check=False,timeout=30)
    stdout.close();stderr.close()
    # Capture only logs from this fresh runner workspace, never generated secrets/config.
    logs=out/'backend-logs';logs.mkdir(exist_ok=True)
    for file in work.rglob('*.log'):
        shutil.copyfile(file,logs/str(file.relative_to(work)).replace('\\','__').replace('/','__'))
    (out/'result.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    for file in (out/'stderr.log',out/'stdout.log'):
        print('TAIL',file.name,'\n',file.read_text(encoding='utf-8',errors='replace')[-8000:],flush=True)
    # Do not upload plugin credentials, coordination capabilities or full runtime state.
    shutil.rmtree(runtime,ignore_errors=True)
    shutil.rmtree(plugin,ignore_errors=True)
if summary.get('controller_exception'): raise SystemExit(1)
