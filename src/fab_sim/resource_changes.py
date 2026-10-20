"""Derived invalidation identities; these records never own physical resources."""
from dataclasses import dataclass,fields


@dataclass(frozen=True,slots=True)
class ResourceChange:
    slot_ids: frozenset[str]=frozenset()
    carrier_ids: frozenset[str]=frozenset()
    tool_ids: frozenset[str]=frozenset()
    group_ids: frozenset[str]=frozenset()
    vehicle_ids: frozenset[str]=frozenset()
    rail_ids: frozenset[str]=frozenset()

    def __post_init__(self):
        for field in fields(self):
            value=getattr(self,field.name)
            if not isinstance(value,frozenset) or any(not isinstance(v,str) or not v for v in value):
                raise ValueError('resource identities require immutable nonempty strings')


class DirtyResources:
    def __init__(self):self._full=True;self._ids=set()

    def mark(self,ids=None):
        if ids is None:self._full=True;self._ids.clear();return
        if isinstance(ids,str):raise ValueError('resource worklist requires an iterable of IDs')
        values=frozenset(ids)
        if any(not isinstance(v,str) or not v for v in values):raise ValueError('invalid resource worklist ID')
        if not self._full:self._ids.update(values)

    def take(self):
        current=None if self._full else frozenset(self._ids)
        self._full=False;self._ids.clear()
        return current
