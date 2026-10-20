"""Prepare source-grounded inputs and run the physical fab simulator locally."""
import argparse
from pathlib import Path
import sys
from zipfile import BadZipFile
from openpyxl.utils.exceptions import InvalidFileException
from .inputs import read_json,canonical_text
from .json_stream import json_chunks
from .sources.smt import prepare_manufacturing,validate_manufacturing
from .sources.smat import prepare_topology
from .generation import load_prepared
from .runner import run
from .output import OutputFailure


def _write(path,artifact):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    # Preparation never replaces an earlier artifact implicitly.
    with path.open('xb') as stream:
        for chunk in json_chunks(artifact):stream.write(chunk)
        stream.write(b'\n')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    manufacturing=sub.add_parser('prepare-manufacturing')
    manufacturing.add_argument('--source',required=True);manufacturing.add_argument('--output',required=True)
    physical=sub.add_parser('prepare-topology')
    for name in ('source','manufacturing','profile'):physical.add_argument('--'+name,required=True)
    physical.add_argument('--output');physical.add_argument('--topology-directory')
    generated=sub.add_parser('generate-topology')
    for name in ('manufacturing','reference'):generated.add_argument('--'+name,required=True)
    generated.add_argument('--output');generated.add_argument('--topology-directory')
    generated.add_argument('--settings')
    execution=sub.add_parser('run');execution.add_argument('--config',required=True)
    execution.add_argument('--progress',action='store_true')
    execution.add_argument('--progress-interval',type=float,default=15)
    execution.add_argument('--checkpoint-interval-wall-seconds',type=int)
    execution.add_argument('--resume-from')
    validation=sub.add_parser('preflight');validation.add_argument('--config',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='preflight':
            from .preflight import preflight
            result=preflight(Path(args.config));print(canonical_text(result))
            return 2 if result['status']=='rejected' else 0
        if args.command=='run':
            result=run(Path(args.config),progress=(lambda record: print(canonical_text(record),file=sys.stderr,flush=True))
                if args.progress else None,progress_interval=args.progress_interval,
                checkpoint_interval_wall_seconds=args.checkpoint_interval_wall_seconds,resume_from=args.resume_from)
            summary={key:result.get(key) for key in ('outcome','reason','now_us','exited_count','constraint_status','last_committed_sequence','domain_digest')}
            summary['manifest_path']=str(Path(result['resolved_config']['output']['directory'])/'manifest.json')
            print(canonical_text(summary))
            return 1 if result['outcome']=='Failed' else 0
        if args.command=='prepare-manufacturing':artifact=prepare_manufacturing(Path(args.source))
        else:
            from .topology_store import default_directory, generated as stored_generation, persist
            directory=args.topology_directory or default_directory()
            m=validate_manufacturing(read_json(Path(args.manufacturing)))
            if args.command=='prepare-topology':
                artifact,record=persist(prepare_topology(Path(args.source),m,read_json(Path(args.profile))),m,directory)
            else:artifact,record=stored_generation(m,load_prepared(Path(args.reference),m),read_json(Path(args.settings)) if args.settings else {},directory)
        if args.output and (args.command=='prepare-manufacturing' or Path(args.output).resolve()!=Path(record['path'])):
            _write(args.output,artifact)
        result={'outcome':'Prepared','content_sha256':artifact['validation']['content_sha256'],
                'schema_version':artifact['schema_version']}
        if args.command=='prepare-manufacturing':result['output']=str(Path(args.output).resolve())
        else:result.update(output=record['path'],topology_artifact=record)
        print(canonical_text(result))
        return 0
    except OutputFailure as error:
        print(canonical_text({'outcome':'Failed','reason':'required_output_failure','error':str(error)}));return 1
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,BadZipFile,InvalidFileException) as error:
        print(canonical_text({'outcome':'Rejected','reason':'input_rejected','error':str(error)}))
        print('fab-sim: '+str(error),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
