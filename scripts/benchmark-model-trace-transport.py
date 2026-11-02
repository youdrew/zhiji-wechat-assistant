"""Synthetic, independent latency dataset through production HTTP + SQLite."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from model_traces import ModelTraceService
from real_http import make_handler, LocalHTTPServer


def main():
    desktop = "--desktop" in sys.argv
    with tempfile.TemporaryDirectory(prefix="trace-latency-", dir=ROOT / ".local/tmp") as folder:
        service = ModelTraceService(Path(folder), current_account=lambda: "synthetic-account")
        viewer = service.create_session("benchmark-latency")
        state = {"producer": None, "panel": False, "closing": False, "ready": None}
        readiness = threading.Condition()
        control = secrets.token_hex(32)
        monitor = None
        handler = make_handler(SimpleNamespace(model_traces=service, request_lease=nullcontext), control_token=control)
        html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', (ROOT / 'chatui/index.html').read_text(encoding='utf-8'))
        # Explicit viewer mode for the synthetic latency fixture, not the main app.
        html = html.replace('</body>', '<style>#modelTracesPanel{position:fixed;inset:0;z-index:48}</style><script src="/model-traces.js"></script><script>ModelTraces.init({standalone:true,account:"synthetic-account",user:"synthetic-contact",view:"chat"});</script></body>')
        original_get, original_post = handler.do_GET, handler.do_POST
        def get(self):
            if self.path == "/fixture-viewer-state":
                return self.send(200, {"panel": state["panel"], "finished": state["closing"]})
            if self.path == "/fixture-viewer" or self.path in ("/style.css", "/advisor.css", "/message-analysis.css", "/media.css", "/model-traces.css", "/model-traces.js"):
                raw = html.encode('utf-8') if self.path == '/fixture-viewer' else (ROOT / 'chatui' / self.path[1:]).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path == '/fixture-viewer' else 'application/javascript' if self.path.endswith('.js') else 'text/css')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
                return
            if self.path != "/fixture-settle":
                return original_get(self)
            service.flush()
            listing = service.read(viewer["token"], "records", {"limit": 1})
            latest = listing["records"][0] if listing["records"] else {}
            self.send(200, {"collecting": service.snapshot()["collecting"],
                "pending": latest.get("collectionStatus") == "recording",
                "incomplete": bool(latest and not latest.get("complete"))})
        def post(self):
            if self.path == "/fixture-viewer-ready":
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with readiness:
                    state["ready"] = request.get("panel")
                    readiness.notify_all()
                return self.send(200, {"ready": True})
            if self.path != "/fixture-prepare":
                return original_post(self)
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if state["producer"]:
                service.unregister(state["producer"])
            if service.snapshot()["enabled"] != request["enabled"]:
                service.update_settings({"enabled": request["enabled"]})
            state["panel"] = request["panel"]
            if desktop:
                with readiness:
                    if not readiness.wait_for(lambda: state["ready"] == state["panel"], timeout=5):
                        return self.send(503, {"error": "fixture-viewer-not-ready"})
            context = service.register_execution({"accountId": "synthetic-account", "contactId": "synthetic-contact", "source": "advisor"})
            state["producer"] = context["producerId"]
            context.update(service.bind_transport(context["producerId"], child.pid))
            self.send(200, context)
        handler.do_GET, handler.do_POST = get, post
        server = LocalHTTPServer(("127.0.0.1", 0), handler)
        service.transport_url = "http://127.0.0.1:" + str(server.server_port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def panel_reader():
            while not state["closing"]:
                if state["panel"] and not desktop:
                    listing = service.read(viewer["token"], "records", {"limit": 50})
                    if listing["records"]:
                        record_id = listing["records"][0]["recordId"]
                        service.read(viewer["token"], "detail", {"recordId": record_id})
                        service.read(viewer["token"], "body", {"recordId": record_id, "kind": "response", "limit": 65536})
                time.sleep(.3)
        reader = threading.Thread(target=panel_reader, daemon=True)
        reader.start()
        try:
            if desktop:
                monitor = subprocess.Popen([str(ROOT / 'node_modules/electron/dist/electron.exe'),
                    str(ROOT / 'scripts/benchmark-model-trace-desktop.cjs'), '--monitor', service.transport_url + '/'],
                    cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', env={**os.environ, 'KYF_TRACE_FIXTURE_TOKEN': control},
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                monitor.stdin.close()
            child = subprocess.Popen([shutil.which("node"), "--import", "tsx", str(ROOT / "scripts/benchmark-model-trace-transport.ts")],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            stdout, stderr = child.communicate(json.dumps({"endpoint": service.transport_url, "desktop": desktop}), timeout=900)
            if child.returncode:
                raise RuntimeError(stderr[-3000:])
            result = json.loads(stdout)
            result["storage"] = {"bytes": service._store.usage(), "state": service.snapshot()}
            output = ROOT / (".local/tmp/model-trace-desktop-transport-benchmark.json" if desktop else ".local/tmp/model-trace-transport-benchmark.json")
            output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"passed": result["passed"], "deltas": result["deltas"],
                "relayOverhead": result["relayOverhead"], "report": str(output)}, ensure_ascii=False))
        finally:
            state["closing"] = True
            if monitor:
                try:
                    monitor.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    monitor.terminate(); monitor.wait(timeout=5)
            reader.join(2)
            server.shutdown(); server.server_close(); service.close()


if __name__ == "__main__":
    main()
