"""Static tool-group load for an SMT2020-format workbook (see docs/capacity.md).
Usage: python tools/static_capacity.py WORKBOOK.xlsx [--worst-setups]
--worst-setups charges every visit its route setup time (an upper bound; consecutive
LOTs of the same setup skip it). Gas setups with minimum runs are not modelled."""
import sys, json, collections
from openpyxl import load_workbook
UNIT = {'sec':1,'s':1,'min':60,'hr':3600,'h':3600,'hour':3600,'day':86400,'d':86400,'week':604800}
def secs(v,u): return float(v)*UNIT[str(u).strip().lower()]
def rows(b,name):
    it=b[name].iter_rows(values_only=True); head=[str(h).strip() if h is not None else '' for h in next(it)]
    for r in it:
        if r is None or all(x is None for x in r): break
        yield dict(zip(head,r)), r
path=sys.argv[1]; WORST_SETUPS='--worst-setups' in sys.argv; b=load_workbook(path,read_only=True)
groups={}
for d,r in rows(b,'Toolgroups'):
    groups[d['TOOLGROUP']]={'area':d['AREA'],'n':int(d['NUMBER OF TOOLS']),'cascading':bool(d.get('CASCADINGTOOL')),
        'batching':bool(d.get('BACTHINGTOOL')),'load':secs(d['LOADINGTIME'] or 0,d['LT UNITS'] or 'min')+secs(d['UNLOADINGTIME'] or 0,d['ULT UNITS'] or 'min')}
lots_per_day=collections.Counter(); wafers=25
for d,r in rows(b,'Lotrelease'):
    if d['RELEASE INTERVAL'] in (None,''):continue  # engineering lots: separate release sheet
    lots_per_day[d['ROUTE NAME']]+=86400/secs(d['RELEASE INTERVAL'],d['R UNITS'])*(d['LOTS PER RELEASE'] or 1)
    wafers=d['WAFERS PER LOT'] or 25
demand=collections.Counter(); wafer_rate=collections.Counter(); steps_per_lot={}
for route,L in lots_per_day.items():
    st=[d for d,r in rows(b,route)]
    visits=[(float(d['PROCESSING PROBABILITY in % (Sampling)'])/100 if d.get('PROCESSING PROBABILITY in % (Sampling)') not in (None,'') else 1.0) for d in st]
    idx={str(d['STEP']):i for i,d in enumerate(st)}
    extra=[0.0]*len(st)
    for i,d in enumerate(st):
        p=d.get('REWORK PROBABILITY in %')
        if p not in (None,'') and d.get('STEP FOR REWORK') not in (None,''):
            j=idx[str(d['STEP FOR REWORK'])]
            for k in range(j,i+1): extra[k]+=float(p)/100*visits[i]
    steps_per_lot[route]=sum(v+e for v,e in zip(visits,extra))
    for i,d in enumerate(st):
        g=groups[d['TOOLGROUP']]; unit=str(d['PROCESSING UNIT']).lower(); mean=secs(d['MEAN'],d['PT UNITS'])
        c=d.get('CASCADING INTERVAL');c=secs(c,d['C Units']) if c not in (None,'') else None
        # Cascading tool = two slots in series: slot 1 for min(p, interval), slot 2 for the rest;
        # a unit occupies the tool for max(interval, p - interval) in steady state.
        per=(max(c,mean-c) if c is not None else mean)
        if unit=='lot': t=per+g['load']
        elif unit=='wafer':t=((wafers-1)*per+mean if c is not None else wafers*mean)+g['load']
        else:  # batch: share of one batch run per lot, at the maximum batch size
            t=(mean+g['load'])*wafers/float(d['BATCH MAXIMUM'])
        st=d.get('SETUP TIME')
        if WORST_SETUPS and d.get('SETUP') and st not in (None,'') and str(d.get('WHEN','')).lower()!='always':t+=secs(st,d['ST UNITS'])
        n=(visits[i]+extra[i])*L
        demand[d['TOOLGROUP']]+=n*t; wafer_rate[d['TOOLGROUP']]+=n*wafers
down=collections.defaultdict(float)
for d,r in rows(b,'Breakdown'):
    mttf=secs(d['MTTF'],d['MTTF UNITS']);mttr=secs(d['MTTR'],d['MTTR UNITS'])
    for tg,g in groups.items():
        if g['area']==d['TYPE NAME']:down[tg]+=mttr/(mttf+mttr)
for d,r in rows(b,'PM'):
    tg=d['TYPE NAME'];dur=secs(d['MEAN'],d['TTR UNITS'])
    if tg not in groups:continue
    if 'counter' in str(d['PM TYPE']):
        # interval in wafers per tool; time between PMs at this group's wafer rate per tool
        rate=wafer_rate[tg]/groups[tg]['n']/86400
        if rate>0:down[tg]+=dur/(float(d['MTBeforePM'])/rate+dur)
    else:down[tg]+=dur/(secs(d['MTBeforePM'],d['MTBPM UNITS'])+dur)
out=[]
for tg,g in groups.items():
    if not demand[tg] or tg.startswith('Delay'):continue
    cap=g['n']*86400*max(0.0,1-down[tg]); out.append((demand[tg]/cap if cap else float('inf'),tg,g['n'],round(down[tg],3)))
out.sort(reverse=True)
total_lots=sum(lots_per_day.values())
print(json.dumps({'file':path.split('/')[-1],'lots_per_day':round(total_lots,2),'routes':{k:round(v,2) for k,v in lots_per_day.items()},
    'steps_per_lot':{k:round(v,1) for k,v in steps_per_lot.items()},
    'operations_per_day':round(sum(lots_per_day[r]*steps_per_lot[r] for r in lots_per_day),0),
    'tools':sum(g['n'] for g in groups.values()),'groups_used':len(out)}))
for u,tg,n,dn in out[:15]:print(f'  {tg:22s} tools {n:3d} down {dn:5.3f} load {u:6.1%}')
print('  groups >85%:',sum(1 for x in out if x[0]>0.85),' >100%:',sum(1 for x in out if x[0]>1.0))
