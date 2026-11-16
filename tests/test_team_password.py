"""Supplied account-support checks. Temporary SQLite and mocked client I/O only."""
import copy
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import Mock, patch

from diagnostics import enrollment_client as client
from diagnostics.enrollment_server import password_bytes
from diagnostics.protocol import DiagnosticError
from diagnostics.server import create_app
from tests.test_diagnostics_server import example, request


def device(letter):
    return 'apd_' + letter * 43


class PasswordValidationTests(unittest.TestCase):
    def test_client_and_server_accept_short_existing_and_utf8_boundary(self):
        for value in ('a', '42', '한글', ' padded ', 'previous-password-123',
                      'x' * 256, '😀' * 64, '한' * 85 + 'x'):
            with self.subTest(value_length=len(value)):
                client.validate_password(value)
                self.assertEqual(password_bytes(value), value.encode('utf-8'))

    def test_client_and_server_reject_invalid_without_raw_encoding_errors(self):
        for value in (None, 123, b'abc', '', '   ', '\t', 'a\nb', 'a\0b',
                      'a\u2028b', '\ud800', 'x' * 257, '😀' * 65, '한' * 86):
            with self.subTest(value=repr(value)):
                with self.assertRaises(client.EnrollmentError):
                    client.validate_password(value)
                with self.assertRaises(DiagnosticError):
                    password_bytes(value)


class PasswordServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        admin = root / 'instructor.secret'
        admin.write_text(secrets.token_urlsafe(48), encoding='ascii')
        self.app = create_app(root/'board.sqlite', root/'diagnostics.sqlite', admin,
                              public_origin='https://class.example.test')
        self.a, self.a2, self.b = device('A'), device('B'), device('C')
        self.old = 'previous-password-123'
        self.team_a = self.connect('20260001', self.old, self.a)['value']['team_id']
        self.assertEqual(self.connect('20260001', self.old, self.a2)['status'], 200)
        self.team_b = self.connect('20260002', 'other-password-123', self.b)['value']['team_id']

    def connect(self, name, password, token):
        return request(self.app, '/api/enrollment/connect', method='POST',
                       value={'team_name': name, 'password': password, 'device_token': token})

    def change(self, password, token=None, **extra):
        return request(self.app, '/api/enrollment/password', method='POST', token=token,
                       value={'new_password': password, **extra})

    def table(self, name):
        with self.app.leaderboard.store.connection() as db:
            return sorted([tuple(row) for row in db.execute('SELECT * FROM '+name)], key=repr)

    def test_recovery_retains_identity_devices_and_existing_history(self):
        # Synthetic history rows exist only in this temporary test database.
        # No simulated result or competition receipt is fabricated for delivery.
        digest = '0' * 64
        with self.app.leaderboard.store.connection() as db:
            db.execute('INSERT INTO rules VALUES(?,?,?,?)',
                       (digest, 'password-fixture', '{}', '2026-11-17T00:00:00Z'))
            db.execute('''INSERT INTO attempts_v3
                (team_id,rule_digest,run_id,request_digest,record_json,outcome,finished_at_utc,accepted_at_utc)
                VALUES(?,?,?,?,?,?,?,?)''',
                (self.team_a, digest, 'fixture-run', '1'*64, '{}', 'SUCCESS',
                 '2026-11-17T00:00:00Z', '2026-11-17T00:00:00Z'))
            db.execute('INSERT INTO selections_v3 VALUES(?,?,?,?,?)',
                       (self.team_a, digest, 'fixture-run', '{}', '2026-11-17T00:00:00Z'))
        report = example()
        self.assertEqual(request(self.app, '/api/diagnostics/reports', method='POST',
                                 value=report, token=self.a)['status'], 201)
        diagnostics_before = request(self.app, '/api/diagnostics/own', token=self.a)['value']
        tables = ('teams', 'enrollment_devices', 'attempts_v3', 'selections_v3', 'rules')
        before = {name: self.table(name) for name in tables}
        enrollment_before = self.table('enrollment_teams')
        changed = self.change('7', self.a)
        self.assertEqual(changed['status'], 200, changed)
        self.assertEqual(changed['value']['status'], 'password_updated')
        self.assertEqual(changed['value']['team_id'], self.team_a)
        self.assertEqual({name: self.table(name) for name in tables}, before)
        enrollment_after = self.table('enrollment_teams')
        other_before = [r for r in enrollment_before if self.team_b in r]
        other_after = [r for r in enrollment_after if self.team_b in r]
        self.assertEqual(other_before, other_after)
        self.assertNotEqual(enrollment_before, enrollment_after)
        for token in (self.a, self.a2):
            identity = request(self.app, '/api/enrollment/identity', token=token)
            self.assertEqual(identity['status'], 200)
            self.assertEqual(identity['value']['team_id'], self.team_a)
        self.assertEqual(request(self.app, '/api/diagnostics/own', token=self.a)['value'], diagnostics_before)
        self.assertEqual(self.connect('20260001', self.old, device('D'))['status'], 401)
        self.assertEqual(self.connect('20260001', '7', device('E'))['value']['team_id'], self.team_a)
        self.assertEqual(self.connect('20260002', 'other-password-123', device('F'))['status'], 200)

    def test_requires_active_device_and_cannot_target_another_team(self):
        before = self.table('enrollment_teams')
        for token in (None, device('Z')):
            self.assertEqual(self.change('new', token)['status'], 401)
        self.app.enrollment.revoke(self.a)
        self.assertEqual(self.change('new', self.a)['status'], 401)
        self.assertEqual(self.change('new', self.b, team_id=self.team_a)['status'], 400)
        self.assertEqual(self.table('enrollment_teams'), before)
        own = self.change('new', self.b)
        self.assertEqual(own['status'], 200)
        self.assertEqual(own['value']['team_id'], self.team_b)
        self.assertEqual(self.connect('20260001', self.old, device('G'))['status'], 200)

    def test_invalid_password_or_request_does_not_change_storage(self):
        before = self.table('enrollment_teams')
        for value in ('', '   ', 'a\nb', 'x'*257):
            with self.subTest(value_length=len(value)):
                result = self.change(value, self.a)
                self.assertEqual(result['status'], 400)
        self.assertEqual(request(self.app, '/api/enrollment/password', token=self.a)['status'], 405)
        self.assertEqual(request(self.app, '/api/enrollment/password', method='POST',
                                 value={'new_password':'new'}, token=self.a,
                                 origin='https://other.example.test')['status'], 403)
        self.assertEqual(self.table('enrollment_teams'), before)

    def test_password_not_returned_or_appended_to_history(self):
        password = 'PRIVATE_TEST_PASSWORD_17'
        before = {name: self.table(name) for name in ('attempts_v3', 'selection_history_v3', 'enrollment_devices')}
        answer = self.change(password, self.a)
        self.assertEqual(answer['status'], 200)
        self.assertNotIn(password, answer['raw'].decode())
        self.assertEqual({name: self.table(name) for name in before}, before)
        with self.app.leaderboard.store.connection() as db:
            rows = db.execute('SELECT password_hash,password_salt FROM enrollment_teams WHERE team_id=?',
                              (self.team_a,)).fetchone()
        self.assertTrue(all(password.encode() not in bytes(value) for value in rows))


class PasswordClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'repo'
        self.root.mkdir()
        self.server = 'https://class.example.test'
        self.token = device('A')
        self.data = {'team_id':'auto_test_team','team_name':'20260001'}
        self.identity = {'status':'connected', **self.data}

    def test_new_password_confirmation_mismatch_does_not_post_or_save(self):
        with patch.object(client, '_read', return_value=(copy.deepcopy(self.data), self.token)), \
                patch.object(client, 'request', return_value=self.identity) as remote, \
                patch.object(client, '_save') as save:
            with self.assertRaises(client.EnrollmentError):
                client.change_password(self.root, self.server,
                    read_password=Mock(side_effect=['new', 'different']), write=Mock())
            self.assertTrue(all(call.kwargs.get('payload') is None for call in remote.call_args_list))
            save.assert_not_called()

    def test_authenticated_recovery_needs_no_old_password_and_uses_same_token(self):
        answers = [self.identity, {'status':'password_updated', **self.data}]
        prompt, write = Mock(side_effect=['7','7']), Mock()
        with patch.object(client, '_read', return_value=(copy.deepcopy(self.data), self.token)), \
                patch.object(client, 'request', side_effect=answers) as remote, \
                patch.object(client, '_save'):
            client.change_password(self.root, self.server, read_password=prompt, write=write)
        self.assertEqual(prompt.call_count, 2)
        post = remote.call_args_list[-1]
        self.assertEqual(post.args[1], '/api/enrollment/password')
        self.assertEqual(post.kwargs['payload'], {'new_password':'7'})
        self.assertEqual(post.kwargs['token'], self.token)
        self.assertTrue(all('old' not in str(c).lower() for c in prompt.call_args_list))
        self.assertNotIn(self.token, str(write.call_args_list))

    def test_identity_mismatch_or_revocation_prevents_password_post(self):
        for answer in ({'status':'connected','team_id':'another-team','team_name':'20260002'},
                       client.EnrollmentError('TEAM_SIGN_IN_REFUSED','refused')):
            with self.subTest(answer_type=type(answer).__name__), \
                    patch.object(client, '_read', return_value=(copy.deepcopy(self.data), self.token)), \
                    patch.object(client, 'request', side_effect=[answer]) as remote, \
                    patch.object(client, '_save') as save:
                prompt = Mock()
                with self.assertRaises(client.EnrollmentError):
                    client.change_password(self.root,self.server,read_password=prompt,write=Mock())
                prompt.assert_not_called()
                self.assertEqual(remote.call_count, 1)
                save.assert_not_called()

    def test_failed_forced_reconnect_preserves_working_local_credential(self):
        with patch.object(client, '_read', return_value=(copy.deepcopy(self.data), self.token)), \
                patch.object(client, 'request', side_effect=client.EnrollmentError('TEAM_SIGN_IN_REFUSED','refused')), \
                patch.object(client, '_save') as save:
            with self.assertRaises(client.EnrollmentError):
                client._connect_locked(self.root,self.server,self.data['team_name'],'wrong',new_device=True)
            save.assert_not_called()

    def test_fresh_registration_confirmation_mismatch_does_not_connect(self):
        with patch.object(client,'stored_account',return_value=None), \
                patch.object(client,'_read',return_value=(None,'')), \
                patch.object(client,'connect') as connect:
            with self.assertRaises(client.EnrollmentError):
                client.ensure_account(self.root,self.server,interactive=True,
                    read_name=lambda _:self.data['team_name'],
                    read_password=Mock(side_effect=['short','typo']),write=Mock())
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
