"""Regression tests: configs without per-identifier tag sections must produce exactly the same output as before.

The golden file was captured from the code *before* per-identifier tags were introduced.
Regenerate (only for an intentional behaviour change) with: OCTOGRAPH_UPDATE_GOLDEN=1 pytest tests/test_regression_existing_config.py
"""
import json
import os
from pathlib import Path

import pytest

from tests.octograph_harness import (Harness, PRODUCTION_CONFIG, account_after_move, account_before_move,
                                     intelligent_dispatches, OLD_MPAN)

GOLDEN = Path(__file__).parent / 'fixtures' / 'regression_existing_config.json'

INTELLIGENT_CONFIG = PRODUCTION_CONFIG + f'intelligent_tariff_meter = {OLD_MPAN}\n'

SCENARIOS = {
    'before_move_fixed_dates': (PRODUCTION_CONFIG, account_before_move, '2026-11-24', '2026-11-24'),
    'before_move_latest': (PRODUCTION_CONFIG, account_before_move, 'latest', '2026-11-25'),
    'after_move_fixed_dates': (PRODUCTION_CONFIG, account_after_move, '2026-11-24', '2026-11-24'),
    'after_move_latest': (PRODUCTION_CONFIG, account_after_move, 'latest', '2026-11-25'),
    'after_move_old_property_not_moved_out_latest': (PRODUCTION_CONFIG, lambda: account_after_move(old_moved_out=False), 'latest', '2026-11-25'),
    'intelligent_fixed_dates': (INTELLIGENT_CONFIG, account_before_move, '2026-11-24', '2026-11-24'),
}


def _run(tmp_path, name):
    config, account, from_date, to_date = SCENARIOS[name]
    dispatches = intelligent_dispatches() if 'intelligent' in name else None
    return Harness(tmp_path, config, account(), dispatches=dispatches).run(from_date, to_date).output()


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_existing_config_output_unchanged(tmp_path, name):
    actual = _run(tmp_path, name)
    if os.getenv('OCTOGRAPH_UPDATE_GOLDEN'):
        golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        golden[name] = actual
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + '\n')
    expected = json.loads(GOLDEN.read_text())[name]
    assert actual['queries'] == expected['queries']
    assert actual['consumption_requests'] == expected['consumption_requests']
    assert actual['points'] == expected['points']
