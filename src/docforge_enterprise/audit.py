from __future__ import annotations
import re
from typing import Any
def collect_claims(records:list[dict[str,Any]])->list[dict[str,Any]]:
    claims=[]
    for r in records:
        for key in ("purpose","public_api","internal_logic","business_rules","interfaces","security_notes","operations_notes","risks","enterprise_notes","documentation_notes"):
            val=r.get(key)
            items=val if isinstance(val,list) else [val] if isinstance(val,str) else []
            for item in items:
                if item and str(item).strip(): claims.append({"source_id":r.get("file_path") or r.get("module_name") or r.get("shard_id",""),"claim":str(item),"evidence":r.get("evidence",[])})
    return claims
def validate_claims(records:list[dict[str,Any]], source_files:dict[str,str])->dict[str,Any]:
    claims=collect_claims(records); supported=[]; unsupported=[]
    for c in claims:
        ev=c.get("evidence") or []
        ok=False
        if isinstance(ev,list):
            for e in ev:
                fp=e.get("file_path") if isinstance(e,dict) else None
                if fp and fp in source_files: ok=True
        if ok: supported.append(c)
        else: unsupported.append(c)
    total=len(claims); cov=(len(supported)/total*100.0) if total else 100.0
    return {"claims_total":total,"claims_supported":len(supported),"claims_unsupported":len(unsupported),"evidence_coverage_percent":round(cov,2),"unsupported_claims":unsupported[:200]}
def append_audit_section(markdown:str, report:dict[str,Any])->str:
    return markdown + "\n\n## Audit-Validation\n\n" + f"- Claims total: {report['claims_total']}\n- Supported: {report['claims_supported']}\n- Unsupported: {report['claims_unsupported']}\n- Evidence coverage: {report['evidence_coverage_percent']}%\n"
