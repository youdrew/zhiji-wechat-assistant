"""Synthetic renderer/store benchmark; no business data or model request is used.
Run with the project Python, open the printed URL and click Run benchmark.
Browser mode includes authenticated service/store reads; --electron uses production
HTTP routes and a trusted desktop runner that also measures preload/IPC.
The desktop runner's --large flag selects the separate 16 MiB boundary dataset.
"""
from __future__ import annotations
import json
import platform
import re
import sys
import tempfile
import threading
import time
import psutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from model_traces_store import TraceStore
from model_traces import ModelTraceService
from model_traces import TraceError
import model_traces_http

HARNESS = r"""
<style>#modelTracesPanel{position:fixed;inset:0;z-index:48}#benchmarkControls{position:fixed;left:70px;top:4px;z-index:1000;padding:8px;background:#fff;color:#111;max-width:460px;font:12px monospace}#benchmarkReport{white-space:pre-wrap;max-height:180px;overflow:auto}</style>
<div id="benchmarkControls"><button id="runBenchmark">Run benchmark: 10 warmups + 100 list/detail samples</button><pre id="benchmarkReport">Ready — synthetic data only</pre></div>
<script>
if(!window.desktopHost) window.desktopHost={modelTracesRequest:async(operation,args={})=>{
 const response=await fetch('/fixture-api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation,args})});
 return response.json();
}};
</script><script src="model-traces.js"></script><script>
// Embedded benchmark fixture only; production opens model-traces-window.html.
ModelTraces.init({standalone:true,account:'synthetic-account',user:'synthetic-contact',view:'chat'});
const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const viewReady=()=>new Promise((resolve,reject)=>{let timer;const ready=()=>[...document.querySelectorAll('#modelTraceDetail pre')].some(node=>node.textContent.includes('SYNTHETIC'));const observer=new MutationObserver(()=>{if(ready()){clearTimeout(timer);observer.disconnect();resolve();}});if(ready())return resolve();observer.observe(document.querySelector('#modelTraceDetail'),{childList:true,subtree:true});timer=setTimeout(()=>{observer.disconnect();reject(Error('Body did not render'));},5000);});
const stats=values=>{const sorted=[...values].sort((a,b)=>a-b);return{samples:values.length,medianMs:sorted[Math.floor(sorted.length/2)],p95Ms:sorted[Math.ceil(.95*sorted.length)-1],maxMs:sorted.at(-1)}};
document.querySelector('#runBenchmark').onclick=async()=>{
 const button=document.querySelector('#runBenchmark'),report=document.querySelector('#benchmarkReport');button.disabled=true;
 const heapBefore=performance.memory?.usedJSHeapSize, lists=[],details=[];let coldList,coldDetail,peakHeap=heapBefore;const sampleHeap=()=>{if(performance.memory)peakHeap=Math.max(peakHeap,performance.memory.usedJSHeapSize);};
 try{
 for(let i=0;i<111;i++) { const start=performance.now();await ModelTraces.open(button);await paint();const elapsed=performance.now()-start;sampleHeap();if(i===0)coldList=elapsed;else if(i>10)lists.push(elapsed);await ModelTraces.close({restore:false,focus:false});report.textContent=`List ${i+1}/111`; }
 await ModelTraces.open(button);
 for(let i=0;i<111;i++){const rows=document.querySelectorAll('#modelTraceList button');const start=performance.now();rows[i%rows.length].click();if(i===0)document.querySelector('#traceTab_request').click();await viewReady();await paint();const elapsed=performance.now()-start;sampleHeap();if(i===0)coldDetail=elapsed;else if(i>10)details.push(elapsed);report.textContent=`Detail ${i+1}/111`;}
 const result={dataset:{metadata:10000,regularBodies:100,requestBytes:65536,responseBytes:262144},conditions:{renderer:'browser',cache:'application body cache cleared per selection; OS cache not cleared',cold:'first operation after page load, application already initialized',excluded:['Electron IPC'],backend:'authenticated ModelTraceService.read and TraceStore',modelRequests:0},coldListMs:coldList,coldDetailMs:coldDetail,list:stats(lists),detail:stats(details),rendererPeakHeapDeltaBytes:performance.memory?peakHeap-heapBefore:null,rendererHeapDeltaBytes:performance.memory?performance.memory.usedJSHeapSize-heapBefore:null,userAgent:navigator.userAgent};
 result.passed=result.list.p95Ms<=500&&result.detail.p95Ms<=1000&&coldList<=500&&coldDetail<=1000;
 report.textContent=JSON.stringify(result,null,2);await fetch('/benchmark-results',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(result)});document.body.dataset.benchmark='complete';
 }catch(error){report.textContent='FAILED: '+error.message;document.body.dataset.benchmark='failed';}finally{button.disabled=false;}
};
</script>
"""

LARGE_RUN = r"""
document.querySelector('#runBenchmark').textContent='Run 16 MiB boundary: first read + 10 repeat reads per side';
document.querySelector('#runBenchmark').onclick=async()=>{
 const button=document.querySelector('#runBenchmark'),report=document.querySelector('#benchmarkReport');button.disabled=true;
 const heapBefore=performance.memory?.usedJSHeapSize;let peakHeap=heapBefore;const sides={};
 const waitFor=predicate=>new Promise((resolve,reject)=>{let timer;const observer=new MutationObserver(()=>{if(predicate()){clearTimeout(timer);observer.disconnect();resolve();}});if(predicate())return resolve();observer.observe(document.querySelector('#modelTraceDetail'),{childList:true,subtree:true});timer=setTimeout(()=>{observer.disconnect();reject(Error('Large body page did not render'));},5000);});
 try{
  await ModelTraces.open(button);
  for(const kind of ['request','response']){
   const samples=[];let firstMs,maxRenderedBytes=0;
   for(let i=0;i<11;i++){
    const start=performance.now();document.querySelector('#modelTraceList button').click();document.querySelector('#traceTab_'+kind).click();await viewReady();await paint();
    const elapsed=performance.now()-start;if(i===0)firstMs=elapsed;else samples.push(elapsed);
    const content=[...document.querySelectorAll('#modelTraceDetail pre')].map(item=>item.textContent).join('');
    maxRenderedBytes=Math.max(maxRenderedBytes,new TextEncoder().encode(content).length);
    if(document.querySelector('#modelTraceDetail .model-traces-pages button:last-child')?.disabled!==false)throw Error('Large body must retain next-page control');
    if(performance.memory)peakHeap=Math.max(peakHeap,performance.memory.usedJSHeapSize);
    report.textContent=`Large ${kind}: ${i+1}/11`;
   }
   document.querySelector('#modelTraceDetail .model-traces-pages button:last-child').click();
   await waitFor(()=>document.querySelector('#modelTraceDetail .model-traces-pages span')?.textContent.startsWith('64.0 KB')&&document.querySelector('#modelTraceDetail pre'));await paint();
   sides[kind]={firstMs,repeated:stats(samples),maxRenderedBytes,nextPageWorked:true,pageLabel:document.querySelector('#modelTraceDetail .model-traces-pages span').textContent};
  }
  const input=document.querySelector('#chatInput');input.focus();input.value='SYNTHETIC_BOUNDARY_DRAFT';
  const inputFocusable=document.activeElement===input&&!input.disabled;
  document.querySelector('#modelTraceClose').click();await paint();
  const closeWorked=!ModelTraces.isOpen(),draftPreserved=input.value==='SYNTHETIC_BOUNDARY_DRAFT';
  const reads=await fetch('/fixture-read-audit').then(response=>response.json());
  const result={mode:'large-body-boundary',dataset:{metadata:1,requestBytes:16777216,responseBytes:16777216,storageChunkBytes:65536},conditions:{renderer:'Electron production preload and IPC',backend:'authenticated production HTTP, ModelTraceService.read and TraceStore',cache:'application body cache cleared per selection; OS cache not cleared',modelRequests:0},sides,reads,inputFocusable,closeWorked,draftPreserved,rendererPeakHeapDeltaBytes:performance.memory?peakHeap-heapBefore:null,userAgent:navigator.userAgent};
  result.passed=Object.values(sides).every(side=>side.firstMs<=1000&&side.repeated.maxMs<=1000&&side.maxRenderedBytes<=65536&&side.nextPageWorked)&&reads.length>0&&reads.every(read=>read.limit===65536&&read.returnedBytes<=65536&&[0,65536].includes(read.offset)&&read.totalBytes===16777216)&&inputFocusable&&closeWorked&&draftPreserved;
  report.textContent=JSON.stringify(result,null,2);await fetch('/benchmark-results',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(result)});document.body.dataset.benchmark='complete';
 }catch(error){report.textContent='FAILED: '+error.message;document.body.dataset.benchmark='failed';}finally{button.disabled=false;}
};
</script>
"""

def payload(size):
    prefix = '{"model":"SYNTHETIC","messages":[{"role":"user","content":"'
    suffix = '"}]}'
    return prefix + 'x' * (size - len(prefix) - len(suffix)) + suffix

def main():
    native = "--electron" in sys.argv
    large = "--large" in sys.argv
    if large and not native:
        raise ValueError("--large requires the Electron benchmark runner")
    control = json.loads(sys.stdin.readline()) if native else {}
    control_token = control.get("token")
    temporary_root = ROOT / ".local" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kyf-trace-viewer-", dir=temporary_root) as directory:
        store = TraceStore(Path(directory))
        started = time.time()
        for index in range(1 if large else 10000):
            meta = {"recordId": f"synthetic-{index:05d}", "taskId": f"task-{index}", "accountId": "synthetic-account", "contactId": "synthetic-contact", "startedAt": started-index,
                    "source": "advisor", "model": "SYNTHETIC", "transportStatus": "completed", "applicationStatus": "completed", "adoption": "adopted", "collectionStatus": "complete", "complete": True}
            store.insert(meta)
            for seq, kind in enumerate(("request", "response")):
                size = 16*1024*1024 if large else (65536 if kind == "request" else 262144) if index < 100 else 128
                text = None if large else payload(size)
                for chunk, offset in enumerate(range(0, size, 65536)):
                    # The boundary fixture builds one store chunk at a time.
                    part = ('SYNTHETIC_'+kind+'\n').ljust(min(65536,size-offset),'x') if large else text[offset:offset+65536]
                    store.append(meta["recordId"], {"eventSeq": seq, "contentId": kind, "kind": kind, "chunkIndex": chunk, "byteOffset": offset, "text": part, "final": offset+65536>=size})
        store.db.commit()
        store.close()
        service = ModelTraceService(Path(directory), lambda: "synthetic-account")
        viewer = service.create_session("benchmark-fixture", "account")
        store = service._store
        html=(ROOT / "chatui/index.html").read_text(encoding="utf-8")
        html=re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', html)
        harness = HARNESS
        if large:
            harness = HARNESS.split("document.querySelector('#runBenchmark').onclick=async()=>{")[0] + LARGE_RUN
        if native:
            harness = harness.replace("renderer:'browser'", "renderer:'Electron production preload and IPC'").replace("excluded:['Electron IPC']", "excluded:[]")
            harness += "<script>document.getElementById('runBenchmark').click();</script>"
        html=html.replace('</body>',harness+'</body>')
        lock=threading.RLock()
        body_reads=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def output(self,data,mime="application/json",status=200):
                raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                name=urlsplit(self.path).path
                if name=='/':return self.output(html.encode(),'text/html; charset=utf-8')
                if name=='/fixture-read-audit':return self.output(body_reads)
                if name in ['/style.css','/advisor.css','/message-analysis.css','/media.css','/model-traces.css','/model-traces.js']:
                    return self.output((ROOT/'chatui'/name[1:]).read_bytes(),'application/javascript' if name.endswith('.js') else 'text/css')
                if native and name.startswith(model_traces_http.PREFIX):
                    try:
                        query={key:value[-1] for key,value in parse_qs(urlsplit(self.path).query).items()}
                        result=model_traces_http.get(service,name,query,self.headers)
                        if large and name.endswith('/body'):
                            body_reads.append({'kind':query.get('kind'),'offset':int(query.get('offset',0)),'limit':int(query.get('limit',0)),'returnedBytes':len(result.get('text','').encode('utf-8')),'totalBytes':result.get('totalBytes'),'nextOffset':result.get('nextOffset')})
                        return self.output(result)
                    except TraceError as error:
                        return self.output({'error':error.code,'message':str(error)},status=error.status)
                self.send_error(404)
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
                if self.path=='/fixture-shutdown' and native and self.headers.get('X-Fixture-Control') == control_token:
                    self.output({'stopping':True});threading.Thread(target=server.shutdown,daemon=True).start();return
                if self.path=='/benchmark-results':
                    filename='model-trace-electron-large-viewer-benchmark.json' if large and native else 'model-trace-large-viewer-benchmark.json' if large else 'model-trace-electron-viewer-benchmark.json' if native else 'model-trace-viewer-benchmark.json'
                    output=ROOT/'.local/tmp'/filename;data['platform']=platform.platform();data['databaseBytes']=store.usage();output.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');self.output({'saved':True})
                    # The desktop runner still needs a live service for its final
                    # rendered screenshot; it authenticates an explicit shutdown.
                    if not native:
                        threading.Thread(target=server.shutdown,daemon=True).start()
                    return
                if native and self.path.startswith(model_traces_http.PREFIX):
                    try:
                        return self.output(model_traces_http.post(service,self.path,data,self.headers,control_token))
                    except TraceError as error:
                        return self.output({'error':error.code,'message':str(error)},status=error.status)
                if self.path!='/fixture-api':self.send_error(404);return
                operation,args=data['operation'],data.get('args',{})
                with lock:
                    if operation=='scope':result={'accountId':viewer['accountId'],'scope':args['scope']}
                    elif operation in ('state','events'):result=service.read(viewer['token'],operation,args)
                    elif operation=='records':result=service.read(viewer['token'],'records',{**args,'limit':50})
                    elif operation=='detail':result=service.read(viewer['token'],'detail',{'recordId':args['id']})
                    elif operation=='body':result=service.read(viewer['token'],'body',{'recordId':args['id'],'kind':args['kind'],'offset':args.get('offset',0),'limit':65536})
                    else:result={}
                self.output({'ok':True,**result,'deleteVersion':0})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        if native and control.get('parentPid'):
            parent = psutil.Process(control['parentPid'])
            def watch_parent():
                while parent.is_running():
                    time.sleep(1)
                server.shutdown()
            threading.Thread(target=watch_parent,daemon=True).start()
        print('SYNTHETIC_BENCHMARK_URL=http://127.0.0.1:'+str(server.server_address[1])+'/',flush=True)
        try:server.serve_forever()
        finally:server.server_close();service.close()

if __name__=='__main__':main()
