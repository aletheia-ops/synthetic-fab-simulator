"""Render a topology (layout/topologies/<sha>.json) as an SVG floor plan.

Rails (interbay rails thicker), tools coloured by process area, stockers and the fab's
entry/exit ports. Each tool carries a hover title (id, tool group, bay). Standard library only.

Usage: python tools/render_topology.py TOPOLOGY.json [--manufacturing layout/manufacturing.json] [-o out.svg]
"""
import argparse, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCALE = 10 / 1e6  # SVG units per micrometre (1 unit = 0.1 m)
# Fixed categorical order (light, dark). Metrology areas share a neutral hatched fill.
AREAS = [('Dry_Etch', '#2a78d6', '#3987e5'), ('Wet_Etch', '#eb6834', '#d95926'), ('Litho', '#1baf7a', '#199e70'),
         ('TF', '#eda100', '#c98500'), ('Dielectric', '#e87ba4', '#d55181'), ('Diffusion', '#008300', '#008300'),
         ('Implant', '#6250d6', '#9085e9'), ('Planar', '#e34948', '#e66767')]
METROLOGY = ('Litho_Met', 'TF_Met', 'Def_Met')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('topology', type=Path)
    ap.add_argument('--manufacturing', type=Path, default=ROOT / 'layout/manufacturing.json')
    ap.add_argument('-o', '--out', type=Path)
    a = ap.parse_args()
    m = json.loads(a.topology.read_text())['model']
    area = {g['id']: g['area'] for g in json.loads(a.manufacturing.read_text())['model']['tool_groups']}
    nodes = {n['id']: (n['x_um'], n['y_um']) for n in m['nodes']}
    bay_type = {b['id']: b['type'] for b in m['bays']}
    xs = [p[0] for p in nodes.values()] + [e['x_um'] for e in m['equipment']]
    ys = [p[1] for p in nodes.values()] + [e['y_um'] for e in m['equipment']]
    pad = 3e6
    x0, x1, y0, y1 = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    W, H = (x1 - x0) * SCALE, (y1 - y0) * SCALE
    legend_h = 90
    X = lambda x: f'{(x - x0) * SCALE:.1f}'
    Y = lambda y: f'{(y1 - y) * SCALE:.1f}'

    slot = {name: i for i, (name, _, _) in enumerate(AREAS)}
    css_light = ''.join(f'.a{i}{{fill:{lt}}}' for i, (_, lt, _) in enumerate(AREAS))
    css_dark = ''.join(f'.a{i}{{fill:{dk}}}' for i, (_, _, dk) in enumerate(AREAS))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H + legend_h:.0f}" '
           f'width="{W:.0f}" height="{H + legend_h:.0f}" font-family="system-ui, sans-serif">',
           '<title>Fab floor plan: SMAT2022 rail network with Dataset 1 tools by process area</title>',
           '<style>'
           ':root{--bg:#fbfaf7;--rail:#9a998f;--inter:#55544c;--ink:#1f1f1c;--muted:#6b6a62;--met:#b4b2a8;--edge:#fbfaf7}'
           f'{css_light}'
           '@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--rail:#5d5c55;--inter:#a3a29a;--ink:#f0efec;'
           f'--muted:#a3a29a;--met:#6b6a62;--edge:#1a1a19}}{css_dark}}}'
           '.bg{fill:var(--bg)}.r{stroke:var(--rail);stroke-width:1.2;fill:none}'
           '.i{stroke:var(--inter);stroke-width:3;fill:none}.t{stroke:var(--edge);stroke-width:1.5}'
           '.m{fill:url(#hatch)}.s{fill:var(--ink)}.p{fill:none;stroke:var(--ink);stroke-width:3}'
           '.txt{fill:var(--ink);font-size:22px}.sub{fill:var(--muted);font-size:18px}'
           '</style>',
           '<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
           '<rect width="8" height="8" fill="var(--met)"/><line x1="0" y1="0" x2="0" y2="8" stroke="var(--bg)" stroke-width="3"/></pattern></defs>',
           f'<rect class="bg" width="{W:.0f}" height="{H + legend_h:.0f}"/>']
    for r in m['rails']:
        (ax, ay), (bx, by) = nodes[r['from_node']], nodes[r['to_node']]
        cls = 'i' if bay_type.get(r['bay_id']) == 'INTERBAY' else 'r'
        out.append(f'<line class="{cls}" x1="{X(ax)}" y1="{Y(ay)}" x2="{X(bx)}" y2="{Y(by)}"/>')
    for e in m['equipment']:
        w, h = e['width_um'] * SCALE, e['height_um'] * SCALE
        x, y = float(X(e['x_um'])) - w / 2, float(Y(e['y_um'])) - h / 2
        if e['kind'] == 'ordinary':
            ar = area.get(e['tool_group_id'], '?')
            cls = f'a{slot[ar]} t' if ar in slot else 'm t'
            label = f"{e['id']} ({ar}, bay {e['bay_id']})"
        elif e['kind'] == 'stocker':
            cls, label = 's', f"{e['id']} (stocker, bay {e['bay_id']})"
        else:
            cls, label = 'p', f"{e['id']} (fab {e['kind']})"
        out.append(f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="2"><title>{label}</title></rect>')
    # Legend
    ly = H + 30
    items = [(f'a{i} t', name.replace('_', ' ')) for i, (name, _, _) in enumerate(AREAS)] + \
            [('m t', 'Metrology'), ('s', 'Stocker'), ('p', 'Fab entry / exit')]
    x = 20
    for cls, name in items:
        out.append(f'<rect class="{cls}" x="{x}" y="{ly - 16}" width="20" height="20" rx="3"/>'
                   f'<text class="txt" x="{x + 28}" y="{ly}">{name}</text>')
        x += 40 + 12 * len(name)
    counts = {k: sum(1 for e in m['equipment'] if e['kind'] == k) for k in ('ordinary', 'stocker')}
    out.append(f'<text class="sub" x="20" y="{ly + 40}">{counts["ordinary"]} tools, {counts["stocker"]} stockers, '
               f'{len(m["rails"])} rail segments ({len(m["bays"])} bays), {len(m.get("vehicles", []))} vehicles; '
               f'{(x1 - x0 - 2 * pad) / 1e6:.0f} × {(y1 - y0 - 2 * pad) / 1e6:.0f} m</text>')
    out.append('</svg>')
    text = '\n'.join(out) + '\n'
    if a.out:
        a.out.write_text(text)
        print(f'wrote {a.out} ({len(text) / 1e6:.1f} MB)')
    else:
        print(text)


if __name__ == '__main__':
    main()
