"""Supplied account-support checks using temporary stores and loopback only."""
import copy
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

from diagnostics import client as reporting
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
        profile = patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name, 'XDG_DATA_HOME': self.temp.name})
        profile.start()
        self.addCleanup(profile.stop)
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
                patch.object(client,'_connect_locked') as connect:
            with self.assertRaises(client.EnrollmentError):
                client.ensure_account(self.root,self.server,interactive=True,
                    read_name=lambda _:self.data['team_name'],
                    read_password=Mock(side_effect=['short','typo']),write=Mock())
            connect.assert_not_called()


class ProvisionalCheckoutTests(unittest.TestCase):
    password = 'sample08'  # Synthetic eight-character project password.

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.repo, self.peer = base/'repo', base/'peer'
        self.repo.mkdir()
        self.peer.mkdir()
        profile = patch.dict(os.environ, {'LOCALAPPDATA': str(base/'profile'), 'XDG_DATA_HOME': str(base/'profile')})
        profile.start()
        self.addCleanup(profile.stop)
        admin = base/'admin'
        admin.write_text(secrets.token_urlsafe(48), encoding='ascii')
        self.app = create_app(base/'board.sqlite', base/'reports.sqlite', admin,
                              public_origin='https://class.example.test')

        class Quiet(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        httpd = make_server('127.0.0.1', 0, self.app, handler_class=Quiet)
        thread = threading.Thread(target=httpd.serve_forever, args=(.02,), daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(httpd.shutdown)
        self.server = 'http://127.0.0.1:' + str(httpd.server_port)

    def sign_in(self, root, names=(), passwords=(), *, force=False):
        return client.ensure_account(root, self.server, interactive=True, force=force,
            read_name=Mock(side_effect=names), read_password=Mock(side_effect=passwords), write=Mock())

    def team_count(self):
        with self.app.leaderboard.store.connection() as db:
            return db.execute('SELECT count(*) FROM enrollment_teams').fetchone()[0]

    def make_queue(self):
        files = []
        for kind in ('diagnostics', 'leaderboard'):
            path = self.repo/'output'/(kind + '_queue')/'sample.json'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'pending local fixture')
            files.append(path)
        return files

    def test_refused_first_password_then_corrected_team_confirms_queue_and_joins_peer(self):
        wrong, right = '20990011', '20990022'
        other = self.sign_in(self.peer, [wrong], ['wrong-team-pw'] * 2)
        peer2 = self.peer.parent/'peer2'
        peer2.mkdir()
        target = self.sign_in(peer2, [right], [self.password] * 2)
        with self.assertRaises(client.EnrollmentError) as first:
            self.sign_in(self.repo, [wrong], [self.password] * 2)
        self.assertEqual(first.exception.code, 'TEAM_SIGN_IN_REFUSED')
        _, token = client._read(self.repo, self.server)
        queue = self.make_queue()
        report = example()
        reporting.atomic_json(queue[0], report)
        queued_bytes = [p.read_bytes() for p in queue]
        before = client.credential_path(self.repo, self.server).read_bytes()
        with self.assertRaises(client.EnrollmentError) as cancelled:
            self.sign_in(self.repo, [right, ''], force=True)
        self.assertEqual(cancelled.exception.code, 'TEAM_NAME_NOT_CONFIRMED')
        self.assertEqual(client.credential_path(self.repo, self.server).read_bytes(), before)
        joined = self.sign_in(self.repo, [right] * 2, [self.password] * 2, force=True)
        self.assertEqual(joined['team_id'], target['team_id'])
        self.assertEqual(joined['token'], token)
        self.assertNotEqual(joined['token'], target['token'])
        self.assertEqual(self.sign_in(self.repo)['team_id'], target['team_id'])
        self.assertEqual([p.read_bytes() for p in queue], queued_bytes)
        self.assertEqual(self.team_count(), 2)
        self.assertEqual(client.request(self.server, '/api/diagnostics/own', token=target['token'])['reports'], [])
        self.assertEqual(reporting.retry(self.repo, joined)[0]['status'], 'uploaded')
        received = client.request(self.server, '/api/diagnostics/own', token=target['token'])['reports']
        self.assertEqual([(r['report_id'], r['team_id']) for r in received], [(report['report_id'], target['team_id'])])
        self.assertEqual(client.request(self.server, '/api/diagnostics/own', token=other['token'])['reports'], [])

    def test_old_unconfirmed_file_corrects_and_recovers_accepted_post_when_reply_lost(self):
        # Exact old on-disk shape, including its original device credential.
        original = device('L')
        client._save(self.repo, self.server, {'team_name': 'old-typo'}, original)
        real = client.request

        def drop_connect_reply(server, path, **options):
            value = real(server, path, **options)
            if path.endswith('/connect'):
                raise client.EnrollmentError('TEAM_SIGN_IN_UNAVAILABLE', 'simulated lost reply')
            return value

        with patch.object(client, 'request', side_effect=drop_connect_reply):
            with self.assertRaises(client.EnrollmentError):
                self.sign_in(self.repo, ['correct-team'] * 2, [self.password] * 2)
        path = client.credential_path(self.repo, self.server)
        before = path.read_bytes()
        with patch.object(client, 'request', side_effect=client.EnrollmentError('TEAM_SIGN_IN_UNAVAILABLE', 'offline')):
            with self.assertRaises(client.EnrollmentError) as offline:
                self.sign_in(self.repo, force=True)
        self.assertEqual(offline.exception.code, 'TEAM_SIGN_IN_UNAVAILABLE')
        self.assertEqual(before, path.read_bytes())
        recovered = self.sign_in(self.repo, force=True)
        self.assertEqual((recovered['team_name'], recovered['token']), ('correct-team', original))
        self.assertEqual(self.team_count(), 1)

    def test_first_post_already_registered_and_working_credential_survives_bad_reconnect(self):
        token = device('P')
        client._save(self.repo, self.server, {'team_name': 'agreed-team'}, token)
        accepted = client.request(self.server, '/api/enrollment/connect',
            payload={'team_name': 'agreed-team', 'password': self.password, 'device_token': token})
        recovered = self.sign_in(self.repo, force=True)  # No name/password prompt.
        self.assertEqual((recovered['team_id'], recovered['token']), (accepted['team_id'], token))
        path = client.credential_path(self.repo, self.server)
        before = path.read_bytes()
        with self.assertRaises(client.EnrollmentError):
            self.sign_in(self.repo, passwords=['mistyped'], force=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(client.request(self.server, '/api/enrollment/identity', token=token)['team_id'], recovered['team_id'])
        with patch.object(client, 'request', side_effect=AssertionError('cached sign-in must work offline')):
            self.assertEqual(self.sign_in(self.repo), recovered)

    def test_noninteractive_first_run_does_not_write_to_the_account_profile(self):
        with self.assertRaises(client.EnrollmentError) as required:
            client.ensure_account(self.repo, self.server, interactive=False)
        self.assertEqual(required.exception.code, 'TEAM_SIGN_IN_REQUIRED')
        self.assertFalse(client.credential_path(self.repo, self.server).parent.exists())

    def test_revoked_unconfirmed_credential_cannot_be_reassigned_even_with_consent(self):
        original = self.sign_in(self.repo, ['original-team'], [self.password] * 2)
        client._save(self.repo, self.server, {'team_name': 'original-team'}, original['token'])
        self.app.enrollment.revoke(original['token'])
        queue = self.make_queue()
        with self.assertRaises(client.EnrollmentError) as refused:
            self.sign_in(self.repo, ['different-team'] * 2, [self.password] * 2, force=True)
        self.assertEqual(refused.exception.code, 'TEAM_SIGN_IN_REFUSED')
        saved, token = client._read(self.repo, self.server)
        self.assertEqual((saved['team_name'], token), ('original-team', original['token']))
        self.assertFalse(client.stored_account(self.repo, self.server))
        self.assertEqual(self.team_count(), 1)
        self.assertEqual([p.read_bytes() for p in queue], [b'pending local fixture'] * 2)

    def test_bound_or_expected_identity_cannot_change_and_reconnects_to_original_team(self):
        original = self.sign_in(self.repo, ['original-team'], [self.password] * 2)
        path = client.credential_path(self.repo, self.server)
        for id_field in ('team_id', 'expected_team_id'):
            with self.subTest(id_field=id_field):
                client._save(self.repo, self.server,
                    {'team_name': 'original-team', id_field: original['team_id']}, original['token'])
                self.app.enrollment.revoke(original['token'])
                before = path.read_bytes()
                with self.assertRaises(client.EnrollmentError) as blocked:
                    client.connect(self.repo, self.server, 'other-team', self.password, new_device=True)
                self.assertEqual(blocked.exception.code, 'TEAM_ALREADY_BOUND')
                prompts = ['incorrect'] * (2 if id_field == 'expected_team_id' else 1)
                with self.assertRaises(client.EnrollmentError) as wrong:
                    self.sign_in(self.repo, passwords=prompts, force=True)
                self.assertEqual(wrong.exception.code, 'TEAM_SIGN_IN_REFUSED')
                self.assertEqual(path.read_bytes(), before)
                original = self.sign_in(self.repo, passwords=[self.password] * len(prompts), force=True)
                self.assertEqual(original['team_name'], 'original-team')
        self.assertEqual(self.team_count(), 1)

    def test_uncertain_identity_cannot_prompt_for_another_team_or_change_credential(self):
        client._save(self.repo, self.server, {'team_name': 'original-team'}, device('K'))
        path = client.credential_path(self.repo, self.server)
        before = path.read_bytes()
        for reply in (client.EnrollmentError('TEAM_SIGN_IN_REFUSED', 'generic HTTP refusal'),
                      client.EnrollmentError('TEAM_SIGN_IN_BUSY', 'temporary refusal'),
                      {'status': 'unknown', 'team_id': 'one', 'team_name': 'original-team'},
                      {'status': 'connected', 'team_id': 'two', 'team_name': 'different-team'}):
            with self.subTest(reply=type(reply).__name__), patch.object(client, 'request', side_effect=[reply]) as remote:
                with self.assertRaises(client.EnrollmentError):
                    self.sign_in(self.repo, force=True)
                self.assertEqual(remote.call_count, 1)
                self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
