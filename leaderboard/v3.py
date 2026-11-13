"""V3 bead records, exact competition arithmetic and student-input identity.

These are source-matched student-run records, not remote execution attestation.
Legacy records and their time-only boards retain their original contracts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

from .contracts import (ProtocolError, canonical_bytes, digest, exact_keys,
                        finite, integer, require, sha)

RULE_SCHEMA = 'ap-leaderboard-rule-v3'
RECORD_SCHEMA = 'ap-attempt-record-v3'
SNAPSHOT_SCHEMA = 'ap-student-snapshot-v1'
SELECTION_SCHEMA = 'ap-final-selection-v3'
EXTRA_RULE_FIELDS = {'competition', 'allowed_handoff_support_ids'}
EXTRA_EVALUATION_FIELDS = {'carried_distance_mm', 'distance_metric', 'support_route', 'student_snapshot'}
BONUSES = {'easy': (5, 3, 1), 'medium': (10, 5, 3), 'hard': (15, 10, 5)}
BASE_CAPS = {'easy': 80, 'medium': 90, 'hard': 100}


def competition_contract():
    return {'schema_version': 'ap-distance-time-competition-v1',
            'distance_unit': 'mm', 'time_unit': 'native_ticks',
            'distance_weight': [1, 2], 'time_weight': [1, 2],
            'normalization': 'population_minmax_zero_spread_zero',
            'ties': 'exact_fraction_shared_rank_1_1_3',
            'live_population': 'latest_successful_attempt_per_team',
            'official_population': 'explicit_successful_selection_per_team',
            'bonuses': {k: list(v) for k, v in BONUSES.items()},
            'lower_penalty': '-N*(C-C4)/(Cmax-C4) for rank>=5 and N>=5; zero when Cmax=C4',
            'freeze': 'explicit_instructor_command'}


def validate_snapshot(value):
    exact_keys(value, {'schema_version', 'files', 'snapshot_sha256'}, 'Student snapshot')
    require(value['schema_version'] == SNAPSHOT_SCHEMA, 'STUDENT_SNAPSHOT', 'Unknown student snapshot format.')
    files = value['files']
    require(type(files) is dict and 1 <= len(files) <= 2000, 'STUDENT_SNAPSHOT', 'Student source hashes are required.')
    for name, value_hash in files.items():
        require(type(name) is str and 0 < len(name) <= 240 and '\\' not in name and ':' not in name and '\0' not in name,
                'STUDENT_SNAPSHOT_PATH', 'Student hashes use relative student/ paths only.')
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and path.as_posix() == name
                and path.parts[0] == 'student' and path.suffix in ('.py', '.json'),
                'STUDENT_SNAPSHOT_PATH', 'Only student Python and design JSON identities may be uploaded.')
        sha(value_hash, 'student file hash')
    require(value['snapshot_sha256'] == digest(files), 'STUDENT_SNAPSHOT', 'Student snapshot digest differs.', 409)
    return value


def capture_student_snapshot(project_root):
    root = Path(project_root).resolve(strict=True)
    student = root / 'student'
    files = {}
    for path in sorted(student.rglob('*')):
        if path.suffix not in ('.py', '.json') or not path.is_file() or '__pycache__' in path.parts:
            continue
        resolved = path.resolve(strict=True)
        require(resolved.is_relative_to(student.resolve()) and not path.is_symlink(),
                'STUDENT_SNAPSHOT_PATH', 'Student sources must stay in the student directory.')
        files[path.relative_to(root).as_posix()] = student_file_hash(path.read_bytes())
    return validate_snapshot({'schema_version': SNAPSHOT_SCHEMA, 'files': files, 'snapshot_sha256': digest(files)})


def student_file_hash(raw):
    """Hash UTF-8 source with CRLF normalized to LF, matching Windows Git text.

    No other whitespace, JSON formatting or executable content is normalized.
    Native planner/source pins still bind the actual local bytes separately.
    """
    try:
        raw.decode('utf-8')
    except UnicodeError as error:
        raise ProtocolError('STUDENT_SOURCE_ENCODING', 'Use UTF-8 for student Python and JSON files.') from error
    return hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()


def commit_student_snapshot(project_root, commit_id):
    require(type(commit_id) is str and re.fullmatch('[0-9a-f]{40}', commit_id),
            'COMMIT_ID', 'Use the full 40-character commit ID.')
    root = Path(project_root).resolve(strict=True)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], timeout=20, stderr=subprocess.PIPE)
    try:
        require(git('rev-parse', '--verify', commit_id + '^{commit}').decode().strip() == commit_id,
                'COMMIT_ID', 'The selected commit does not resolve to the supplied commit ID.')
        files = {}
        for row in git('ls-tree', '-rz', commit_id, '--', 'student').split(b'\0'):
            if not row:
                continue
            metadata, encoded_path = row.split(b'\t', 1)
            mode, kind, object_id = metadata.split()
            name = encoded_path.decode('utf-8')
            if PurePosixPath(name).suffix not in ('.py', '.json'):
                continue
            require(mode in (b'100644', b'100755') and kind == b'blob',
                    'COMMIT_STUDENT_SOURCE', 'The selected student source must be a regular Git blob.')
            files[name] = student_file_hash(git('cat-file', 'blob', object_id.decode('ascii')))
    except (subprocess.SubprocessError, OSError, UnicodeError) as error:
        raise ProtocolError('COMMIT_UNAVAILABLE', 'Cannot read the selected commit from this local Git repository.') from error
    return validate_snapshot({'schema_version': SNAPSHOT_SCHEMA, 'files': files, 'snapshot_sha256': digest(files)})


def validate_v3_rule_extras(rule):
    require(rule['competition'] == competition_contract(), 'COMPETITION_CONTRACT', 'The released distance/time formula is fixed.')
    allowed = rule['allowed_handoff_support_ids']
    require(type(allowed) is list and len(allowed) <= 100
            and all(type(v) is str and re.fullmatch('[a-zA-Z0-9_-]{1,100}', v) for v in allowed)
            and len(set(allowed)) == len(allowed),
            'HANDOFF_CATALOG', 'Use the frozen allowed handoff support IDs.')
    require(len(allowed) >= len(rule['handoffs']), 'HANDOFF_CATALOG', 'Required handoffs need an allowed support.')


def validate_distance(metric, distance_mm, rule):
    fields = {'schema_version', 'sample_dt_s', 'path_length_m', 'distance_mm', 'counted_intervals',
              'segments', 'rounding', 'excludes', 'reference_point_body_m'}
    exact_keys(metric, fields, 'Carried distance')
    require(metric['schema_version'] == 'ap-carried-cup-center-distance-v1'
            and metric['sample_dt_s'] == rule['dt_s']
            and metric['rounding'] == 'nearest_mm_half_up_once_after_total'
            and metric['excludes'] == ['unheld_approach', 'donor_return', 'released_settle'],
            'DISTANCE_CONTRACT', 'Use the measured carried cup center at every outer step.')
    point = metric['reference_point_body_m']
    require(type(point) is list and len(point) == 3, 'DISTANCE_CONTRACT', 'The fixed cup center must be a 3D body point.')
    for value in point:
        finite(value, -100, 100, 'cup center')
    finite(metric['path_length_m'], 0, 1_000_000, 'carried path length')
    integer(distance_mm, 0, 1_000_000_000, 'carried_distance_mm')
    require(type(metric['distance_mm']) is int and metric['distance_mm'] == distance_mm
            and int(metric['path_length_m'] * 1000 + .5) == distance_mm,
            'DISTANCE_QUANTIZATION', 'Round the measured total once to the nearest millimetre.', 409)
    integer(metric['counted_intervals'], 1, 1_000_000_000, 'distance interval count')
    segments = metric['segments']
    require(type(segments) is list and len(rule['arm_ids']) <= len(segments) <= 10000,
            'DISTANCE_SEGMENTS', 'Each required carrying arm needs its measured distance segment.')
    count = 0
    length = 0.
    last = -1
    visited = []
    for segment in segments:
        exact_keys(segment, {'arm_id', 'from_native_step', 'to_native_step', 'path_length_m'}, 'Distance segment')
        if not visited or visited[-1] != segment['arm_id']:
            visited.append(segment['arm_id'])
        for key in ('from_native_step', 'to_native_step'):
            integer(segment[key], 0, 1_000_000_200, key)
        require(segment['to_native_step'] > segment['from_native_step'] >= last,
                'DISTANCE_SEGMENTS', 'Distance segments must advance in native time.')
        last = segment['to_native_step']
        count += segment['to_native_step'] - segment['from_native_step']
        finite(segment['path_length_m'], 0, 1_000_000, 'segment length')
        length += segment['path_length_m']
    require(visited == rule['arm_ids'] and count == metric['counted_intervals'] and abs(length - metric['path_length_m']) <= max(1e-10, length * 1e-12),
            'DISTANCE_SEGMENTS', 'Segment totals contradict the measured carried distance.', 409)


def validate_v3_evaluation_extras(rule, report):
    validate_snapshot(report['student_snapshot'])
    validate_distance(report['distance_metric'], report['carried_distance_mm'], rule)
    route = report['support_route']
    require(type(route) is list and len(route) == len(rule['arm_ids']) + 1 and route[0] == 'source_support'
            and len(set(route)) == len(route), 'HANDOFF_ROUTE', 'Use one source, the selected supports and the fixed finish.')
    expected_terminal = {'easy': 'relay_1_support', 'medium': 'relay_2_support', 'hard': 'goal_support'}[rule['difficulty']]
    require(route[-1] == expected_terminal and all(s in rule['allowed_handoff_support_ids'] for s in route[1:-1]),
            'HANDOFF_ROUTE', 'Selected handoffs or fixed final support differ from the rule.')
    return [dict(from_arm=rule['arm_ids'][i], to_arm=rule['arm_ids'][i + 1], support_id=route[i + 1],
                 released_supported_and_resting=True, same_sample_acquired=True)
            for i in range(len(rule['arm_ids']) - 1)]


def utc_timestamp(value):
    require(type(value) is str and len(value) <= 40, 'RUN_TIME', 'Use a UTC run completion timestamp.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
                'RUN_TIME', 'Use a UTC run completion timestamp.')
    except (ValueError, OverflowError) as error:
        raise ProtocolError('RUN_TIME', 'Use a valid UTC run completion timestamp.') from error
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')


RECORD_FIELDS = {'schema_version', 'run_id', 'rule_id', 'rule_digest', 'outcome', 'finished_at_utc',
                 'student_snapshot', 'evaluation', 'failure', 'valid_carry_count', 'required_carry_count',
                 'evidence_sha256'}


def validate_attempt(rule, record, *, allow_unqualified=False):
    from .contracts import validate_rule, validate_evaluation, validate_evaluation_record
    validate_rule(rule)
    require(rule['schema_version'] == RULE_SCHEMA and (rule['qualified'] or allow_unqualified is True),
            'RULE_NOT_QUALIFIED', 'V3 staging requires an explicit non-scoring admission.', 409)
    exact_keys(record, RECORD_FIELDS, 'Attempt record')
    require(record['schema_version'] == RECORD_SCHEMA and record['rule_id'] == rule['rule_id']
            and record['rule_digest'] == rule['rule_digest'], 'RECORD_RULE', 'The attempt must match its installed v3 rule.', 409)
    try:
        require(str(uuid.UUID(record['run_id'])) == record['run_id'], 'RUN_ID', 'Use a canonical run UUID.')
    except (ValueError, AttributeError, TypeError) as error:
        raise ProtocolError('RUN_ID', 'Use a canonical run UUID.') from error
    utc_timestamp(record['finished_at_utc'])
    validate_snapshot(record['student_snapshot'])
    sha(record['evidence_sha256'], 'local evidence digest')
    require(record['outcome'] in ('SUCCESS', 'FAILED'), 'ATTEMPT_OUTCOME', 'Record a successful or failed full-run attempt.')
    integer(record['required_carry_count'], 1, 3, 'required carry count')
    require(record['required_carry_count'] == len(rule['arm_ids']), 'ATTEMPT_PROGRESS', 'The carry target must match the tier.')
    valid = record['valid_carry_count']
    if valid is not None:
        integer(valid, 0, record['required_carry_count'], 'valid carry count')
    if record['outcome'] == 'SUCCESS':
        require(record['failure'] is None, 'ATTEMPT_OUTCOME', 'A successful attempt cannot contain a failure.')
        (validate_evaluation_record if allow_unqualified is True else validate_evaluation)(rule, record['evaluation'])
        require(record['evaluation']['run_id'] == record['run_id']
                and record['evaluation']['student_snapshot'] == record['student_snapshot']
                and valid == record['required_carry_count'], 'ATTEMPT_BINDING', 'The successful record differs from its verified evaluation.', 409)
    else:
        require(record['evaluation'] is None, 'ATTEMPT_OUTCOME', 'A failed attempt cannot carry a successful evaluation.')
        exact_keys(record['failure'], {'code'}, 'Failure')
        require(type(record['failure']['code']) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,79}', record['failure']['code']),
                'FAILURE_CODE', 'Use a bounded supplied failure code, without paths or source text.')
    return record


def ranked_rows(rows, difficulty):
    """Recompute C over exactly one successful representative per team."""
    require(difficulty in BONUSES, 'DIFFICULTY', 'Unknown competition tier.')
    if not rows:
        return []
    require(len({r['team_id'] for r in rows}) == len(rows), 'RANK_POPULATION', 'Use one representative per team.')
    dvals, tvals = [r['carried_distance_mm'] for r in rows], [r['completion_ticks'] for r in rows]
    for value in dvals:
        integer(value, 0, 1_000_000_000, 'distance')
    for value in tvals:
        integer(value, 1, 1_000_000_000, 'ticks')
    dmin, dmax, tmin, tmax = min(dvals), max(dvals), min(tvals), max(tvals)
    scored = []
    for row in rows:
        dn = Fraction(row['carried_distance_mm'] - dmin, dmax - dmin) if dmax != dmin else Fraction(0)
        tn = Fraction(row['completion_ticks'] - tmin, tmax - tmin) if tmax != tmin else Fraction(0)
        scored.append((Fraction(dn + tn, 2), row))
    scored.sort(key=lambda item: (item[0], item[1]['team_id']))
    result = []
    n = len(scored)
    c4, cmax = (scored[3][0] if n >= 4 else None), scored[-1][0]
    last = None
    for position, (cost, row) in enumerate(scored, 1):
        if cost != last:
            rank = position
        last = cost
        award = Fraction(BONUSES[difficulty][rank - 1]) if rank <= 3 else Fraction(0)
        if n >= 5 and rank >= 5 and cmax != c4:
            award = -Fraction(n) * (cost - c4) / (cmax - c4)
        result.append({**row, 'rank': rank, 'C': float(cost), 'C_exact': [cost.numerator, cost.denominator],
                       'competition_adjustment': float(award), 'competition_adjustment_exact': [award.numerator, award.denominator],
                       'population_n': n})
    return result


def assessed_grade(*, difficulty, design_score, valid_carry_count, required_carry_count,
                   contribution_adjustment=0, competition_adjustment=0, presentation_adjustment=0):
    require(difficulty in BASE_CAPS, 'DIFFICULTY', 'Unknown assessment tier.')
    finite(design_score, 0, 50, 'manual design score')
    integer(required_carry_count, 1, 3, 'required carries')
    integer(valid_carry_count, 0, required_carry_count, 'valid carries')
    require(required_carry_count == {'easy': 1, 'medium': 2, 'hard': 3}[difficulty], 'GRADE_INPUT', 'Carry target differs from the tier.')
    finite(contribution_adjustment, -5, 5, 'contribution adjustment')
    finite(presentation_adjustment, -10, 10, 'presentation adjustment')
    finite(competition_adjustment, -1_000_000, 15, 'competition adjustment')
    maximum = BASE_CAPS[difficulty]
    design_points = maximum * design_score / 100
    carry_score = maximum * .5 * valid_carry_count / required_carry_count
    base = min(maximum, max(0, design_points + carry_score + contribution_adjustment))
    return {'normalized_design_score': design_score, 'design_score': design_points,
            'carry_score': carry_score, 'base_after_contribution': base,
            'base_cap': BASE_CAPS[difficulty], 'competition_adjustment': competition_adjustment,
            'presentation_adjustment': presentation_adjustment,
            'total': max(0, base + competition_adjustment + presentation_adjustment)}
