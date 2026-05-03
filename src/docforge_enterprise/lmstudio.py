from __future__ import annotations
import ast, json, re, time, random, urllib.request, urllib.error
from dataclasses import dataclass, field
from typing import Any, Mapping
from .config import LMStudioSettings
class LLMError(RuntimeError): pass
def _retry(fn, attempts:int, backoff:float):
    last=None
    for i in range(max(1,attempts+1)):
        try: return fn()
        except Exception as e:
            last=e
            if "timeout" not in str(e).lower() and "timed out" not in str(e).lower() and i==0: raise
            if i<attempts: time.sleep(backoff*(2**i)+random.random()*0.3)
    raise last
@dataclass(slots=True)
class LMStudioChatClient:
    settings:LMStudioSettings; json_repairs:int=field(default=0,init=False)
    def _base(self):
        return self.settings.base_url.rstrip("/")
    def _post(self,path:str,payload:Mapping[str,Any],timeout:float):
        data=json.dumps(payload,ensure_ascii=False).encode()
        req=urllib.request.Request(self._base()+"/"+path.lstrip("/"),data=data,headers={"Content-Type":"application/json"},method="POST")
        def once():
            with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read().decode())
        try: return _retry(once,self.settings.request_retries,self.settings.retry_backoff_seconds)
        except Exception as e: raise LLMError(str(e)) from e
    def chat(self,*,system:str,user:str,temperature:float|None=None,max_tokens:int|None=None,timeout:float|None=None,label:str="chat")->str:
        payload={"model":self.settings.chat_model,"temperature": self.settings.temperature if temperature is None else temperature,"messages":[{"role":"system","content":system},{"role":"user","content":user}]}
        if max_tokens: payload["max_tokens"]=max_tokens
        r=self._post("/chat/completions",payload,timeout or self.settings.chat_timeout_seconds)
        return r["choices"][0]["message"]["content"]
    def chat_json(self,*,system:str,user:str,max_tokens:int|None=None,timeout:float|None=None,label:str="chat_json")->dict[str,Any]:
        raw=self.chat(system=system,user=user,max_tokens=max_tokens,timeout=timeout,label=label)
        try: return extract_json(raw)
        except LLMError:
            repair=self.chat(system="Return only valid JSON object.",user=f"Repair this to strict JSON:\n{raw[:10000]}",temperature=0,max_tokens=max_tokens,timeout=timeout,label=label+".repair")
            self.json_repairs+=1
            return extract_json(repair)
def extract_json(raw:str)->dict[str,Any]:
    t=raw.strip()
    if t.startswith("```"): t=re.sub(r"^```(?:json)?","",t).strip(); t=re.sub(r"```$","",t).strip()
    for cand in [t]+re.findall(r"\{.*?\}",t,re.S):
        for x in [cand, re.sub(r",\s*([}\]])",r"\1",cand)]:
            try:
                v=json.loads(x)
                if isinstance(v,dict): return v
            except Exception: pass
            try:
                v=ast.literal_eval(x)
                if isinstance(v,dict): return {str(k):val for k,val in v.items()}
            except Exception: pass
    raise LLMError("No valid JSON object found")
