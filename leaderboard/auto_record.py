"""Durable automatic attempt records and retry without repeating Genesis.

Only hashes and bounded result data leave this computer. Local evidence paths,
student source text and credentials are never included in the request payload.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .contracts import canonical_bytes, decode_json, digest, load_rule, require, source_snapshot, runtime_snapshot
from .evaluate import verify_review
from .submit import _NoRedirect, token_value
from .v3 import (RECORD_SCHEMA, RULE_SCHEMA, SELECTION_SCHEMA, capture_student_snapshot,
                 commit_student_snapshot, utc_timestamp, validate_attempt, validate_snapshot)


DEFAULT_SERVER = 'https://phoniness-reenact-veggie.ngrok-free.dev'


def _read(path, limit=64 * 1024 * 1024):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'RECORD_FILE_TOO_LARGE', 'The local result exceeds its supplied bound.')
    return decode_json(raw), hashlib.sha256(raw).hexdigest()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix='.pending-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical_bytes(value) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def request_json(server_url, endpoint, token, *, payload=None, timeout_s=15):
    parsed = urlsplit(server_url)
    require(parsed.scheme in ('https', 'http') and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ('', '/'),
            'SERVER_URL', 'Use the class HTTPS base URL without credentials or extra path.')
    require(parsed.scheme == 'https' or parsed.hostname in ('localhost', '127.0.0.1', '::1'),
            'SERVER_URL', 'Remote record uploads require HTTPS.')
    require(type(token) is str and token and not any(c.isspace() for c in token), 'TOKEN_REQUIRED', 'A team token is required.')
    require(endpoint.startswith('/api/v3/') and len(endpoint) <= 1024, 'ENDPOINT', 'Use the supplied record endpoint.')
    body = canonical_bytes(payload) if payload is not None else None
    require(body is None or len(body) <= 1_048_576, 'RECORD_TOO_LARGE', 'Record exceeds the 1 MiB limit.')
    request = Request(server_url.rstrip('/') + endpoint, data=body,
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                               'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true'},
                      method='POST' if body is not None else 'GET')
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout_s) as response:
            raw = response.read(1_048_577)
            require(len(raw) <= 1_048_576, 'SERVER_RESPONSE', 'Server response is too large.')
            result = decode_json(raw)
            require(type(result) is dict, 'SERVER_RESPONSE', 'Server response is not an object.')
            return result
    except HTTPError as error:
        # Never echo response text, authorization headers or the token.
        raise ValueError('HTTP_' + str(error.code)) from None
    except (URLError, TimeoutError, ConnectionError):
        raise ValueError('CONNECTION_UNAVAILABLE') from None


def queue_record(record, queue_dir):
    target = Path(queue_dir).resolve() / (record['run_id'] + '.json')
    if target.exists():
        old, _ = _read(target, 1_048_576)
        require(old == record, 'QUEUE_RUN_CONFLICT', 'This run ID already has different queued bytes.', 409)
    else:
        # Link a fully fsynced temporary file into place. An interrupted write
        # cannot leave a truncated visible queue record, and link is exclusive.
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('wb', dir=target.parent, prefix='.queue-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(canonical_bytes(record) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            old, _ = _read(target, 1_048_576)
            require(old == record, 'QUEUE_RUN_CONFLICT', 'Concurrent record differs.', 409)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return target


def upload_queued(path, *, server_url=None, token_file=None):
    path = Path(path).resolve(strict=True)
    record, _ = _read(path, 1_048_576)
    require(record.get('schema_version') == RECORD_SCHEMA, 'QUEUE_RECORD', 'Use an automatically recorded attempt.')
    status_path = path.with_suffix('.status.json')
    base = {'run_id': record['run_id'], 'simulation_outcome': record['outcome'],
            'record_file': str(path), 'native_physics_launched': False}
    url = server_url or os.environ.get('AP_LEADERBOARD_URL') or DEFAULT_SERVER
    try:
        token = token_value(token_file=token_file or os.environ.get('AP_LEADERBOARD_TOKEN_FILE'))
    except (OSError, ValueError):
        result = {**base, 'status': 'pending', 'reason': 'TEAM_TOKEN_REQUIRED'}
        _atomic(status_path, result)
        return result
    try:
        answer = request_json(url, '/api/v3/records', token, payload=record)
        require(answer.get('status') == 'recorded' and answer.get('run_id') == record['run_id'],
                'UPLOAD_ACK', 'The server did not acknowledge this run.')
        result = {**base, 'status': 'uploaded', 'duplicate': answer.get('duplicate') is True,
                  'server_sequence': answer.get('sequence'), 'scoring_enabled': answer.get('scoring_enabled') is True}
    except (OSError, ValueError) as error:
        code = getattr(error, 'code', None)
        message = str(error)
        reason = code if type(code) is str else message if re.fullmatch('[A-Z0-9_]{1,80}', message) else 'UPLOAD_UNAVAILABLE'
        result = {**base, 'status': 'pending', 'reason': reason}
    _atomic(status_path, result)
    return result


def retry_queue(queue_dir, *, server_url=None, token_file=None):
    statuses = []
    for path in sorted(Path(queue_dir).glob('*.json')):
        if path.name.endswith('.status.json'):
            continue
        status_path = path.with_suffix('.status.json')
        if status_path.exists():
            old, _ = _read(status_path, 1_048_576)
            if old.get('status') == 'uploaded':
                continue
        statuses.append(upload_queued(path, server_url=server_url, token_file=token_file))
    return {'status': 'retry_complete', 'attempts': statuses, 'native_physics_launched': False}


def _partial_count(native, required):
    proof = native.get('device_tier_execution', {})
    progress = proof.get('partial_progress', native.get('partial_progress')) if type(proof) is dict else None
    if progress is None:
        return None
    require(type(progress) is dict, 'ATTEMPT_PROGRESS', 'Invalid native carry progress.')
    count = progress.get('valid_carry_count')
    require(type(count) is int and 0 <= count <= required and progress.get('required_carry_count') == required
            and progress.get('valid_carry_stage_ids') == ['arm_' + str(i) + '__carry' for i in range(count)],
            'ATTEMPT_PROGRESS', 'Native carry progress must be the measured accepted prefix.')
    return count


def auto_record_from_review(rule_path, review_dir, *, project_root, student_snapshot, queue_dir,
                            server_url=None, token_file=None):
    rule = load_rule(rule_path)
    require(rule['schema_version'] == RULE_SCHEMA, 'RULE_VERSION', 'Automatic attempt recording requires term v3.')
    validate_snapshot(student_snapshot)
    directory = Path(review_dir).resolve(strict=True)
    supervisor_path = directory / 'REVIEW_SUPERVISOR.json'
    supervisor, supervisor_sha = _read(supervisor_path)
    require(supervisor.get('scope') == 'full_mission' and supervisor.get('contents') == 'beads'
            and supervisor.get('difficulty') == rule['difficulty'] and supervisor.get('rule_digest') == rule['rule_digest']
            and supervisor.get('student_snapshot') == student_snapshot,
            'ATTEMPT_SCOPE', 'Record only the matching full bead review with its original student snapshot.')
    # Verify the actual current supplied code. No record is allowed to silently
    # promote a modified evaluator to a source-matched run.
    source_snapshot(rule, project_root)
    runtime_snapshot(rule)
    successful = (supervisor.get('review_accepted') is True and supervisor.get('physical_transport_accepted') is True
                  and supervisor.get('full_mission_passed') is True)
    native_path = directory / 'result.json'
    native, native_sha = {}, None
    if native_path.is_file():
        try:
            native, native_sha = _read(native_path)
            require(type(native) is dict, 'NATIVE_RESULT_SCHEMA', 'Native result must be an object.')
        except ValueError:
            if successful:
                raise
            # A killed worker can leave no readable final JSON. Retain the
            # failed review and its raw-file identity with unknown progress.
            hasher = hashlib.sha256()
            with native_path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(chunk)
            native, native_sha = {}, hasher.hexdigest()
    current_snapshot = capture_student_snapshot(project_root)
    report = None
    required = len(rule['arm_ids'])
    if successful:
        require(current_snapshot == student_snapshot, 'STUDENT_INPUT_CHANGED', 'Student files changed after the evaluated snapshot.', 409)
        report, _ = verify_review(rule, directory / 'score_evidence', project_root=project_root, allow_unqualified=True)
        require(report['student_snapshot'] == student_snapshot, 'STUDENT_INPUT_CHANGED', 'Exported snapshot differs.', 409)
        run_id, outcome, count, failure = report['run_id'], 'SUCCESS', required, None
    else:
        evidence_identity = digest({'supervisor_sha256': supervisor_sha, 'native_sha256': native_sha})
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ap-attempt-v3:' + evidence_identity))
        outcome, count = 'FAILED', _partial_count(native, required)
        code = supervisor.get('outcome', 'FULL_REVIEW_FAILED')
        if current_snapshot != student_snapshot:
            code = 'STUDENT_INPUT_CHANGED'
        if type(code) is not str or not re.fullmatch('[A-Z][A-Z0-9_]{0,79}', code):
            code = 'FULL_REVIEW_FAILED'
        failure = {'code': code}
    finished = supervisor.get('finished_at_utc') or datetime.fromtimestamp(supervisor_path.stat().st_mtime, timezone.utc).isoformat()
    record = {'schema_version': RECORD_SCHEMA, 'run_id': run_id, 'rule_id': rule['rule_id'], 'rule_digest': rule['rule_digest'],
              'outcome': outcome, 'finished_at_utc': utc_timestamp(finished), 'student_snapshot': student_snapshot,
              'evaluation': report, 'failure': failure, 'valid_carry_count': count, 'required_carry_count': required,
              'evidence_sha256': digest({'supervisor_sha256': supervisor_sha, 'native_sha256': native_sha})}
    validate_attempt(rule, record, allow_unqualified=True)
    path = queue_record(record, queue_dir)
    return upload_queued(path, server_url=server_url, token_file=token_file)


def select_record(record_file, commit_id, *, project_root, server_url=None, token_file=None):
    record, _ = _read(record_file, 1_048_576)
    require(record.get('schema_version') == RECORD_SCHEMA and record.get('outcome') == 'SUCCESS',
            'SELECTION_FAILED_RUN', 'Select a locally recorded successful full run.')
    snapshot = commit_student_snapshot(project_root, commit_id)
    require(snapshot == validate_snapshot(record['student_snapshot']), 'SELECTION_COMMIT_MISMATCH',
            'The actual Git commit student blobs differ from this run. Commit the evaluated files or select another run.', 409)
    payload = {'schema_version': SELECTION_SCHEMA, 'rule_id': record['rule_id'], 'rule_digest': record['rule_digest'],
               'run_id': record['run_id'], 'commit_id': commit_id, 'commit_student_snapshot': snapshot}
    token = token_value(token_file=token_file or os.environ.get('AP_LEADERBOARD_TOKEN_FILE'))
    return request_json(server_url or os.environ.get('AP_LEADERBOARD_URL') or DEFAULT_SERVER,
                        '/api/v3/selection', token, payload=payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('retry', 'select', 'history', 'record'):
        sub = commands.add_parser(name)
        sub.add_argument('--server-url', default=None)
        sub.add_argument('--token-file', type=Path)
        if name in ('retry', 'record'):
            sub.add_argument('--queue-dir', type=Path, default=Path('output/leaderboard_queue'))
        if name == 'select':
            selection_input = sub.add_mutually_exclusive_group(required=True)
            selection_input.add_argument('--record', type=Path)
            selection_input.add_argument('--review-dir', type=Path,
                                         help='Read the queued record path from this run LEADERBOARD_STATUS.json')
            sub.add_argument('--commit', required=True)
            sub.add_argument('--project-root', type=Path, default=Path.cwd())
        if name == 'history':
            sub.add_argument('--rule-id', required=True)
        if name == 'record':
            sub.add_argument('--rule', type=Path, required=True)
            sub.add_argument('--review-dir', type=Path, required=True)
            sub.add_argument('--project-root', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        if args.command == 'retry':
            result = retry_queue(args.queue_dir, server_url=args.server_url, token_file=args.token_file)
        elif args.command == 'select':
            record_path = args.record
            if args.review_dir is not None:
                status, _ = _read(args.review_dir / 'LEADERBOARD_STATUS.json', 1_048_576)
                require(type(status.get('record_file')) is str, 'LOCAL_RECORD_MISSING', 'Retry recording this completed run before final selection.')
                record_path = Path(status['record_file'])
            result = select_record(record_path, args.commit, project_root=args.project_root,
                                   server_url=args.server_url, token_file=args.token_file)
        elif args.command == 'record':
            supervisor, _ = _read(args.review_dir / 'REVIEW_SUPERVISOR.json')
            result = auto_record_from_review(args.rule, args.review_dir, project_root=args.project_root,
                student_snapshot=supervisor['student_snapshot'], queue_dir=args.queue_dir,
                server_url=args.server_url, token_file=args.token_file)
        else:
            from urllib.parse import quote
            token = token_value(token_file=args.token_file or os.environ.get('AP_LEADERBOARD_TOKEN_FILE'))
            result = request_json(args.server_url or os.environ.get('AP_LEADERBOARD_URL') or DEFAULT_SERVER,
                                  '/api/v3/history?rule_id=' + quote(args.rule_id, safe=''), token)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'record_action_failed', 'code': getattr(error, 'code', 'LOCAL_OR_NETWORK_ERROR'),
                          'native_physics_launched': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
