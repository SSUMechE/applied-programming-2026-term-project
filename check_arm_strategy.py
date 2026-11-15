"""Run the existing small interface checks against one chosen student module.

This is a CPU-only supplied check, not map planning or a physical certificate.
It reuses the three established tests and changes only the selected strategy
and the arm ID in the toy request context. No solution is provided.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib
import inspect
from pathlib import Path
import unittest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy', required=True, help='Your module, for example student.arm_1_strategy')
    parser.add_argument('--arm', required=True, choices=('arm_0', 'arm_1', 'arm_2'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    if not args.strategy.startswith('student.') or not all(p.isidentifier() for p in args.strategy.split('.')):
        parser.error('Choose a Python module under student/')
    try:
        strategy = importlib.import_module(args.strategy)
        source = Path(inspect.getsourcefile(strategy)).resolve(strict=True)
        if not source.is_relative_to(root / 'student') or not callable(getattr(strategy, 'plan_path', None)):
            raise ValueError('Use your student module with plan_path(request, oracle, *, kinematics=None, context=None)')
    except (ValueError, OSError, TypeError, ImportError) as exc:
        parser.error(str(exc))
    from tests import test_student_strategy as supplied
    original = supplied.joint_strategy
    supplied.joint_strategy = strategy

    class SelectedArmChecks(supplied.StudentStrategyChecks):
        def context(self, start, goal):
            return replace(super().context(start, goal), active_arm_id=args.arm)

    try:
        print(f'Checking {args.strategy}: three supplied interface tests for {args.arm}. No Genesis or map simulation.', flush=True)
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SelectedArmChecks))
        return 0 if result.wasSuccessful() else 1
    finally:
        supplied.joint_strategy = original


if __name__ == '__main__':
    raise SystemExit(main())
