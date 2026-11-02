"""Model Client operations for one policy engine instance."""
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen
import json
import re
import struct
import time


class ModelClientMixin:
    def open_api(self, req, timeout=90):
        """访问模型接口。目标是本机回环地址时绕过系统代理。

    本机跑的嵌入/推理服务（Ollama、LM Studio、vLLM 等）经常落在 127.0.0.1，
    而机器上若配了 HTTP_PROXY，urllib 会把请求发给代理，直接连不上真实的本地端口。
    """
        host = (urlparse(req.full_url).hostname or '').strip()
        if self.is_loopback(host):
            return self._NO_PROXY_OPENER.open(req, timeout=timeout)
        return urlopen(req, timeout=timeout)


    def embedding_key(self):
        return (self.environment.getenv('EMBEDDING_API_KEY') or self.environment.getenv('LLM_API_KEY','')).strip()


    def embedding_base(self):
        return (self.environment.getenv('EMBEDDING_BASE_URL') or self.environment.getenv('LLM_BASE_URL','https://api.openai.com/v1')).rstrip('/')


    def embedding_enabled(self):
        """有密钥即启用；**回环地址的自建服务不需要密钥**（Ollama / LM Studio / vLLM / TEI）。

    只认密钥会让本机部署误判成「未配置」：服务明明在 127.0.0.1:11434 上跑着，
    却因为没填 Authorization 而永远不启用语义召回。
    """
        if self.embedding_key(): return True
        return self.is_loopback(urlparse(self.embedding_base()).hostname)


    def embedding_keyless(self):
        """真正的「免密钥」：没填密钥，且地址是回环，服务本身不校验 Authorization。

    只写 `not embedding_key()` 会把「云端地址但忘了填密钥」也标成免密钥，
    让人误以为链路可用；这里要求同时是回环，语义才准。
    """
        return (not self.embedding_key()) and self.is_loopback(urlparse(self.embedding_base()).hostname)


    def pack_vector(self, vector):
        return struct.pack('<%sf'%len(vector),*vector)


    def unpack_vector(self, blob):
        return struct.unpack('<%sf'%(len(blob)//4),blob) if blob else ()


    def embed_texts(self, texts):
        """Call an OpenAI-compatible /embeddings endpoint in bounded batches."""
        if not texts or not self.embedding_enabled(): return []
        key=self.embedding_key(); base=self.embedding_base()
        headers={'Content-Type':'application/json'}
        if key: headers['Authorization']='Bearer '+key
        out=[]
        for start in range(0,len(texts),self.EMBEDDING_BATCH_SIZE):
            batch=texts[start:start+self.EMBEDDING_BATCH_SIZE]
            payload=json.dumps({'model':self.EMBEDDING_MODEL,'input':batch}).encode()
            req=Request(base+'/embeddings',data=payload,headers=headers)
            try:
                with self.open_api(req,timeout=90) as resp: data=json.loads(resp.read())
                out.extend(x['embedding'] for x in sorted(data['data'],key=lambda x:x['index']))
            except (HTTPError,URLError,TimeoutError,KeyError,IndexError,TypeError,ValueError) as e:
                raise RuntimeError(f'嵌入接口调用失败（{self.EMBEDDING_MODEL}）：{e}')
        return out


    def set_deadline(self, seconds=None):
        """Arm a per-request wall-clock budget so a single answer cannot blow past 180s."""
        self._DEADLINE.value = time.monotonic() + (seconds if seconds is not None else self.ANSWER_DEADLINE_SECONDS)


    def seconds_left(self):
        deadline = getattr(self._DEADLINE, 'value', None)
        return None if deadline is None else deadline - time.monotonic()


    def model_call(self, messages, temperature=0, max_tokens=None):
        key=self.environment.getenv('LLM_API_KEY','').strip()
        if not key: return None
        base=self.environment.getenv('LLM_BASE_URL','https://api.openai.com/v1').rstrip('/')
        model=self.environment.getenv('LLM_MODEL','gpt-4o-mini')
        body={'model':model,'messages':messages,'temperature':temperature}
        if max_tokens: body['max_tokens']=max_tokens
        payload=json.dumps(body).encode()
        req=Request(base+'/chat/completions',data=payload,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        last=None
        for attempt in range(int(self.environment.getenv('LLM_MAX_RETRIES','3'))):
            left=self.seconds_left()
            if left is not None and left<=1: raise RuntimeError('本次问答已超出时间预算，已中止模型调用。')
            timeout=90 if left is None else max(5.0,min(90.0,left))
            try:
                with self.open_api(req,timeout=timeout) as resp: data=json.loads(resp.read())
                content=data['choices'][0]['message']['content']
                if not isinstance(content,str) or not content.strip(): raise ValueError('模型返回内容为空或格式错误')
                return content.strip()
            except HTTPError as e:
                last=e
                if e.code in (401,402,403):
                    reason={401:'密钥无效或未授权',402:'账户余额或免费额度不足',403:'无权限使用该模型'}[e.code]
                    raise self.ModelUnavailable(f'模型接口返回 HTTP {e.code}：{reason}')
                if e.code not in (408,429,500,502,503,504) or attempt==int(self.environment.getenv('LLM_MAX_RETRIES','3'))-1:
                    raise RuntimeError(f'模型接口调用失败：HTTP {e.code}')
                time.sleep(min(2**attempt,8))
            except (URLError,TimeoutError,KeyError,IndexError,TypeError,ValueError) as e:
                last=e
                if attempt==int(self.environment.getenv('LLM_MAX_RETRIES','3'))-1: raise RuntimeError(f'模型接口调用失败：{e}')
                time.sleep(min(2**attempt,8))
        raise RuntimeError(f'模型接口调用失败：{last}')


    def parse_model_json(self, content):
        """Accept only a complete JSON object; never salvage malformed model text."""
        value=content.strip()
        if value.startswith('```'):
            value=re.sub(r'^```(?:json)?\s*|\s*```$', '', value, flags=re.I)
        data=json.loads(value)
        if not isinstance(data,dict): raise ValueError('模型输出不是 JSON 对象')
        return data


