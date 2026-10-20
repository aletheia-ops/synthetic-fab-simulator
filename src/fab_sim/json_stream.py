"""Finite JSON in small UTF-8 pieces; exact rationals use numerator/denominator."""
from decimal import Decimal
from fractions import Fraction
import json
import math


def json_chunks(value):
    if isinstance(value,str):
        yield b'"'
        for offset in range(0,len(value),1024):
            yield json.dumps(value[offset:offset+1024],ensure_ascii=False)[1:-1].encode('utf-8')
        yield b'"'
    elif value is None:yield b'null'
    elif isinstance(value,bool):yield b'true' if value else b'false'
    elif isinstance(value,int):yield str(value).encode('ascii')
    elif isinstance(value,float):
        if not math.isfinite(value):raise ValueError('JSON numbers must be finite')
        yield repr(value).encode('ascii')
    elif isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('JSON numbers must be finite')
        yield str(value).encode('ascii')
    elif isinstance(value,Fraction):
        yield from json_chunks({'numerator':value.numerator,'denominator':value.denominator})
    elif isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError('JSON object keys must be strings')
        yield b'{'
        for i,key in enumerate(sorted(value)):
            if i:yield b','
            yield from json_chunks(key);yield b':';yield from json_chunks(value[key])
        yield b'}'
    elif isinstance(value,(tuple,list)):
        yield b'['
        for i,item in enumerate(value):
            if i:yield b','
            yield from json_chunks(item)
        yield b']'
    else:raise ValueError('unsupported JSON value: '+type(value).__name__)


def _plain(text):
    # Strings json.dumps(ensure_ascii=False) emits unchanged inside quotes.
    return text.isascii() and text.isprintable() and '"' not in text and '\\' not in text


def json_bytes(value):
    """The bytes json_chunks yields, built in one pass without generators."""
    parts=[];append=parts.append
    def encode(item):
        kind=type(item)
        if kind is str:
            append(b'"'+item.encode('ascii')+b'"' if _plain(item) else b''.join(json_chunks(item)))
        elif item is None:append(b'null')
        elif kind is bool:append(b'true' if item else b'false')
        elif kind is int:append(str(item).encode('ascii'))
        elif kind is dict:
            if any(type(k) is not str for k in item):
                append(b''.join(json_chunks(item)));return
            append(b'{');first=True
            for key in sorted(item):
                if not first:append(b',')
                first=False;encode(key);append(b':');encode(item[key])
            append(b'}')
        elif kind is list or kind is tuple:
            append(b'[');first=True
            for element in item:
                if not first:append(b',')
                first=False;encode(element)
            append(b']')
        else:append(b''.join(json_chunks(item)))
    encode(value)
    return b''.join(parts)


def bounded_line(value,limit):
    line=json_bytes(value)
    if len(line)+1>limit:raise ValueError(f'record exceeds configured maximum of {limit} bytes')
    return line+b'\n'
