"""CPython MT19937 streams; observation never consumes domain randomness."""
from decimal import Decimal
import hashlib
import json
import random

POLICY = 'sha256-mt19937-v1'
CHANNELS = {'release','processing','maintenance','injected_faults','topology','perturbation'}


def rng_for(master_seed: int, channel: str, scope_id: str) -> random.Random:
    if type(master_seed) is not int or master_seed<0: raise ValueError('master seed must be a nonnegative integer')
    if channel not in CHANNELS: raise ValueError('unsupported domain RNG channel')
    if not isinstance(scope_id,str) or not scope_id: raise ValueError('scope ID must be a nonempty model-local string')
    encoded=json.dumps([POLICY,master_seed,channel,scope_id],ensure_ascii=True,separators=(',',':')).encode('utf-8')
    seed=int.from_bytes(hashlib.sha256(encoded).digest(),'big')
    return random.Random(seed)


class Streams:
    def __init__(self,master_seed,replication_salt=None):
        # Validate without drawing; retain the master only as a stream factory.
        rng_for(master_seed,'topology','validation')
        if replication_salt is not None and (type(replication_salt) is not int or replication_salt<1):
            raise ValueError('replication salt must be a positive integer or None')
        self.master_seed=master_seed; self._streams={};self._lot_history_owner=None
        # Declared replication salt: seeds streams created from now on with a
        # salted scope, so replications differ only in streams that did not exist
        # yet (a restored stream continues from its saved state either way).
        self.replication_salt=replication_salt

    def get(self,channel,scope_id):
        key=(channel,scope_id)
        if key not in self._streams:
            salt=self.replication_salt
            self._streams[key]=rng_for(self.master_seed,channel,scope_id if salt is None else f'{scope_id}@salt:{salt}')
        return self._streams[key]

    def discard(self,channel,scope_id):
        self._streams.pop((channel,scope_id),None)


    def export_state(self) -> dict[str, object]:
        return {'master_seed':self.master_seed,'states':{key:rng.getstate() for key,rng in self._streams.items()}}

    def restore_state(self, state: dict[str, object]) -> None:
        from .checkpoint_format import CheckpointError
        try:
            if set(state)!={'master_seed','states'} or state['master_seed']!=self.master_seed:
                raise ValueError('random stream master mismatch')
            restored={}
            for key,value in state['states'].items():
                if type(key) is not tuple or len(key)!=2:raise ValueError('invalid random stream identity')
                rng=rng_for(self.master_seed,*key);rng.setstate(value);restored[key]=rng
            self._streams=restored
        except (ValueError,TypeError,KeyError) as error:
            raise CheckpointError('invalid random stream state') from error


def sample_seconds(definition, rng):
    mean=Decimal(definition['mean_seconds']); offset=Decimal(definition.get('offset_seconds','0'))
    if not mean.is_finite() or not offset.is_finite() or not 0<=offset<=mean:
        raise ValueError('invalid duration distribution bounds')
    kind=definition['distribution']
    if kind=='constant':
        if offset: raise ValueError('constant duration has no offset')
        return mean
    if kind=='uniform':
        value=rng.uniform(float(mean-offset),float(mean+offset))
    elif kind=='exponential':
        if offset or mean<=0: raise ValueError('invalid exponential duration')
        value=rng.expovariate(1/float(mean))
    else: raise ValueError('unsupported duration distribution')
    result=Decimal(str(value))
    if not result.is_finite() or result<0: raise ValueError('nonfinite sampled duration')
    return result
