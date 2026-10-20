"""Non-executable, exact native checkpoint values and format records."""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import base64
import json
import sys
import math
import re


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointManifest:
    format_version: int
    state_contract: str
    writer_build: str
    runtime_compatibility: dict
    physical_identity: dict
    saved_tick: int
    settlements: int
    parent_run_id: str
    parent_checkpoint: str | None
    last_committed_sequence: int
    physical_digest: str
    payload_checksums: dict


@dataclass(frozen=True)
class CheckpointBundle:
    manifest: CheckpointManifest
    state: dict[str, object]


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def _PLAIN(key):
    return key.isascii() and key.isprintable() and '"' not in key and '\\' not in key and '\x7f' not in key


def encode_value(value: object) -> bytes:
    return _json(tagged_value(value))


class LazyList:
    """A list whose items are produced on demand, encoded exactly like a list.

    write_encoded_value streams it item by item, so the items never exist all
    at once; tagged_value materializes it."""
    __slots__ = ('_length', '_items')

    def __init__(self, length, items):
        self._length = length; self._items = items

    def __len__(self):
        return self._length

    def __iter__(self):
        count = 0
        for item in self._items():
            count += 1
            yield item
        if count != self._length: raise CheckpointError('lazy list length changed')


# The encoders below recurse through module-level functions, not nested
# closures: a self-referencing closure is a reference cycle, which leaks every
# call while the garbage collector is off (as it is during checkpoint capture).

def _emit(v, put, active, quote):
    t = type(v)
    if v is None: put('["none"]'); return
    if t is bool: put('["bool",true]' if v else '["bool",false]'); return
    if t is int: put('["int","' + str(v) + '"]'); return
    if t is str: put('["str",' + quote(v) + ']'); return
    if t is bytes or t is float or t is Decimal or t is Fraction:
        put(_json(tagged_value(v)).decode('ascii')); return
    if t is LazyList:
        put('["list",[')
        for i, x in enumerate(v):
            if i: put(',')
            _emit(x, put, active, quote)
        put(']]'); return
    if t not in (dict, list, tuple, set, frozenset):
        raise CheckpointError('unsupported checkpoint value: ' + t.__name__)
    if id(v) in active: raise CheckpointError('cyclic checkpoint value')
    active.add(id(v))
    try:
        if t is dict:
            if all(type(k) is str and _PLAIN(k) for k in v):
                keys = sorted(v, key=_plain_key)
                encoded = {k: '["str",' + quote(k) + ']' for k in keys}
            else:
                encoded = {k: _json(tagged_value(k)).decode('ascii') for k in v}
                keys = sorted(v, key=encoded.__getitem__)
            put('["dict",[')
            for i, k in enumerate(keys):
                put(('[' if i == 0 else ',[') + encoded[k] + ',')
                _emit(v[k], put, active, quote); put(']')
            put(']]'); return
        if t in (set, frozenset):
            items = sorted(_json(tagged_value(x)).decode('ascii') for x in v)
            put('["' + t.__name__ + '",[' + ','.join(items) + ']]'); return
        put('["' + t.__name__ + '",[')
        for i, x in enumerate(v):
            if i: put(',')
            _emit(x, put, active, quote)
        put(']]')
    finally:
        active.remove(id(v))


def _plain_key(k):
    return k + '"'


class _Chunks:
    __slots__ = ('write', 'limit', 'out', 'size')

    def __init__(self, write, limit):
        self.write = write; self.limit = limit; self.out = []; self.size = 0

    def put(self, text):
        self.out.append(text); self.size += len(text)
        if self.size >= self.limit: self.flush()

    def flush(self):
        if self.out:
            self.write(''.join(self.out).encode('ascii')); self.out.clear(); self.size = 0


def write_encoded_value(value: object, write, chunk_chars: int = 1 << 16) -> None:
    """Stream encode_value(value) to write(bytes) without building the tagged
    tree, the JSON text or the byte payload in memory; the bytes are identical.

    Lists, tuples and LazyLists stream in order; dict pairs and set items are
    ordered exactly as tagged_value orders them (dict keys and set items are
    encoded eagerly to sort, dict values lazily)."""
    from json.encoder import encode_basestring_ascii as quote
    chunks = _Chunks(write, chunk_chars)
    _emit(value, chunks.put, set(), quote)
    chunks.flush()


def _tag(v, active):
    t = type(v)
    if v is None: return ['none']
    if t is bool: return ['bool', v]
    if t is int: return ['int', str(v)]
    if t is str: return ['str', v]
    if t is bytes: return ['bytes', base64.b64encode(v).decode('ascii')]
    if t is float and math.isfinite(v): return ['float', v.hex()]
    if t is Decimal and v.is_finite(): return ['decimal', str(v)]
    if t is Fraction: return ['fraction', [str(v.numerator), str(v.denominator)]]
    if t is LazyList: return ['list', [_tag(x, active) for x in v]]
    if t not in (dict, list, tuple, set, frozenset):
        raise CheckpointError('unsupported checkpoint value: ' + t.__name__)
    if id(v) in active: raise CheckpointError('cyclic checkpoint value')
    active.add(id(v))
    try:
        if t is dict:
            pairs = [[_tag(k, active), _tag(x, active)] for k, x in v.items()]
            if all(type(k) is str and _PLAIN(k) for k in v):
                # JSON leaves these keys unescaped, so ordering by
                # _json(['str', k]) equals ordering by k + '"'.
                return ['dict', sorted(pairs, key=_plain_pair_key)]
            return ['dict', sorted(pairs, key=_json_pair_key)]
        items = [_tag(x, active) for x in v]
        if t in (set, frozenset): items.sort(key=_json)
        return [t.__name__, items]
    finally:
        active.remove(id(v))


def _plain_pair_key(pair):
    return pair[0][1] + '"'


def _json_pair_key(pair):
    return _json(pair[0])


def tagged_value(value: object):
    """The tagged tree encode_value serializes (encode_value(v) == _json(tagged_value(v)))."""
    return _tag(value, set())


def _untag(v):
    if type(v) is not list or not v or type(v[0]) is not str:
        raise ValueError('invalid tagged value')
    tag = v[0]
    if tag == 'none' and len(v) == 1: return None
    if len(v) != 2: raise ValueError('invalid tagged value arity')
    x = v[1]
    if tag == 'bool' and type(x) is bool: return x
    if tag == 'str' and type(x) is str: return x
    if tag == 'int' and type(x) is str and re.fullmatch(r'0|-?[1-9][0-9]*', x): return int(x)
    if tag == 'bytes' and type(x) is str: return base64.b64decode(x, validate=True)
    if tag == 'float' and type(x) is str:
        result = float.fromhex(x)
        if math.isfinite(result) and result.hex() == x: return result
    if tag == 'decimal' and type(x) is str:
        result = Decimal(x)
        if result.is_finite() and str(result) == x: return result
    if tag == 'fraction' and type(x) is list and len(x) == 2:
        numerator = _untag(['int', x[0]]); denominator = _untag(['int', x[1]])
        if denominator <= 0: raise ValueError('invalid fraction denominator')
        return Fraction(numerator, denominator)
    if tag in ('dict', 'list', 'tuple', 'set', 'frozenset') and type(x) is list:
        if tag == 'dict':
            result = {}
            for pair in x:
                if type(pair) is not list or len(pair) != 2: raise ValueError('invalid mapping pair')
                k, item = _untag(pair[0]), _untag(pair[1])
                if k in result: raise ValueError('duplicate checkpoint key')
                result[k] = item
            return result
        items = [_untag(item) for item in x]
        if tag == 'list': return items
        if tag == 'tuple': return tuple(items)
        result = set(items)
        if len(result) != len(items): raise ValueError('duplicate set entry')
        return result if tag == 'set' else frozenset(result)
    raise ValueError('invalid or unsupported checkpoint tag')

def _untag_consume(v):
    """_untag that releases the tagged tree while building values (lower peak memory)
    and interns string mapping keys, as a running process shares its field names."""
    if type(v) is not list or not v or type(v[0]) is not str:
        raise ValueError('invalid tagged value')
    tag = v[0]
    if tag == 'none' and len(v) == 1: return None
    if len(v) != 2: raise ValueError('invalid tagged value arity')
    x = v[1]
    if tag == 'bool' and type(x) is bool: return x
    if tag == 'str' and type(x) is str: return x
    if tag == 'int' and type(x) is str and re.fullmatch(r'0|-?[1-9][0-9]*', x): return int(x)
    if tag == 'bytes' and type(x) is str: return base64.b64decode(x, validate=True)
    if tag == 'float' and type(x) is str:
        result = float.fromhex(x)
        if math.isfinite(result) and result.hex() == x: return result
    if tag == 'decimal' and type(x) is str:
        result = Decimal(x)
        if result.is_finite() and str(result) == x: return result
    if tag == 'fraction' and type(x) is list and len(x) == 2:
        numerator = _untag_consume(['int', x[0]]); denominator = _untag_consume(['int', x[1]])
        if denominator <= 0: raise ValueError('invalid fraction denominator')
        return Fraction(numerator, denominator)
    if tag in ('dict', 'list', 'tuple', 'set', 'frozenset') and type(x) is list:
        if tag == 'dict':
            result = {}
            for index in range(len(x)):
                pair = x[index]; x[index] = None
                if type(pair) is not list or len(pair) != 2: raise ValueError('invalid mapping pair')
                k, item = _untag_consume(pair[0]), _untag_consume(pair[1])
                if type(k) is str: k = sys.intern(k)
                if k in result: raise ValueError('duplicate checkpoint key')
                result[k] = item
            return result
        items = []
        for index in range(len(x)):
            item = x[index]; x[index] = None
            items.append(_untag_consume(item))
        if tag == 'list': return items
        if tag == 'tuple': return tuple(items)
        result = set(items)
        if len(result) != len(items): raise ValueError('duplicate set entry')
        return result if tag == 'set' else frozenset(result)
    raise ValueError('invalid or unsupported checkpoint tag')


def decode_consuming(tree: object) -> object:
    """decode_tagged for a large parsed payload (json.loads output) the caller no
    longer needs: the tagged tree is released as values are built, and mapping
    keys are interned."""
    try:
        return _untag_consume(tree)
    except (ValueError, TypeError, ArithmeticError, RecursionError) as error:
        raise CheckpointError('invalid checkpoint encoding: ' + str(error)) from error


_NODES = '[["str","runtime"],["dict",[[["str","nodes"],["list",['


def decode_state_payload(payload: bytes) -> object:
    """decode_consuming(json.loads(payload)) for a checkpoint state payload.

    The runtime node list (almost all of a large checkpoint) is parsed and
    decoded one node at a time, so the parsed JSON tree of the whole payload
    never exists at once; the rest is parsed normally. Falls back to the plain
    path when the payload does not have the expected layout."""
    text = payload.decode('ascii')
    start = text.find(_NODES)
    if start < 0 or text.find(_NODES, start + 1) >= 0:
        return decode_consuming(json.loads(text))
    decoder = json.JSONDecoder(); index = start + len(_NODES); nodes = []
    try:
        if text[index] != ']':
            while True:
                tree, index = decoder.raw_decode(text, index)
                nodes.append(decode_consuming(tree)); del tree
                if text[index] == ']': break
                if text[index] != ',': raise ValueError('invalid runtime node separator')
                index += 1
        rest = text[:start + len(_NODES)] + text[index:]
    except (ValueError, IndexError) as error:
        raise CheckpointError('invalid checkpoint encoding: ' + str(error)) from error
    del text
    state = decode_consuming(json.loads(rest)); del rest
    runtime = state.get('runtime') if type(state) is dict else None
    if type(runtime) is not dict or runtime.get('nodes') != []:
        raise CheckpointError('invalid checkpoint runtime layout')
    runtime['nodes'] = nodes
    return state


def decode_tagged(tree: object) -> object:
    """decode_value for an already parsed tagged tree (json.loads output)."""
    try:
        return _untag(tree)
    except (ValueError, TypeError, ArithmeticError, RecursionError) as error:
        raise CheckpointError('invalid checkpoint encoding: ' + str(error)) from error


def decode_value(data: bytes) -> object:
    try:
        return _untag(json.loads(data))
    except (ValueError, TypeError, ArithmeticError, RecursionError) as error:
        raise CheckpointError('invalid checkpoint encoding: ' + str(error)) from error
