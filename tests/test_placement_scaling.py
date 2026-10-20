"""Check the exact placement objective against exhaustive small-layout decisions."""
from fractions import Fraction
from itertools import combinations
import random
import pytest
from fab_sim.generation import _place


class Costs(dict):
    reads = 0
    def __getitem__(self, key):
        self.reads += 1
        return super().__getitem__(key)


def fixture(seed=0, replicas=3):
    rng=random.Random(seed)
    instances=[{'id':f'{g}:{i}', 'tool_group_id':g,'width_um':1,'height_um':1}
               for g in ('A','B','C') for i in range(replicas)]
    slots=[{'id':str(i),'clearance_um':0,'envelope_long_um':2,'envelope_trans_um':2}
           for i in range(len(instances)+2)]
    graph={('A','B'):Fraction(3,7),('B','A'):Fraction(11,13),
           ('B','C'):Fraction(1,3),('C','A'):Fraction(5,17),('A','A'):Fraction(2,5)}
    costs=Costs({(a['id'],b['id']):rng.randrange(1,100) for a in slots for b in slots})
    return instances,slots,graph,costs


def exhaustive(instances,slots,graph,costs,limit):
    def score(placed):
        result=Fraction(0)
        for (a,b),weight in graph.items():
            left=[e['id'] for e in instances if e['tool_group_id']==a and e['id'] in placed]
            right=[e['id'] for e in instances if e['tool_group_id']==b and e['id'] in placed]
            if left and right:
                result+=Fraction(weight)*sum(min(0 if e==f else costs[placed[e],placed[f]] for f in right)
                                   for e in left)/len(left)
        return result
    degree={g:sum(w for edge,w in graph.items() for k in edge if k==g) for g in ('A','B','C')}
    placed={}
    for e in sorted(instances,key=lambda e:(-degree[e['tool_group_id']],e['tool_group_id'],e['id'])):
        candidates=[]
        for slot in slots:
            if (slot['id'] not in placed.values()
                    and e['width_um']+2*slot['clearance_um']<=slot['envelope_long_um']
                    and e['height_um']+2*slot['clearance_um']<=slot['envelope_trans_um']):
                candidates.append((score({**placed,e['id']:slot['id']}),slot['id']))
        placed[e['id']]=min(candidates)[1]
    initial=score(placed);total=initial;swaps=[];termination='local_optimum'
    for iteration in range(limit):
        candidates=[]
        for a,b in combinations(sorted(placed),2):
            eq={e['id']:e for e in instances};slot_by={s['id']:s for s in slots}
            if any(eq[e]['width_um']+2*slot_by[placed[f]]['clearance_um']>slot_by[placed[f]]['envelope_long_um']
                   or eq[e]['height_um']+2*slot_by[placed[f]]['clearance_um']>slot_by[placed[f]]['envelope_trans_um']
                   for e,f in ((a,b),(b,a))):continue
            trial={**placed,a:placed[b],b:placed[a]};value=score(trial)
            if value<total:candidates.append((value,a,b))
        if not candidates:break
        total,a,b=min(candidates);placed[a],placed[b]=placed[b],placed[a];swaps.append([a,b])
        if iteration==limit-1:termination='iteration_limit'
    return placed,initial,total,swaps,termination


@pytest.mark.parametrize('seed',range(6))
def test_replicated_placement_matches_exhaustive_exact_decisions(seed):
    instances,slots,graph,costs=fixture(seed)
    expected,initial,final,swaps,termination=exhaustive(instances,slots,graph,costs,3)
    actual,history=_place(instances,slots,graph,costs,3)
    assert actual==expected
    assert Fraction(history['initial_objective'])==initial
    assert Fraction(history['final_objective'])==final
    assert history['swaps']==swaps
    assert history['termination']==termination


def test_replicated_placement_avoids_repeated_distance_scans():
    instances,slots,graph,costs=fixture(replicas=8)
    exhaustive(instances,slots,graph,costs,1)
    reference_reads=costs.reads;costs.reads=0
    _place(instances,slots,graph,costs,1)
    assert costs.reads < reference_reads/5


def test_placement_preserves_footprint_eligibility():
    instances,slots,graph,costs=fixture(17)
    instances[0]['width_um']=3
    slots[2]['envelope_long_um']=3
    slots[7]['envelope_long_um']=4
    expected,initial,final,swaps,termination=exhaustive(instances,slots,graph,costs,2)
    actual,history=_place(instances,slots,graph,costs,2)
    assert actual==expected
    assert history['swaps']==swaps
    assert Fraction(history['final_objective'])==final


def test_large_destination_group_preserves_exact_placement():
    instances,slots,graph,costs=fixture(23,replicas=1)
    instances.extend({**instances[1],'id':f'B:extra:{i}'} for i in range(64))
    slots=[{**slots[0],'id':str(i)} for i in range(len(instances)+2)]
    rng=random.Random(23)
    costs=Costs({(a['id'],b['id']):rng.randrange(1,100) for a in slots for b in slots})
    graph={('A','B'):1,('B','A'):Fraction(7,19)}
    expected,initial,final,swaps,termination=exhaustive(instances,slots,graph,costs,1)
    actual,history=_place(instances,slots,graph,costs,1)
    assert actual==expected
    assert history['swaps']==swaps
    assert Fraction(history['initial_objective'])==initial
    assert Fraction(history['final_objective'])==final
