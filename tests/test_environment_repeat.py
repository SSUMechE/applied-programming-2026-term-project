"""Repeated checks report today's result without replacing yesterday's receipt."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import check_environment as check
from diagnostics.client import PACKAGE_VERSION, make_report


class RepeatEnvironmentChecks(unittest.TestCase):
    def test_exclusive_receipt_and_current_pass_or_failure(self):
        with TemporaryDirectory() as temp:
            path = Path(temp, 'environment.json')
            for i, passed in enumerate((True, False, True)):
                status = 'ENVIRONMENT_METADATA_PASS' if passed else 'ENVIRONMENT_METADATA_MISMATCH'
                current = dict(passed=passed, status=status)
                out, err = io.StringIO(), io.StringIO()
                with patch.object(check, 'metadata_check', return_value=current), redirect_stdout(out), redirect_stderr(err):
                    result = check.main(['--output', str(path), '--json'])
                self.assertEqual(result, 0 if passed else 1)
                self.assertEqual(json.loads(out.getvalue())['status'], status)
                self.assertEqual('kept' in err.getvalue(), i > 0)
                if i == 0:
                    original = path.read_bytes()
                self.assertEqual(path.read_bytes(), original)

    def test_failed_screen_check_reports_current_package_not_old_receipt(self):
        with TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), 'check', ['--cuda'], 1, 1,
                                 'ENVIRONMENT_METADATA_MISMATCH\nMismatch: mujoco expected x, found y\n',
                                 {}, dict(package_version=PACKAGE_VERSION, git_commit=None,
                                          evaluation_sources_sha256=None, student_snapshot_sha256=None,
                                          dirty=None))['failure']
            self.assertEqual(report['code'], 'ENVIRONMENT_METADATA_MISMATCH')
            self.assertIn('mujoco expected x, found y', report['message'])
