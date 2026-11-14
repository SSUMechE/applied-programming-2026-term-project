"""Supplied compact-input adapter. Students edit student/*.json, not this file.

Adds only supplied parked-arm/default support entries for the unchanged native
scene. Active placements, handoffs, paths, timings and physics are not solved or
modified here. Both CPU planning and physical review use the same expansion.
"""
from __future__ import annotations
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
COMPACT_SCHEMA = 'student-tier-design-v1'
COUNTS = {'easy': 1, 'medium': 2, 'hard': 3}

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('The design or supplied defaults exceed 1 MB')
    def reject(value):
        raise ValueError('Nonfinite JSON value: ' + value)
    return json.loads(path.read_bytes(), parse_constant=reject)

def require(ok, message):
    if not ok:
        raise ValueError(message)

def unfinished(value):
    if value is None: return True
    if type(value) is dict: return any(unfinished(v) for v in value.values())
    if type(value) is list: return any(unfinished(v) for v in value)
    return False

def needs_expansion(record, difficulty):
    if record.get('schema_version') == COMPACT_SCHEMA: return True
    if record.get('schema_version') != 'three-arm-design-v1': return True
    count=COUNTS[difficulty]
    arms,relays=record.get('arms'),record.get('relays')
    require(type(arms) is list and len(arms)==3 and type(relays) is list and len(relays)==2,
            'Keep the complete legacy input structure, or use the current compact template')
    return any(unfinished(a) for a in arms[count:]) or count==1 and unfinished(relays[1])

def expand_design(record, *, difficulty, world, defaults):
    """Return the validated internal record without editing a student input."""
    from transport_challenge.three_arm_design import ThreeArmDesign
    require(type(record) is dict, 'The student design must be a JSON object')
    require(type(defaults) is dict and defaults.get('schema_version') == 'provided-tier-design-defaults-v1',
            'Supplied compact-design defaults are invalid')
    require(difficulty in COUNTS, 'Choose difficulty easy, medium or hard')
    if record.get('schema_version') == 'three-arm-design-v1':
        count=COUNTS[difficulty]
        if not needs_expansion(record,difficulty):
            return ThreeArmDesign.from_record(record, world).to_record()
        provided=defaults['difficulties'][difficulty]
        require(provided['case_id']==world['case_record']['case_id'], 'Supplied default map identity differs')
        expanded=copy.deepcopy(record)
        # Old updates preserve student files. Ignore unfinished unused inputs
        # without overwriting active choices or an already completed legacy layout.
        for arm in provided['parked_arms']:
            index=int(arm['id'].split('_')[1])
            if unfinished(expanded['arms'][index]): expanded['arms'][index]=copy.deepcopy(arm)
        terminal=provided['fixed_finish_support_id']
        target=world['support_targets_by_id'][terminal]['body_world']['position_m']
        require(expanded['relays'][count-1]=={'support_id':terminal,'x_m':target[0],'y_m':target[1]},
                'Keep the supplied legacy fixed finish unchanged. Only unused entries are filled automatically.')
        if count==1 and unfinished(expanded['relays'][1]):
            unused=provided['unused_support_id'];target=world['support_targets_by_id'][unused]['body_world']['position_m']
            expanded['relays'][1]={'support_id':unused,'x_m':target[0],'y_m':target[1]}
        return ThreeArmDesign.from_record(expanded,world).to_record()
    require(set(record) == {'schema_version', 'difficulty', 'arms', 'handoffs', 'joint_speed_scale'}
            and record.get('schema_version') == COMPACT_SCHEMA,
            'Use the supplied student-tier-design-v1 file with difficulty, arms, handoffs and joint_speed_scale')
    require(record['difficulty'] == difficulty, 'The design difficulty must match the selected task')
    count = COUNTS[difficulty]
    require(type(record['arms']) is list and len(record['arms']) == count,
            f'This difficulty requires exactly {count} student arm installation(s)')
    require(type(record['handoffs']) is list and len(record['handoffs']) == count-1,
            f'This difficulty requires exactly {count-1} intermediate handoff(s). Do not enter the fixed finish.')
    require(all(type(a) is dict for a in record['arms']) and all(type(h) is dict for h in record['handoffs']),
            'Each arm and handoff must be a JSON object with the supplied fields')
    require([a.get('id') for a in record['arms']] == [f'arm_{i}' for i in range(count)],
            'Keep the supplied active arm IDs and their order')
    provided = defaults['difficulties'][difficulty]
    require(provided['case_id'] == world['case_record']['case_id'], 'Supplied default map identity differs')
    require(len(provided['parked_arms']) == 3-count, 'Supplied parking count differs')
    arms = copy.deepcopy(record['arms']) + copy.deepcopy(provided['parked_arms'])
    relays = copy.deepcopy(record['handoffs'])
    terminal = provided['fixed_finish_support_id']
    require(all(h.get('support_id') != terminal for h in relays), 'A handoff cannot replace the supplied fixed finish')
    if count < 3:
        target = world['support_targets_by_id'][terminal]['body_world']['position_m']
        relays.append({'support_id': terminal, 'x_m': target[0], 'y_m': target[1]})
    if count == 1:
        unused = provided['unused_support_id']
        target = world['support_targets_by_id'][unused]['body_world']['position_m']
        relays.append({'support_id': unused, 'x_m': target[0], 'y_m': target[1]})
    expanded = {'schema_version': 'three-arm-design-v1', 'arms': arms, 'relays': relays,
                'joint_speed_scale': record['joint_speed_scale']}
    return ThreeArmDesign.from_record(expanded, world).to_record()

def option(argv, key, default=None):
    values=[]
    for i, arg in enumerate(argv):
        if arg == '--'+key:
            require(i+1 < len(argv) and not argv[i+1].startswith('--'), 'Missing --'+key+' value')
            values.append(argv[i+1])
        elif arg.startswith('--'+key+'='):
            values.append(arg.split('=',1)[1])
    require(len(values) <= 1, 'Specify --'+key+' only once')
    return values[0] if values else default

def replace_option(argv, key, value):
    result=[]; i=0
    while i<len(argv):
        if argv[i]=='--'+key: i+=2; continue
        if argv[i].startswith('--'+key+'='): i+=1; continue
        result.append(argv[i]); i+=1
    return [*result, '--'+key, str(value)]

def check_adapter_sources(root):
    manifest=json.loads((root/'course_release.json').read_bytes())
    for name in ('project_run.py','student_design_input.py','supplied_design_defaults.json'):
        require(manifest['managed_files'].get(name)==sha(root/name),
                'Supplied compact-input source changed: '+name+'. Use the official project update.')

def prepare_arguments(kind, argv, *, root=ROOT, check_sources=True):
    """Keep diagnostics' original arguments and give the worker an exact record."""
    root=Path(root).resolve()
    if any(arg in ('--help','-h') for arg in argv): return list(argv), None
    if kind=='plan' and option(argv,'scope','tier-mission')=='one-carry':
        # Preserve the existing explicit legacy practice contract. Its native
        # parser owns task/design requirements and deliberately has no tier.
        source_option=option(argv,'design')
        if source_option is not None:
            legacy=read(Path(source_option).resolve(strict=True))
            require(type(legacy) is not dict or legacy.get('schema_version')!=COMPACT_SCHEMA,
                    'Compact designs use --scope tier-mission. Legacy one-carry practice requires its explicit three-arm design.')
        return list(argv), None
    difficulty=option(argv,'difficulty')
    require(difficulty in COUNTS, 'Choose --difficulty easy, medium or hard')
    source=Path(option(argv,'design',str(root/'student'/f'{difficulty}_design.json'))).resolve(strict=True)
    record=read(source)
    require(type(record) is dict, 'The student design must be a JSON object')
    # Previously completed work remains valid. No automatic conversion or
    # overwrite is performed on an existing three-arm design.
    if not needs_expansion(record,difficulty): return list(argv), None
    if check_sources: check_adapter_sources(root)
    require(kind=='review' or option(argv,'scope','tier-mission')=='tier-mission',
            'Compact student designs use --scope tier-mission')
    default_case={'easy':'easy_open_v1','medium':'medium_staggered_v1','hard':'hard_industrial_v1'}[difficulty]
    task=Path(option(argv,'task',str(root/'cases/difficulty_20261106'/default_case))).resolve(strict=True)
    target=read(task/'difficulty_target.json')
    require(target['difficulty']==difficulty, 'Selected task and difficulty differ')
    world=read(task/'world.json'); defaults=read(root/'supplied_design_defaults.json')
    raw_hash=sha(source)
    expanded=expand_design(record,difficulty=difficulty,world=world,defaults=defaults)
    canonical=(json.dumps(expanded,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    binding={'schema_version':'provided-compact-design-binding-v1',
             'student_file':str(source),'student_file_sha256':raw_hash,
             'difficulty':difficulty,'expanded_design_file_sha256':hashlib.sha256(canonical).hexdigest(),
             'adapter_sha256':sha(root/'student_design_input.py'),
             'defaults_sha256':sha(root/'supplied_design_defaults.json'),
             'world_sha256':sha(task/'world.json'),
             'source_and_finish_supplied':True,'active_choices_changed':False,
             'physics_or_planner_called':False}
    identity=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    directory=root/'output'/'provided_design_inputs'/identity
    require(directory.resolve().is_relative_to(root), 'Provided input cache must remain inside this repository')
    directory.mkdir(parents=True,exist_ok=True)
    for name,data in [('design.json',canonical),('input_binding.json',(json.dumps(binding,indent=2)+'\n').encode())]:
        path=directory/name
        require(path.resolve().is_relative_to(root), 'Provided input cache path escaped this repository')
        try:
            with path.open('xb') as stream: stream.write(data)
        except FileExistsError:
            require(path.read_bytes()==data, 'Generated input binding was modified. Preserve it and choose a fresh output workspace.')
    require(sha(source)==raw_hash, 'Student design changed during input preparation')
    return replace_option(argv,'design',directory/'design.json'), binding

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    require(argv and argv[0] in ('plan','review'), 'Use project_run.py plan or project_run.py review')
    kind,args=argv[0],argv[1:]
    try:
        forwarded,binding=prepare_arguments(kind,args)
        module=importlib.import_module('physics.plan_student_transport' if kind=='plan' else 'physics.review_tier_transport')
        return module.main(forwarded)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print('student_design_input.py: error: '+str(exc),file=sys.stderr)
        print(json.dumps({'status':'DESIGN_INPUT_ERROR','message':str(exc)}),file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
