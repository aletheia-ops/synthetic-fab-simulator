"""Optional geometry storage with conservative whole-entry byte accounting."""
from collections import OrderedDict
from fractions import Fraction
import sys


_LEAVES=(str,bytes,int,float,bool,type(None))
_PYPY_OBJECT_BYTES=64


def _retained_size(value, seen):
    # Iterative walk: each distinct object is counted once, as before.
    # PyPy has no per-object sizes; its default makes each object count a
    # flat estimate (CPython never uses the default for these types).
    getsize=sys.getsizeof;total=0;stack=[value]
    while stack:
        item=stack.pop();identity=id(item)
        if identity in seen:continue
        seen.add(identity);total+=getsize(item,_PYPY_OBJECT_BYTES);kind=type(item)
        if kind is str or kind is int:continue
        if isinstance(item,dict):
            for key,child in item.items():stack.append(key);stack.append(child)
        elif isinstance(item,(tuple,list,set,frozenset)):stack.extend(item)
        elif isinstance(item,Fraction):stack.append(item.numerator);stack.append(item.denominator)
        elif not isinstance(item,_LEAVES):raise ValueError('unsupported derived cache value')
    return total


class DerivedCache:
    def __init__(self,budget_bytes):
        if type(budget_bytes) is not int or budget_bytes<=0:
            raise ValueError('invalid derived cache byte budget')
        self.budget_bytes=budget_bytes;self._entries=OrderedDict();self._bytes=0
        self._hits=0;self._misses=0;self._evictions=0

    def get(self,namespace,key):
        identity=(namespace,key)
        if identity not in self._entries:self._misses+=1;return None
        self._hits+=1;self._entries.move_to_end(identity)
        return self._entries[identity][0]

    def put(self,namespace,key,value):
        identity=(namespace,key)
        # Include key/value containers and conservative per-entry LRU/hash slack.
        size=_retained_size((identity,value),set())+256
        if identity in self._entries:
            _,old=self._entries.pop(identity);self._bytes-=old
        if size>self.budget_bytes:return False
        while self._bytes+size>self.budget_bytes:
            _,(_,old)=self._entries.popitem(last=False);self._bytes-=old;self._evictions+=1
        self._entries[identity]=(value,size);self._bytes+=size
        return True

    def clear(self):
        self._entries.clear();self._bytes=0

    def stats(self):
        return {'retained_bytes':self._bytes,'entries':len(self._entries),
                'hits':self._hits,'misses':self._misses,'evictions':self._evictions}

    def namespace_stats(self,namespace):
        values=[v for (ns,_),(v,_) in self._entries.items() if ns==namespace]
        return {'entries':len(values),'items':sum(len(v) for v in values)}
