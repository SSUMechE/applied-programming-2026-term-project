"""Additive attempt history, explicit final selection and instructor freeze.

The legacy submissions and best_results tables are never rewritten or merged.
"""
from __future__ import annotations

import json
import re

from .contracts import canonical_bytes, digest, exact_keys, require, sha
from .store import Conflict, NotFound, Store, Unauthorized, utc_now
from .v3 import (RULE_SCHEMA, SELECTION_SCHEMA, ranked_rows, utc_timestamp,
                 validate_attempt, validate_snapshot)


class AttemptStore:
    def __init__(self, base):
        self.base = base if isinstance(base, Store) else Store(base)
        with self.base.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts_v3 (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id TEXT NOT NULL REFERENCES teams(team_id),
                    rule_digest TEXT NOT NULL REFERENCES rules(rule_digest),
                    run_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('SUCCESS','FAILED')),
                    finished_at_utc TEXT NOT NULL,
                    accepted_at_utc TEXT NOT NULL,
                    UNIQUE(team_id,run_id)
                );
                CREATE INDEX IF NOT EXISTS attempts_v3_team_rule
                    ON attempts_v3(team_id,rule_digest,sequence);
                CREATE TABLE IF NOT EXISTS selections_v3 (
                    team_id TEXT NOT NULL REFERENCES teams(team_id),
                    rule_digest TEXT NOT NULL REFERENCES rules(rule_digest),
                    run_id TEXT NOT NULL,
                    selection_json TEXT NOT NULL,
                    selected_at_utc TEXT NOT NULL,
                    PRIMARY KEY(team_id,rule_digest),
                    FOREIGN KEY(team_id,run_id) REFERENCES attempts_v3(team_id,run_id)
                );
                CREATE TABLE IF NOT EXISTS selection_history_v3 (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id TEXT NOT NULL REFERENCES teams(team_id),
                    rule_digest TEXT NOT NULL REFERENCES rules(rule_digest),
                    selection_json TEXT NOT NULL,
                    selected_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS freezes_v3 (
                    rule_digest TEXT PRIMARY KEY REFERENCES rules(rule_digest),
                    frozen_at_utc TEXT NOT NULL,
                    population_sha256 TEXT NOT NULL,
                    board_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qualification_v3 (
                    rule_digest TEXT PRIMARY KEY REFERENCES rules(rule_digest),
                    evidence_sha256 TEXT NOT NULL,
                    activated_at_utc TEXT NOT NULL
                );
            ''')

    def record(self, team_id, rule, record):
        validate_attempt(rule, record, allow_unqualified=True)
        encoded = canonical_bytes(record).decode()
        request_hash = digest(record)
        with self.base.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM teams WHERE team_id=?', (team_id,)).fetchone():
                raise Unauthorized('Unknown team')
            prior = db.execute('SELECT * FROM attempts_v3 WHERE team_id=? AND run_id=?', (team_id, record['run_id'])).fetchone()
            if prior:
                if prior['request_digest'] != request_hash or prior['rule_digest'] != rule['rule_digest']:
                    raise Conflict('This run ID is already bound to different attempt bytes')
                duplicate, sequence = True, prior['sequence']
            else:
                row = db.execute('INSERT INTO attempts_v3(team_id,rule_digest,run_id,request_digest,record_json,outcome,finished_at_utc,accepted_at_utc) VALUES(?,?,?,?,?,?,?,?)',
                                 (team_id, rule['rule_digest'], record['run_id'], request_hash, encoded, record['outcome'],
                                  utc_timestamp(record['finished_at_utc']), utc_now()))
                duplicate, sequence = False, row.lastrowid
            db.commit()
        return {'status': 'recorded', 'run_id': record['run_id'], 'outcome': record['outcome'],
                'rule_id': rule['rule_id'], 'sequence': sequence, 'duplicate': duplicate,
                'final_selected': False, 'scoring_enabled': self.activation(rule) is not None,
                'source_trust': 'student_run_source_matched'}

    def activation(self, rule):
        with self.base.connection() as db:
            row = db.execute('SELECT evidence_sha256,activated_at_utc FROM qualification_v3 WHERE rule_digest=?',
                             (rule['rule_digest'],)).fetchone()
        return dict(row) if row else None

    def activate(self, rule, evidence_sha256):
        require(rule['schema_version'] == RULE_SCHEMA, 'QUALIFICATION_RULE', 'External activation is only available for v3.')
        sha(evidence_sha256, 'qualification evidence')
        with self.base.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM qualification_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone()
            if old:
                require(old['evidence_sha256'] == evidence_sha256, 'QUALIFICATION_CONFLICT',
                        'This activation is bound to different qualification evidence.', 409)
                db.commit()
                return {'status': 'activated', 'rule_id': rule['rule_id'], 'evidence_sha256': evidence_sha256, 'duplicate': True}
            db.execute('INSERT INTO qualification_v3 VALUES(?,?,?)', (rule['rule_digest'], evidence_sha256, utc_now()))
            db.commit()
        return {'status': 'activated', 'rule_id': rule['rule_id'], 'evidence_sha256': evidence_sha256, 'duplicate': False}

    @staticmethod
    def _row(row):
        record = json.loads(row['record_json'])
        report = record['evaluation']
        result = {'team_id': row['team_id'], 'display_name': row['display_name'], 'run_id': row['run_id'],
                  'finished_at_utc': row['finished_at_utc'], 'accepted_at_utc': row['accepted_at_utc'],
                  'outcome': record['outcome'], 'student_snapshot_sha256': record['student_snapshot']['snapshot_sha256'],
                  'valid_carry_count': record['valid_carry_count'], 'required_carry_count': record['required_carry_count'],
                  'failure': record['failure']}
        if report:
            result.update(carried_distance_mm=report['carried_distance_mm'], completion_ticks=report['completion_ticks'])
        return result

    def history(self, team_id, rule, *, before_sequence=None, limit=100):
        require(type(limit) is int and 1 <= limit <= 200, 'HISTORY_LIMIT', 'Use 1..200 history rows.')
        if before_sequence is not None:
            require(type(before_sequence) is int and before_sequence > 0, 'HISTORY_CURSOR', 'Use a positive history sequence.')
        with self.base.connection() as db:
            query = '''SELECT a.*,t.display_name FROM attempts_v3 a JOIN teams t USING(team_id)
                       WHERE a.team_id=? AND a.rule_digest=?'''
            params = [team_id, rule['rule_digest']]
            if before_sequence is not None:
                query += ' AND a.sequence<?'
                params.append(before_sequence)
            query += ' ORDER BY a.sequence DESC LIMIT ?'
            rows = db.execute(query, [*params, limit]).fetchall()
            selection = db.execute('SELECT selection_json FROM selections_v3 WHERE team_id=? AND rule_digest=?',
                                   (team_id, rule['rule_digest'])).fetchone()
            frozen = db.execute('SELECT frozen_at_utc FROM freezes_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone()
        selected = json.loads(selection[0]) if selection else None
        result = []
        for row in rows:
            item = self._row(row)
            item['sequence'] = row['sequence']
            item['selected'] = bool(selected and selected['run_id'] == item['run_id'])
            item['selected_commit_id'] = selected['commit_id'] if item['selected'] else None
            result.append(item)
        return {'records': result, 'selected': selected,
                'frozen_at_utc': frozen[0] if frozen else None,
                'next_before_sequence': rows[-1]['sequence'] if len(rows) == limit else None}

    def get_attempt(self, team_id, run_id):
        with self.base.connection() as db:
            row = db.execute('SELECT record_json FROM attempts_v3 WHERE team_id=? AND run_id=?', (team_id, run_id)).fetchone()
        if not row:
            raise NotFound('Attempt not found for this team')
        return json.loads(row[0])

    def select(self, team_id, rule, selection):
        exact_keys(selection, {'schema_version', 'rule_id', 'rule_digest', 'run_id', 'commit_id', 'commit_student_snapshot'}, 'Final selection')
        require(selection['schema_version'] == SELECTION_SCHEMA and rule['schema_version'] == RULE_SCHEMA
                and selection['rule_id'] == rule['rule_id'] and selection['rule_digest'] == rule['rule_digest'],
                'SELECTION_RULE', 'Select a successful attempt under this exact v3 rule.')
        require(type(selection['commit_id']) is str and re.fullmatch('[0-9a-f]{40}', selection['commit_id']),
                'COMMIT_ID', 'Final selection requires the full commit ID.')
        validate_snapshot(selection['commit_student_snapshot'])
        encoded = canonical_bytes(selection).decode()
        with self.base.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            require(db.execute('SELECT 1 FROM qualification_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone() is not None,
                    'RULE_NOT_QUALIFIED', 'This rule is still in non-scoring staging.', 409)
            old = db.execute('SELECT selection_json FROM selections_v3 WHERE team_id=? AND rule_digest=?',
                             (team_id, rule['rule_digest'])).fetchone()
            # A retry after freeze may confirm the exact existing selection.
            if old and old[0] == encoded:
                db.commit()
                return {'status': 'selected', 'run_id': selection['run_id'], 'commit_id': selection['commit_id'], 'duplicate': True}
            require(not db.execute('SELECT 1 FROM freezes_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone(),
                    'SELECTIONS_FROZEN', 'The instructor has frozen final selections for this rule.', 409)
            row = db.execute('SELECT * FROM attempts_v3 WHERE team_id=? AND run_id=? AND rule_digest=?',
                             (team_id, selection['run_id'], rule['rule_digest'])).fetchone()
            require(row is not None, 'SELECTION_RUN', 'Select an uploaded attempt owned by your team.', 409)
            record = json.loads(row['record_json'])
            require(record['outcome'] == 'SUCCESS', 'SELECTION_FAILED_RUN', 'Only a successful full run can be selected.', 409)
            require(selection['commit_student_snapshot'] == record['student_snapshot'], 'SELECTION_COMMIT_MISMATCH',
                    'The selected Git commit does not contain the student files used by this run.', 409)
            now = utc_now()
            db.execute('''INSERT INTO selections_v3 VALUES(?,?,?,?,?) ON CONFLICT(team_id,rule_digest)
                          DO UPDATE SET run_id=excluded.run_id,selection_json=excluded.selection_json,selected_at_utc=excluded.selected_at_utc''',
                       (team_id, rule['rule_digest'], selection['run_id'], encoded, now))
            db.execute('INSERT INTO selection_history_v3(team_id,rule_digest,selection_json,selected_at_utc) VALUES(?,?,?,?)',
                       (team_id, rule['rule_digest'], encoded, now))
            db.commit()
        return {'status': 'selected', 'run_id': selection['run_id'], 'commit_id': selection['commit_id'], 'duplicate': False}

    def _population(self, db, rule, view):
        if view == 'live':
            rows = db.execute('''SELECT * FROM (
                SELECT a.*,t.display_name, ROW_NUMBER() OVER (
                    PARTITION BY a.team_id ORDER BY a.finished_at_utc DESC,a.sequence DESC) AS newest
                FROM attempts_v3 a JOIN teams t USING(team_id)
                WHERE a.rule_digest=? AND a.outcome='SUCCESS') WHERE newest=1''', (rule['rule_digest'],)).fetchall()
            values = [self._row(row) for row in rows]
        else:
            rows = db.execute('''SELECT a.*,t.display_name,s.selection_json FROM selections_v3 s
                JOIN attempts_v3 a ON a.team_id=s.team_id AND a.run_id=s.run_id AND a.rule_digest=s.rule_digest
                JOIN teams t ON t.team_id=a.team_id WHERE s.rule_digest=? AND a.outcome='SUCCESS' ''', (rule['rule_digest'],)).fetchall()
            values = [{**self._row(row), 'commit_id': json.loads(row['selection_json'])['commit_id']} for row in rows]
        ranked = ranked_rows(values, rule['difficulty'])
        for row in ranked:
            row['simulation_seconds'] = row['completion_ticks'] * rule['dt_s']
        return ranked

    def board(self, rule, view='live'):
        require(rule['schema_version'] == RULE_SCHEMA and view in ('live', 'selected'), 'BOARD_VIEW', 'Use a v3 live or selected board.')
        with self.base.connection() as db:
            active = db.execute('SELECT * FROM qualification_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone()
            frozen = db.execute('SELECT * FROM freezes_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone()
            rows = json.loads(frozen['board_json']) if frozen and view == 'selected' else self._population(db, rule, view)
        staging_rows = []
        if not active:
            staging_rows = [{k: v for k, v in row.items() if k not in ('rank', 'C', 'C_exact', 'competition_adjustment', 'competition_adjustment_exact', 'population_n')} for row in rows]
            rows = []
        return {'rows': rows, 'staging_rows': staging_rows, 'enabled': bool(active), 'view': view, 'population_n': len(rows), 'rule_id': rule['rule_id'],
                'rule_digest': rule['rule_digest'], 'ranking_metric': 'equal_weight_normalized_distance_time',
                'frozen': bool(frozen), 'frozen_at_utc': frozen['frozen_at_utc'] if frozen else None,
                'population_sha256': frozen['population_sha256'] if frozen and view == 'selected' else digest(rows)}

    def freeze(self, rule):
        require(rule['schema_version'] == RULE_SCHEMA, 'FREEZE_RULE', 'Freeze an installed v3 rule.')
        with self.base.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            require(db.execute('SELECT 1 FROM qualification_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone() is not None,
                    'RULE_NOT_QUALIFIED', 'Cannot freeze an unqualified staging board.', 409)
            existing = db.execute('SELECT * FROM freezes_v3 WHERE rule_digest=?', (rule['rule_digest'],)).fetchone()
            if existing:
                db.commit()
                return {'status': 'frozen', 'rule_id': rule['rule_id'], 'frozen_at_utc': existing['frozen_at_utc'],
                        'population_sha256': existing['population_sha256'], 'duplicate': True}
            rows = self._population(db, rule, 'selected')
            require(bool(rows), 'FREEZE_EMPTY', 'There are no successful final selections to freeze.', 409)
            now, population_hash = utc_now(), digest(rows)
            db.execute('INSERT INTO freezes_v3 VALUES(?,?,?,?)',
                       (rule['rule_digest'], now, population_hash, canonical_bytes(rows).decode()))
            db.commit()
        return {'status': 'frozen', 'rule_id': rule['rule_id'], 'frozen_at_utc': now,
                'population_n': len(rows), 'population_sha256': population_hash, 'duplicate': False}
