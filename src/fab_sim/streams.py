"""External event streams: factory events and a sensor-style OHT stream.

Domain owners emit rich internal events (hop reservations, routes, planning
records). This projection decides what leaves the simulator: `events.jsonl`
carries factory, LOT and carrier events; `oht.jsonl` carries what an OHT
controller would report per vehicle. It reads current state only to resolve
identities and positions and never changes it.
"""
from fractions import Fraction

POLICY = 'fab-events-v3'

# Planning data on transfer records; the physical facts stay.
TRANSFER_INTERNAL = frozenset({'loaded_path', 'destinations', 'pickup_pose', 'drop_pose',
                               'dock_claims', 'rack_owner'})
HOIST_STARTED = {'pickup_handling': 'oht_load_started', 'drop_handling': 'oht_unload_started'}


class StreamProjection:
    def __init__(self, traffic, transfers, custody):
        self.traffic, self.transfers, self.custody = traffic, transfers, custody
        nodes = {n['id']: n for n in traffic.model.get('nodes', ())}
        # Rails are straight chords between their nodes; offsets map linearly.
        # Topologies without node coordinates report rail offsets only.
        self._rails = {}
        for rid, r in traffic.rails.items():
            a, b = nodes.get(r['from_node'], {}), nodes.get(r['to_node'], {})
            if all(k in n for n in (a, b) for k in ('x_um', 'y_um')):
                self._rails[rid] = (a['x_um'], a['y_um'], b['x_um'], b['y_um'], r['length_um'])

    def pose(self, rail_id, offset_um):
        offset = Fraction(offset_um)
        pose = {'rail_id': rail_id, 'offset_um': int(offset)}
        if rail_id in self._rails:
            x0, y0, x1, y1, length = self._rails[rail_id]
            pose.update(x_um=round(x0 + (x1 - x0) * offset / length), y_um=round(y0 + (y1 - y0) * offset / length))
        return pose

    def _vehicle_pose(self, vehicle_id):
        vehicle = self.traffic.vehicles[vehicle_id]
        return self.pose(vehicle['rail_id'], vehicle['offset_um'])

    def _carrier(self, vehicle_id):
        slot = self.traffic.vehicles[vehicle_id].get('carrier_slot_id')
        return self.custody.slots[slot]['carrier_id'] if self.custody is not None and slot else None

    @staticmethod
    def _task(request_id):
        if request_id.startswith('idle:'):
            return {'task': 'reposition', 'transfer_id': None}
        transfer, _, phase = request_id.rpartition(':')
        return {'task': phase, 'transfer_id': transfer}

    def _request_vehicle(self, request_id):
        request = self.traffic.requests.get(request_id)
        return request['vehicle_id'] if request is not None else None

    def project(self, kind, payload):
        """Return [(stream, event_type, payload)] for one internal event."""
        if kind == 'traffic_hop_started':
            vid = payload['vehicle_id']; start = payload['parts'][0]; end = payload['target_pose']
            return [('oht', 'oht_segment', {
                'vehicle_id': vid, **self._task(payload['request_id']), 'request_id': payload['request_id'],
                'from': self.pose(start['rail_id'], start['start_um']),
                'to': self.pose(end['rail_id'], end['offset_um']),
                'started_at_us': payload['started_at_us'],
                'expected_arrival_us': payload['started_at_us'] + payload['duration_us'],
                'distance_um': payload['distance_um'], 'carrier_id': self._carrier(vid)})]
        if kind in ('processing_perturbed', 'transport_abstraction_replaced', 'dispatch_cap_changed'):
            # Ground truth / simulator bookkeeping, never in the observational streams.
            return [('diagnostics', kind, payload)]
        if kind.startswith('handoff_'):
            demand = self.transfers.demands.get(payload.get('demand_id')) if self.transfers else None
            if demand is not None:
                payload = {**payload, 'carrier_id': demand['carrier_id'], 'lot_id': demand['lot_id']}
            return [('events', kind, payload)]
        if kind == 'traffic_hop_arrived':
            return []  # Each segment already states its end.
        if kind in ('traffic_arrived', 'traffic_cancelled'):
            vid = payload['vehicle_id']
            return [('oht', 'oht_arrived' if kind == 'traffic_arrived' else 'oht_movement_cancelled', {
                'vehicle_id': vid, **self._task(payload['request_id']), 'request_id': payload['request_id'],
                'pose': self.pose(payload['pose']['rail_id'], payload['pose']['offset_um']),
                'carrier_id': self._carrier(vid)})]
        if kind == 'traffic_wait_changed':
            vid = self._request_vehicle(payload['request_id'])
            if vid is None:
                return []
            return [('oht', 'oht_blocked', {
                'vehicle_id': vid, **self._task(payload['request_id']), 'request_id': payload['request_id'],
                'pose': self._vehicle_pose(vid), 'blocked_by': payload['blockers']})]
        if kind == 'traffic_idle_wait_changed':
            vid = payload['vehicle_id']
            return [('oht', 'oht_blocked', {
                'vehicle_id': vid, 'task': 'clear', 'transfer_id': None, 'request_id': None,
                'pose': self._vehicle_pose(vid), 'blocked_by': payload['blockers']})]
        if kind in ('amhs_fault_started', 'amhs_fault_recovered'):
            return [('oht', kind, {**payload, 'segments': self._fault_segments(payload)})]
        if kind == 'transfer_movement_requested':
            demand = self.transfers.demands.get(payload['demand_id']) if self.transfers else None
            port = None
            if demand is not None:
                port = demand.get('origin_id') if payload['phase'] == 'pickup' else demand.get('destination_id')
            return [('oht', 'oht_dispatched', {
                'vehicle_id': payload['vehicle_id'], 'task': payload['phase'], 'transfer_id': payload['demand_id'],
                'request_id': payload['id'], 'port_id': port, 'queued_at_us': payload['queued_at_us']})]
        if kind == 'transfer_handling_started':
            hoist = HOIST_STARTED.get(payload.get('status'))
            if hoist is None:
                return [('events', kind, self._transfer(payload))]
            pickup = hoist == 'oht_load_started'
            return [('oht', hoist, self._hoist(payload, payload['origin_id'] if pickup else payload['destination_id']))]
        if kind.startswith('transfer_'):
            records = [('events', kind, self._transfer(payload))]
            if kind == 'transfer_picked_up':
                records.append(('oht', 'oht_load_completed', self._hoist(payload, payload['origin_id'])))
            elif kind == 'transfer_delivered':
                records.append(('oht', 'oht_unload_completed', self._hoist(payload, payload['destination_id'])))
            return records
        return [('events', kind, payload)]

    @staticmethod
    def _transfer(payload):
        return {k: v for k, v in payload.items() if k not in TRANSFER_INTERNAL}

    @staticmethod
    def _hoist(payload, port_id):
        return {'vehicle_id': payload['vehicle_id'], 'transfer_id': payload['id'],
                'carrier_id': payload['carrier_id'], 'lot_id': payload.get('lot_id'), 'port_id': port_id}

    def _fault_segments(self, payload):
        # Moving vehicles affected by this fault, with the current expected
        # arrival of each running segment (None while it remains paused).
        target, kind = payload['target_id'], payload['target_type']
        affected = []
        for vid, hop in sorted(self.traffic.hops.items()):
            if kind == 'oht' and vid == target or kind == 'rail' and any(p['rail_id'] == target for p in hop['parts']):
                timer = hop['timer']
                affected.append({'vehicle_id': vid, 'request_id': hop['request_id'],
                                 'expected_arrival_us': None if timer.causes else timer._end})
        return affected
