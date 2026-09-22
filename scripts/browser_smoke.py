"""Optional end-to-end check. Requires Playwright and Chromium, not needed to run the app."""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time
import httpx

from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]
out=root/'artifacts'
out.mkdir(exist_ok=True)
env={**os.environ,'PYTHONPATH':str(root/'src')}
command=[sys.executable,'-m','arc_monitor','--db',str(root/'data/demo.db'),'--mode','demo']
subprocess.run(command+['demo'],cwd=root,env=env,check=True,capture_output=True)
with socket.socket() as s:
    s.bind(('127.0.0.1',0))
    port=s.getsockname()[1]
log=(out/'browser-server.log').open('w')
server=subprocess.Popen(command+['serve','--port',str(port)],cwd=root,env=env,stdout=log,stderr=log)
result={'external_requests':[],'console_errors':[],'checks':[]}
try:
    for attempt in range(100):
        if server.poll() is not None:
            raise RuntimeError('Server exited; see browser-server.log')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.1): break
        except OSError: time.sleep(.1)
    else: raise RuntimeError('Server did not start')
    with sync_playwright() as p:
        executable=os.getenv('CHROMIUM_PATH')
        if executable is None and Path('/usr/bin/chromium').exists(): executable='/usr/bin/chromium'
        browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1600,'height':1100},device_scale_factor=1)
        def restrict(route):
            if route.request.url.startswith(f'http://127.0.0.1:{port}/'):
                route.continue_()
            else:
                result['external_requests'].append(route.request.url)
                route.abort()
        context.route('**/*',restrict)
        page=context.new_page()
        page.on('pageerror',lambda e:result['console_errors'].append(str(e)))
        page.on('console',lambda m:result['console_errors'].append(m.text) if m.type=='error' else None)
        base=f'http://127.0.0.1:{port}'
        # The managed Chromium in this execution environment blocks localhost navigation.
        # Test the real HTTP server separately, then bridge its responses into an offline page.
        with httpx.Client(base_url=base,trust_env=False) as client:
            home=client.get('/')
            assert home.status_code==200 and 'Content-Security-Policy' in home.headers
            assert client.get('/assets/app.js').status_code==200
        result['checks'].append('real local HTTP server: HTML, static JS and security headers')
        def backend(path):
            if not path.startswith('/api/'):
                raise ValueError('Only local API reads are allowed in the offline browser bridge')
            with httpx.Client(base_url=base,trust_env=False) as client:
                response=client.get(path)
                return {'status':response.status_code,'body':response.text}
        page.expose_function('localBackend',backend)
        html=(root/'src/arc_monitor/web/index.html').read_text()
        html=html.replace('<link rel="stylesheet" href="/assets/style.css">','')
        html=html.replace('<script src="/assets/app.js" defer></script>','')
        page.set_content(html)
        page.add_style_tag(content=(root/'src/arc_monitor/web/style.css').read_text())
        page.add_script_tag(content="window.fetch=async path=>{const r=await window.localBackend(String(path));return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});};")
        page.add_script_tag(content=(root/'src/arc_monitor/web/app.js').read_text())
        result['browser_mode']='offline page with real HTTP API response bridge; direct navigation blocked by managed Chromium' 
        page.wait_for_function("document.querySelectorAll('#rows tr').length===7")
        page.wait_for_function("document.querySelector('#detail-title').textContent==='GROW'")
        assert page.locator('#mode').inner_text()=='DEMO'
        assert not page.locator('#error').is_visible()
        result['checks'].append('desktop: 7 rows, DEMO warning, default detail and local charts')
        page.screenshot(path=str(out/'dashboard.png'),full_page=True)
        page.locator('#search').fill('LOOP')
        page.wait_for_function("document.querySelectorAll('#rows tr').length===1")
        page.locator('.token-button').click()
        page.wait_for_function("document.querySelector('#detail-title').textContent==='LOOP'")
        result['checks'].append('search and token detail selection')
        page.locator('#search').fill('')
        page.wait_for_function("document.querySelectorAll('#rows tr').length===7")
        page.locator('#stage').select_option('NEW')
        page.wait_for_function("document.querySelectorAll('#rows tr').length===1")
        assert 'EARLY' in page.locator('#rows').inner_text()
        page.locator('#stage').select_option('')
        page.wait_for_function("document.querySelectorAll('#rows tr').length===7")
        result['checks'].append('stage filter')
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_timeout(200)
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        page.screenshot(path=str(out/'dashboard-mobile.png'),full_page=True)
        result['checks'].append('390px viewport without document overflow; table scrolls inside container')
        browser.close()
    assert not result['external_requests'], result['external_requests']
    assert not result['console_errors'], result['console_errors']
    result['passed']=True
finally:
    server.terminate()
    try: server.wait(timeout=5)
    except subprocess.TimeoutExpired: server.kill(); server.wait()
    log.close()
    (out/'browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
