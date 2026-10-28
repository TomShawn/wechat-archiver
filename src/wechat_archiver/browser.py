import json, subprocess, shutil, uuid, time
from .models import ArchiveError, AccessBlocked

class BrowserFetcher:
    def __init__(self, config):
        self.config=config
        self.session='wa-'+uuid.uuid4().hex[:12]
        self.blocked=False
        self.started=False
    def command(self,*args):
        if not shutil.which('agent-browser'):
            raise ArchiveError('请先安装 agent-browser 及其浏览器运行时')
        result=subprocess.run(['agent-browser','--session',self.session,*args],capture_output=True,text=True,timeout=self.config.timeout+15)
        if result.returncode: raise ArchiveError(result.stderr[:500] or '浏览器命令失败')
        return result.stdout
    def get(self,url,**kwargs):
        if self.blocked: raise AccessBlocked('浏览器遇到验证，当前批次已停止网络访问')
        self.started=True
        self.command('open',url)
        for _ in range(10):
            response=json.loads(self.command('eval',"JSON.stringify({html:document.documentElement.outerHTML,url:location.href,text:document.body.innerText,ready:!!document.querySelector('#js_content')?.textContent.trim()})",'--json'))
            if not response.get('success'): raise ArchiveError('浏览器读取失败')
            data=json.loads(response['data']['result'])
            if 'captcha' in data['url'] or (not data['ready'] and any(x in data['text'] for x in ('验证码','环境异常','访问受限','操作频繁'))):
                self.blocked=True
                raise AccessBlocked('浏览器出现验证或访问限制，请人工处理后重新运行')
            if data['ready']:
                raw=data['html'].encode()
                if len(raw)>self.config.max_html_bytes: raise ArchiveError('页面超过大小限制')
                time.sleep(self.config.request_interval)
                return raw,'text/html',data['url']
            time.sleep(1)
        raise ArchiveError('浏览器没有取得正文：'+data['text'][:150])
    def close(self):
        if self.started:
            try: self.command('close')
            except Exception: pass
