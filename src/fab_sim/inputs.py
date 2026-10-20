"""Strict JSON and content-bound preparation records; no live state."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

def _pairs(items):
    out={}
    for key,value in items:
        if key in out:raise ValueError(f'duplicate JSON key: {key}')
        out[key]=value
    return out

def _constant(value):raise ValueError(f'nonfinite JSON number: {value}')

def read_json(path: Path) -> dict:
    value=json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=_pairs,
                     parse_float=Decimal,parse_constant=_constant)
    if not isinstance(value,dict):raise ValueError('JSON root must be an object')
    return value

def canonical_text(value) -> str:
    if isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError('JSON object keys must be strings')
        return '{'+','.join(json.dumps(k,ensure_ascii=False)+':'+canonical_text(value[k]) for k in sorted(value))+'}'
    if isinstance(value,(tuple,list)):return '['+','.join(canonical_text(x) for x in value)+']'
    if isinstance(value,float): value=Decimal(str(value))
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('JSON numbers must be finite')
        if value==0:return '0'
        number=format(value,'f')
        return number.rstrip('0').rstrip('.') if '.' in number else number
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))

def canonical_digest(value: dict) -> str:
    return hashlib.sha256(canonical_text(value).encode('utf-8')).hexdigest()

def validate_keys(value: dict, allowed: set[str], where: str) -> None:
    if not isinstance(value,dict):raise ValueError(f'{where}: expected object')
    unknown=set(value)-allowed-{'extensions'}
    if unknown:raise ValueError(f'{where}: unknown keys: {sorted(unknown)}')
    if 'extensions' in value and not isinstance(value['extensions'],dict):raise ValueError(f'{where}: extensions must be an object')

def _content(artifact):
    return {k:artifact[k] for k in ('schema_version','model','provenance')}

def bind_validation(artifact: dict, requirements: dict, validator_version: str) -> dict:
    return {**artifact,'validation':{'validated':True,'content_sha256':canonical_digest(_content(artifact)),
            'requirements_sha256':canonical_digest(requirements),'validator_version':validator_version,
            'adapter_version':artifact['provenance'].get('adapter_version')}}

def verify_binding(artifact: dict, requirements: dict, validator_version: str) -> bool:
    v=artifact.get('validation',{})
    try:
        return (v.get('validated') is True and v.get('validator_version')==validator_version
                and v.get('adapter_version')==artifact['provenance'].get('adapter_version')
                and v.get('requirements_sha256')==canonical_digest(requirements)
                and v.get('content_sha256')==canonical_digest(_content(artifact)))
    except (KeyError,TypeError,ValueError):return False
