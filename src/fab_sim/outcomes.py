"""Pure stop assessment from settled runtime counts."""
from copy import deepcopy


def validate_exit_targets(targets,products=None):
    if (not isinstance(targets,dict) or not targets or any(
            not isinstance(product,str) or not product or type(count) is not int or count<=0
            for product,count in targets.items())):
        raise ValueError('exit_targets must map product IDs to positive integer counts')
    if products is not None and set(targets)-products:raise ValueError('unknown exit target products')
    return dict(targets)


def classify_stop(state: dict) -> dict:
    violations=sorted(set(state.get('violation_ids',[])));unresolved=sorted(set(state.get('unresolved_obligations',[])))
    constraint='violated' if violations else 'unassessed' if unresolved or state.get('checks_assessed') is not True else 'satisfied'
    result={'outcome':None,'reason':None,'stop_domain':False,'domain_complete':False,
            'constraint_status':constraint,'violation_ids':violations,'violation_count':len(violations),
            'unresolved_obligations':unresolved,'unresolved_count':len(unresolved)}
    achieved=False
    if 'exit_targets' in state:
        targets=validate_exit_targets(state['exit_targets']);exits=state.get('product_exit_counts')
        if (not isinstance(exits,dict) or set(targets)-set(exits) or any(
                not isinstance(p,str) or not p or type(n) is not int or n<0 for p,n in exits.items())):
            raise ValueError('invalid product exit counts')
        achieved=all(exits[p]>=n for p,n in targets.items());reached=state.get('exit_targets_reached_at_us')
        if reached is not None and (type(reached) is not int or not 0<=reached<=state['now_us'] or not achieved):
            raise ValueError('invalid exit target observation tick')
        result.update(exit_targets=targets,product_exit_counts=dict(exits),
            exit_targets_reached_at_us=reached,objective_achieved=achieved)
    if state.get('failure') is not None or state.get('output_status')=='failed':
        return {**result,'outcome':'Failed','reason':'required_output_failure' if state.get('output_status')=='failed' else 'runtime_failure',
                'stop_domain':True,'failure':deepcopy(state.get('failure'))}
    if state.get('rejection') is not None:
        return {**result,'outcome':'Rejected','reason':'input_rejected','stop_domain':True,'rejection':deepcopy(state['rejection'])}
    now,horizon=state['now_us'],state['horizon_us']
    counts=('cohort_size','exited_count','pending_release_count','pending_admission_count','wip_count','active_execution_count','loaded_transfer_count')
    mode=state.get('release_mode','cohort');ongoing=mode=='ongoing'
    if mode not in ('cohort','ongoing'):raise ValueError('invalid release mode assessment')
    physical_counts=tuple(k for k in counts if k not in ('cohort_size','pending_release_count')) if ongoing else counts
    if ongoing:
        if state['cohort_size'] is not None or state['pending_release_count'] is not None or state.get('source_open') is not True:
            raise ValueError('ongoing assessment requires open future supply')
    elif state.get('source_open',False) is not False:raise ValueError('cohort source cannot remain open')
    if (type(now) is not int or type(horizon) is not int or not 0<=now<=horizon or horizon<=0 or
            any(type(state[k]) is not int or state[k]<0 for k in physical_counts) or
            not ongoing and (state['cohort_size']<=0 or state['exited_count']>state['cohort_size']) or
            state['output_status'] not in ('pending','complete') or
            type(state['settled']) is not bool):raise ValueError('invalid stop assessment')
    if not state['settled']:
        if state.get('operator_stop'):return {**result,'outcome':'Incomplete','reason':'operator_stop','stop_domain':True}
        return {**result,'reason':'awaiting_timestamp_settlement'}
    transport_pending=any(state.get(k,0) for k in ('pending_transfer_count','committed_transfer_count'))
    complete=not ongoing and state['exited_count']==state['cohort_size'] and all(state[k]==0 for k in counts[2:]) and not transport_pending
    if state.get('operator_stop'):
        return {**result,'outcome':'Incomplete','reason':'operator_stop','stop_domain':True,'domain_complete':complete}
    if complete:
        return {**result,'domain_complete':True,'stop_domain':True,
                'outcome':'Completed' if state['output_status']=='complete' else None,
                'reason':'cohort_exited' if state['output_status']=='complete' else 'required_output_pending'}
    if achieved:
        return {**result,'stop_domain':True,
            'outcome':'Incomplete' if state['output_status']=='complete' else None,
            'reason':'exit_target_reached' if state['output_status']=='complete' else 'required_output_pending'}
    reason='operator_stop' if state.get('operator_stop') else 'horizon' if now==horizon else None
    return {**result,'outcome':'Incomplete' if reason else None,'reason':reason,'stop_domain':reason is not None}
