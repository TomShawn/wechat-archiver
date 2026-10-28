import importlib.util
import os
import platform
import socket
import ssl
import subprocess

import certifi
import httpx

from .models import now


def diagnose(config):
    result = {"checked_at": now(), "platform": platform.platform(), "python": platform.python_version(),
              "proxy_environment_variables_present": [name for name in
                  ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
                  if os.environ.get(name)],
              "certificate_verification": "enabled; certifi", "dns": {}, "requests": {},
              "vision_installed": importlib.util.find_spec("Vision") is not None,
              "scope": "仅检查域名、首页和搜索 robots；不等于验证真实文章可访问"}
    if platform.system() == "Darwin":
        output = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, check=False).stdout
        import re
        result["system_proxy_enabled"] = {name: bool(re.search(rf"{name}\s*:\s*1", output))
                                          for name in ("HTTPEnable", "HTTPSEnable", "SOCKSEnable", "ProxyAutoConfigEnable")}
    with httpx.Client(verify=ssl.create_default_context(cafile=certifi.where()), timeout=config.timeout) as client:
        for host in ("mp.weixin.qq.com", "weixin.sogou.com"):
            try:
                result["dns"][host] = sorted({info[4][0] for info in socket.getaddrinfo(host, 443)})
            except OSError as exc:
                result["dns"][host] = str(exc)
        for url in ("https://mp.weixin.qq.com/", "https://weixin.sogou.com/robots.txt"):
            try:
                response = client.get(url)
                result["requests"][url] = {"status": response.status_code, "bytes": len(response.content),
                                          "article_container_present": 'id="js_content"' in response.text}
                if url.endswith("robots.txt"):
                    result["requests"][url]["robots_text"] = response.text
                    result["requests"][url]["search_adapter_enabled"] = False
            except httpx.HTTPError as exc:
                result["requests"][url] = {"error": f"{type(exc).__name__}: {exc}"}
    return result

