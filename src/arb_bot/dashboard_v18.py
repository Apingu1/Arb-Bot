from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .dashboard_v17 import DashboardStateV17, ENHANCED_HTML
from .runtime_controls_v18 import runtime_controls
from .dashboard import _jsonable


log = logging.getLogger(__name__)


class DashboardStateV18(DashboardStateV17):
    def publish(self, *args, **kwargs):
        state = super().publish(*args, **kwargs)
        state["runtime_controls"] = runtime_controls.snapshot()
        state["feed_assets"] = list(self.settings.market_assets)
        state["active_model_assets"] = {
            row["model"]: row["enabled_assets"]
            for row in state["runtime_controls"]["models"]
        }
        with self._lock:
            self._state = state
        return _jsonable(state)


CONTROL_PANEL = r'''
<section class="panel markets"><div class="panel-title"><span>MODEL × ASSET CONTROL</span><span class="label">runtime · session only · feed remains on all configured assets</span></div>
<div id="controlSummary" class="sub" style="margin:8px 0 12px"></div>
<div class="table-wrap"><table><thead><tr><th>MODEL</th><th>MODEL</th><th>BTC</th><th>ETH</th><th>BNB</th><th>SOL</th><th>XRP</th><th>DOGE</th><th>HYPE</th><th>ACTIVE ASSETS</th></tr></thead><tbody id="controlRows"></tbody></table></div></section>
'''

SCRIPT = r'''
<script>
(function(){
  const assets=['BTC','ETH','BNB','SOL','XRP','DOGE','HYPE'];
  function checked(v){return v?'checked':''}
  async function postControl(payload){
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!r.ok){console.error('control update failed',await r.text());return}
    if(typeof load==='function') load();
  }
  window.toggleModel=function(model,enabled){postControl({model:model,enabled:enabled});}
  window.toggleModelAsset=function(model,asset,enabled){postControl({model:model,asset:asset,enabled:enabled});}
  window.renderRuntimeControls=function(){
    const c=(state&&state.runtime_controls)||{models:[]};
    const rows=c.models||[];
    const feed=(state&&state.feed_assets)||[];
    const activeCombos=rows.reduce((n,r)=>n+(r.enabled_assets||[]).length,0);
    const summary=document.getElementById('controlSummary');
    if(summary) summary.textContent='FEED ASSETS: '+feed.join(', ')+'  //  ACTIVE MODEL×ASSET COMBINATIONS: '+activeCombos;
    const body=document.getElementById('controlRows');
    if(!body) return;
    body.innerHTML=rows.map(r=>{
      const aset={};(r.assets||[]).forEach(x=>aset[x.asset]=!!x.enabled);
      return `<tr><td class="strategy">${r.model}</td><td><input type="checkbox" ${checked(r.enabled)} onchange="toggleModel('${r.model}',this.checked)"></td>${assets.map(a=>`<td><input type="checkbox" ${checked(aset[a])} onchange="toggleModelAsset('${r.model}','${a}',this.checked)"></td>`).join('')}<td>${(r.enabled_assets||[]).join(', ')||'OFF'}</td></tr>`
    }).join('')||'<tr><td colspan="10">No runtime-controlled models.</td></tr>';
  }
  const original=window.renderAll;
  if(typeof original==='function') window.renderAll=function(){original();window.renderRuntimeControls();};
  setInterval(()=>{try{window.renderRuntimeControls()}catch(e){}},1000);
})();
</script>
'''

V18_HTML = ENHANCED_HTML.replace(
    '<section class="panel markets"><div class="panel-title"><span>ASSET × MODEL ATTRIBUTION</span>',
    CONTROL_PANEL + '<section class="panel markets"><div class="panel-title"><span>ASSET × MODEL ATTRIBUTION</span>',
)
V18_HTML = V18_HTML.replace('</body>', SCRIPT + '</body>')


class DashboardServerV18:
    def __init__(self, settings, state_getter=None) -> None:
        self.settings = settings
        self.state_getter = state_getter
        self._server = None
        self._thread = None

    def _read_state_file(self):
        try:
            with open(self.settings.dashboard_state_path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except Exception:
            return {}

    def start(self) -> None:
        if self._server is not None:
            return
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(self, status: int, body: bytes, content_type: str = 'application/json') -> None:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path in {'/', '/index.html'}:
                    self._send(200, V18_HTML.encode('utf-8'), 'text/html; charset=utf-8')
                elif self.path.startswith('/api/state'):
                    state = parent.state_getter() if parent.state_getter is not None else parent._read_state_file()
                    self._send(200, json.dumps(_jsonable(state), separators=(',', ':')).encode('utf-8'))
                elif self.path == '/api/control':
                    self._send(200, json.dumps(runtime_controls.snapshot(), separators=(',', ':')).encode('utf-8'))
                elif self.path == '/health':
                    self._send(200, b'{"ok":true}')
                else:
                    self._send(404, b'not found', 'text/plain; charset=utf-8')

            def do_POST(self) -> None:
                if self.path != '/api/control':
                    self._send(404, b'not found', 'text/plain; charset=utf-8')
                    return
                try:
                    length = int(self.headers.get('Content-Length') or '0')
                    payload = json.loads(self.rfile.read(length) or b'{}')
                    model = str(payload['model'])
                    enabled = bool(payload['enabled'])
                    asset = payload.get('asset')
                    if asset is None:
                        runtime_controls.set_model(model, enabled)
                    else:
                        runtime_controls.set_asset(model, str(asset), enabled)
                    self._send(200, json.dumps(runtime_controls.snapshot(), separators=(',', ':')).encode('utf-8'))
                except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send(400, json.dumps({'error': str(exc)}).encode('utf-8'))

        try:
            self._server = ThreadingHTTPServer((self.settings.dashboard_host, self.settings.dashboard_port), Handler)
        except OSError as exc:
            log.warning('Dashboard could not bind %s:%d: %s', self.settings.dashboard_host, self.settings.dashboard_port, exc)
            self._server = None
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name='arb-dashboard-v18', daemon=True)
        self._thread.start()
        log.info('ARB//TERM v1.8 dashboard listening on http://%s:%d', self.settings.dashboard_host, self.settings.dashboard_port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
