"""Immutable static relationships built from already validated physical records."""
from types import MappingProxyType


class ResourceIndex:
    def __init__(self,slots,tools,model,*,protected_slot_ids=()):
        model=model.get('model',model)
        tool_by_slot={};group_by_tool={};tools_by_group={}
        for tid,tool in tools.items():
            group=tool['tool_group_id'];group_by_tool[tid]=group
            tools_by_group.setdefault(group,[]).append(tid)
            for sid in tool['rack_slots']:
                slot=slots.get(sid)
                if (slot is None or slot.get('kind')!='rack' or slot.get('equipment_id')!=tid
                        or sid in tool_by_slot):raise ValueError('contradictory rack ownership')
                tool_by_slot[sid]=tid
        inputs={group:[] for group in tools_by_group};outputs={group:[] for group in tools_by_group}
        rail_by_dock={}
        for dock in model['docks']:
            rail_by_dock[dock['id']]=dock['rail_id']
            group=group_by_tool.get(dock.get('equipment_id'));role=dock.get('role','BOTH')
            if group is not None:
                if role in ('IN','BOTH'):inputs[group].append(dock['id'])
                if role in ('OUT','BOTH'):outputs[group].append(dock['id'])
        for sid,slot in slots.items():
            if slot.get('dock_id') is not None:
                if slot['dock_id'] not in rail_by_dock:raise ValueError('unknown internal storage dock')
                rail_by_dock[sid]=rail_by_dock[slot['dock_id']]
        vehicles={}
        for vehicle in model.get('vehicles',[]):vehicles.setdefault(vehicle['type_id'],[]).append(vehicle['id'])
        def frozen(mapping):return MappingProxyType({key:tuple(sorted(value)) for key,value in mapping.items()})
        self._inputs=MappingProxyType({tid:tuple(d['id'] for d in model['docks']
            if d.get('equipment_id')==tid and d.get('role') in ('IN','BOTH')) for tid in tools})
        self._outputs=MappingProxyType({tid:tuple(d['id'] for d in model['docks']
            if d.get('equipment_id')==tid and d.get('role') in ('OUT','BOTH')) for tid in tools})
        self._buffers=tuple(d['id'] for d in model['docks'] if d.get('kind')=='track_buffer')
        self._protected=frozenset(protected_slot_ids)
        self._buffer_split={flag:tuple(sid for sid in self._buffers if (sid in self._protected)==flag)
                            for flag in (False,True)}
        self.tool_by_slot=MappingProxyType(tool_by_slot);self.group_by_tool=MappingProxyType(group_by_tool)
        self.tools_by_group=frozen(tools_by_group)
        # Diagnostics retain source insertion order rather than dispatch order.
        self.tool_order_by_group=MappingProxyType({g:tuple(ids) for g,ids in tools_by_group.items()})
        self.input_docks_by_group=frozen(inputs);self.output_docks_by_group=frozen(outputs)
        self.rail_by_dock=MappingProxyType(rail_by_dock);self.vehicles_by_type=frozen(vehicles)

    def input_ports(self, tool_id):return self._inputs.get(tool_id,())

    def output_ports(self, tool_id):return self._outputs.get(tool_id,())

    def buffer_candidates(self, tool_id, *, protected):
        return self._buffer_split[bool(protected)]

    def dispatch_tools(self,groups=None):
        """Canonical tool order from the already validated static group map."""
        if groups is None:return tuple(sorted(self.group_by_tool))
        return tuple(sorted(tid for group in groups for tid in self.tools_by_group.get(group,())))
