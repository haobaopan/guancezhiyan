"""Request-scoped OpenAI-compatible clients with a restricted outbound policy."""
from __future__ import annotations
import ipaddress
import http.client
import json
import math
import os
import socket
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler
from urllib.error import HTTPError, URLError

class ModelError(RuntimeError):
    pass

DEFAULT_HOSTS = {"api.siliconflow.cn", "api.deepseek.com", "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}


def validate_url(url: str) -> str:
    """Only configured HTTPS DNS origins; no credentials, IPs, ports or redirects."""
    try:
        parts=urlsplit(url.strip())
        host=(parts.hostname or "").lower()
        hosts=DEFAULT_HOSTS | {x.strip().lower() for x in os.getenv("MODEL_ALLOWED_HOSTS", "").split(",") if x.strip()}
        if parts.scheme != "https" or not host or host not in hosts or parts.username or parts.password or parts.port not in (None,443) or parts.query or parts.fragment:
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        if ".." in parts.path.split("/"):
            raise ValueError()
        # Check all DNS answers, including IPv6, before making the request.
        answers=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        if not answers or any(not ipaddress.ip_address(x[4][0]).is_global for x in answers):
            raise ValueError()
        return "https://"+host+parts.path.rstrip("/")
    except (ValueError, TypeError, AttributeError, OSError):
        raise ModelError("Model endpoint is not an allowed public HTTPS address") from None

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError("Model endpoint redirects are not allowed")

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Preserve the original hostname for TLS SNI/certificate verification,
        # but connect to the already checked IP without resolving it a second time.
        try:
            addresses=socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise ModelError("Model DNS resolved to a non-public address")
        except OSError:
            raise ModelError("Model DNS lookup failed") from None
        chosen=addresses[0][4][0]
        self._create_connection=lambda address, timeout=None, source_address=None: socket.create_connection((chosen,address[1]),timeout,source_address)
        super().connect()

class _PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection,req,context=self._context,check_hostname=self._check_hostname)

class Client:
    def __init__(self, config: dict):
        if not isinstance(config, dict): raise ModelError("Model configuration is required")
        self.base=validate_url(str(config.get("base_url", "")))
        self.model=str(config.get("model") or config.get("model_name") or "").strip()
        self._key=str(config.get("api_key") or "").strip()
        if not self.model or len(self.model)>200 or not self._key or len(self._key)>2048:
            raise ModelError("Model name and API key are required")
        self.temperature=float(config.get("temperature",0))
        self.max_tokens=int(config.get("max_tokens",2048))
        self.dimensions=int(config.get("dimensions") or 0)
        if not 0<=self.temperature<=2 or not 1<=self.max_tokens<=32768 or not 0<=self.dimensions<=32768:
            raise ModelError("Invalid model parameters")
        self.parameters={}
        supplied=config.get("parameters") or {}
        if not isinstance(supplied,dict): raise ModelError("Invalid model parameters")
        for key,value in supplied.items():
            if key == "top_p":
                if not isinstance(value,(int,float)) or not 0<=value<=1: raise ModelError("Invalid top_p")
            elif key in ("frequency_penalty","presence_penalty"):
                if not isinstance(value,(int,float)) or not -2<=value<=2: raise ModelError("Invalid penalty")
            elif key == "enable_thinking":
                if not isinstance(value,bool): raise ModelError("Invalid enable_thinking")
            else: raise ModelError("Unsupported model parameter")
            self.parameters[key]=value
        self.config={k:v for k,v in config.items() if k != "api_key"}
        self.config["model"]=self.model
        self._opener=build_opener(ProxyHandler({}),_PinnedHTTPSHandler(),_NoRedirect())

    def __repr__(self):
        return f"Client(model={self.model!r}, base={self.base!r}, api_key=<redacted>)"

    def _request(self, path, body):
        # Recheck DNS for each call; never include body/credentials in exceptions.
        validate_url(self.base)
        req=Request(self.base+path,data=json.dumps(body).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+self._key})
        try:
            with self._opener.open(req,timeout=90) as resp:
                raw=resp.read(8*1024*1024+1)
                if len(raw)>8*1024*1024: raise ModelError("Model response exceeded size limit")
                return json.loads(raw)
        except HTTPError as exc:
            raise ModelError(f"Model provider returned HTTP {exc.code}") from None
        except (URLError, OSError, ValueError, TypeError):
            raise ModelError("Model request failed or returned invalid JSON") from None

    def chat(self, messages):
        if not isinstance(messages,list) or not messages or len(messages)>200:
            raise ModelError("Invalid messages")
        if any(not isinstance(m,dict) or m.get("role") not in ("system","user","assistant") or not isinstance(m.get("content"),str) for m in messages):
            raise ModelError("Invalid messages")
        data=self._request("/chat/completions",{"model":self.model,"messages":messages,"temperature":self.temperature,"max_tokens":self.max_tokens,**self.parameters})
        try: content=data["choices"][0]["message"]["content"]
        except (KeyError,IndexError,TypeError): raise ModelError("Invalid chat response") from None
        if not isinstance(content,str) or not content.strip(): raise ModelError("Empty chat response")
        return content.strip()

    def embed(self,texts):
        if not isinstance(texts,list) or any(not isinstance(t,str) for t in texts):
            raise ModelError("Invalid embedding input")
        if not texts: return []
        if len(texts)>64:
            vectors=[]
            for start in range(0,len(texts),64):
                vectors.extend(self.embed(texts[start:start+64]))
            if len({len(v) for v in vectors})!=1: raise ModelError("Embedding dimension changed across batches")
            return vectors
        data=self._request("/embeddings",{"model":self.model,"input":texts})
        try:
            items=sorted(data["data"],key=lambda x:x["index"])
            if [x["index"] for x in items]!=list(range(len(texts))): raise ValueError()
            vectors=[x["embedding"] for x in items]
            dims={len(v) for v in vectors}
            if len(dims)!=1 or 0 in dims or (self.dimensions and dims!={self.dimensions}): raise ValueError()
            if any(not isinstance(x,(int,float)) or not math.isfinite(x) for v in vectors for x in v): raise ValueError()
            if any(sum(x*x for x in v)<=0 for v in vectors): raise ValueError()
            return vectors
        except (KeyError,IndexError,TypeError,ValueError):
            raise ModelError("Invalid embedding dimensions or vector values") from None
