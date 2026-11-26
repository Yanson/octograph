"""Property move-in / move-out handling, using an account shaped like a real house move (identifiers are fictional)."""
import copy
from datetime import datetime

import click
import pytest

from tests.octograph_harness import Harness, PRODUCTION_CONFIG
from tests.test_identifier_tags import tags_of

OLD_MPAN, OLD_MPRN = '1900000000001', '9000000001'
NEW_MPAN, NEW_MPRN = '1900000000002', '9000000002'
OLD_ELEC, OLD_GAS = '20L0000001', 'E6S00000000001'
NEW_ELEC, NEW_GAS = '22S0000001', 'E6A00000000001'
MOVE = '2026-11-30T00:00:00Z'


def _property(line_1, postcode, moved_in, moved_out, mpan, elec_serials, mprn, gas_serials):
    return {
        'moved_in_at': moved_in, 'moved_out_at': moved_out,
        'address_line_1': line_1, 'address_line_2': '', 'address_line_3': '', 'town': 'TOWNSHIRE', 'county': '',
        'postcode': postcode,
        'electricity_meter_points': [{
            'mpan': mpan, 'meters': [{'serial_number': s} for s in elec_serials], 'is_export': False,
            'agreements': [{'tariff_code': 'E-1R-AGILE-24-10-01-C', 'valid_from': '2021-02-05T00:00:00Z', 'valid_to': None}],
        }],
        'gas_meter_points': [{
            'mprn': mprn, 'meters': [{'serial_number': s} for s in gas_serials],
            'agreements': [{'tariff_code': 'G-1R-VAR-BB-23-04-01-A', 'valid_from': '2021-02-05T00:00:00Z', 'valid_to': None}],
        }],
    }


def move_account(move=MOVE, old_moved_in='2021-02-05T00:00:00Z', new_moved_in=None):
    return {'number': 'A-1234ABCD', 'properties': [
        _property('1 OLD STREET', 'AB1 2CD', old_moved_in, move, OLD_MPAN, ['S00E000001', OLD_ELEC], OLD_MPRN, [OLD_GAS, '000001']),
        _property('2 NEW ROAD', 'AB3 4EF', new_moved_in or move, None, NEW_MPAN, [NEW_ELEC], NEW_MPRN, [NEW_GAS]),
    ]}


CONFIG = PRODUCTION_CONFIG.replace('included_meters = 20L0000001,E6S00000000001',
                                   f'included_meters = {OLD_ELEC},{OLD_GAS},{NEW_ELEC},{NEW_GAS}') + f"""
[additional_tags:{NEW_MPAN}]
location = New Home

[additional_tags:{NEW_MPRN}]
location = New Home
"""


def at(s):
    return datetime.fromisoformat(s)


def latest_by_location(**latest):
    """Fake InfluxDB latest lookup: maps a location tag value in the query to the latest point (or None)."""
    def lookup(q):
        for location, t in latest.items():
            if f'r["location"] == "{location.replace("_", " ")}"' in q:
                return at(t) if t else None
        return None
    return lookup


def run(tmp_path, from_date, to_date, now, account=None, latest=None, config=CONFIG):
    h = Harness(tmp_path, config, account or move_account(), now=at(now), latest=latest or (lambda q: None))
    return h.run(from_date, to_date)


def times(h, measurement, serial=None, mp=None):
    return sorted(p.rsplit(' ', 1)[1] for p in h.points_for(measurement)
                  if (serial is None or tags_of(p).get('meter_serial_number') == serial)
                  and (mp is None or mp in (tags_of(p).get('electricity_mpan'), tags_of(p).get('gas_mprn'))))


def ts(s):
    return str(int(at(s).timestamp()))


def requests(h, serial):
    return [(r[3], r[4]) for r in h.consumption_requests if r[2] == serial]


def test_latest_run_before_move_collects_old_property_and_skips_new(tmp_path, capsys):
    latest = latest_by_location(Old_Home='2026-11-24T23:30:00+00:00')
    for to_date in ['yesterday', 'tomorrow']:
        h = run(tmp_path, 'latest', to_date, '2026-11-26T16:00:00Z', latest=latest)
        assert requests(h, OLD_ELEC)[0][0] == '2026-11-25T00:00:00Z'
        assert requests(h, OLD_GAS)[0][0] == '2026-11-25T00:00:00Z'
        assert requests(h, NEW_ELEC) == [] and requests(h, NEW_GAS) == []
        assert all(tags_of(p)['location'] == 'Old Home' for p in h.points)
    assert 'Skipping property at 2 NEW ROAD, AB3 4EF: not yet moved in (moves in at 2026-11-30 00:00:00+00:00)' in capsys.readouterr().out


def test_latest_run_day_before_move_with_tomorrow_skips_new_meter_points(tmp_path, capsys):
    latest = latest_by_location(Old_Home='2026-11-28T23:30:00+00:00')
    h = run(tmp_path, 'latest', 'tomorrow', '2026-11-29T16:00:00Z', latest=latest)
    assert requests(h, OLD_ELEC) == [('2026-11-29T00:00:00Z', '2026-11-29T23:30:00Z')]
    assert requests(h, NEW_ELEC) == [] and requests(h, NEW_GAS) == []
    assert h.points and all(tags_of(p)['location'] == 'Old Home' for p in h.points)
    out = capsys.readouterr().out
    assert f'No data found for {NEW_MPAN}: property moves in at 2026-11-30 00:00:00+00:00 - skipping for this run' in out


def test_backfill_to_yesterday_collects_old_property_up_to_yesterday(tmp_path):
    h = run(tmp_path, '2026-11-23', 'yesterday', '2026-11-26T16:00:00Z')
    assert requests(h, OLD_ELEC) == [('2026-11-23T00:00:00Z', '2026-11-25T23:30:00Z')]
    assert requests(h, OLD_GAS) == [('2026-11-23T00:00:00Z', '2026-11-25T23:30:00Z')]
    old = times(h, 'electricity_consumption', OLD_ELEC)
    assert (old[0], old[-1], len(old)) == (ts('2026-11-23T00:00:00Z'), ts('2026-11-25T23:30:00Z'), 3 * 48)
    assert requests(h, NEW_ELEC) == []


def test_range_spanning_move_splits_between_properties_with_own_tags(tmp_path):
    h = run(tmp_path, '2026-11-28', '2026-12-02', '2026-12-03T09:00:00Z')
    for serial in [OLD_ELEC, OLD_GAS]:
        assert requests(h, serial) == [('2026-11-28T00:00:00Z', '2026-11-29T23:30:00Z')]
    for serial in [NEW_ELEC, NEW_GAS]:
        assert requests(h, serial) == [('2026-11-30T00:00:00Z', '2026-12-02T23:30:00Z')]
    for measurement in ['electricity_consumption', 'gas_consumption', 'electricity_pricing', 'gas_pricing']:
        old = times(h, measurement, mp=OLD_MPAN if 'elec' in measurement else OLD_MPRN)
        new = times(h, measurement, mp=NEW_MPAN if 'elec' in measurement else NEW_MPRN)
        assert (old[0], old[-1], len(old)) == (ts('2026-11-28T00:00:00Z'), ts('2026-11-29T23:30:00Z'), 2 * 48), measurement
        assert (new[0], new[-1], len(new)) == (ts('2026-11-30T00:00:00Z'), ts('2026-12-02T23:30:00Z'), 3 * 48), measurement
    for p in h.points:
        t = tags_of(p)
        assert t['location'] == ('New Home' if t['property_postcode'] == 'AB3 4EF' else 'Old Home')


def test_first_latest_run_after_move_in_collects_new_property_from_move_in(tmp_path, capsys):
    latest = latest_by_location(Old_Home='2026-11-29T23:30:00+00:00', New_Home=None)
    h = run(tmp_path, 'latest', 'yesterday', '2026-12-01T10:00:00Z', latest=latest)
    for serial in [NEW_ELEC, NEW_GAS]:
        assert requests(h, serial) == [('2026-11-30T00:00:00Z', '2026-11-30T23:30:00Z')]
    assert requests(h, OLD_ELEC) == [] and requests(h, OLD_GAS) == []
    assert all(tags_of(p)['location'] == 'New Home' for p in h.points)
    out = capsys.readouterr().out
    assert f'No data found for {NEW_MPAN} in last 30 days - collecting from move-in date 2026-11-30 00:00:00+00:00' in out
    assert f'All data collected for {OLD_MPAN} up to move-out at 2026-11-30 00:00:00+00:00' in out


def test_latest_run_after_move_finishes_old_property_partial_day(tmp_path):
    latest = latest_by_location(Old_Home='2026-11-29T10:00:00+00:00', New_Home='2026-12-01T23:30:00+00:00')
    h = run(tmp_path, 'latest', 'yesterday', '2026-12-03T10:00:00Z', latest=latest)
    assert requests(h, OLD_ELEC) == [('2026-11-29T00:00:00Z', '2026-11-29T23:30:00Z')]
    assert requests(h, NEW_ELEC) == [('2026-12-02T00:00:00Z', '2026-12-02T23:30:00Z')]


def test_moved_in_over_30_days_ago_without_data_fails_only_that_meter_point(tmp_path, capsys):
    account = move_account(move='2026-10-01T00:00:00Z')
    latest = latest_by_location(Old_Home='2026-09-30T23:30:00+00:00', New_Home=None)
    with pytest.raises(click.ClickException, match='try back-filling') as e:
        run(tmp_path, 'latest', 'yesterday', '2026-11-26T16:00:00Z', account=account, latest=latest)
    assert e.value.message == ('Could not collect 2 meter point(s): '
                               f'{NEW_MPAN}: No data found for electricity_consumption in last 30 days - try back-filling; '
                               f'{NEW_MPRN}: No data found for gas_consumption in last 30 days - try back-filling')
    assert 'Skipping property at 1 OLD STREET, AB1 2CD: moved out (at 2026-10-01 00:00:00+00:00) before the requested range' in capsys.readouterr().out


def test_meter_point_without_data_does_not_stop_other_meter_points(tmp_path):
    account = move_account(move='2026-10-01T00:00:00Z')
    latest = lambda q: at('2026-11-24T23:30:00+00:00') if 'electricity' in q else None
    h = Harness(tmp_path, CONFIG, account, now=at('2026-11-26T16:00:00Z'), latest=latest)
    with pytest.raises(click.ClickException, match=f'Could not collect 1 meter point\\(s\\): {NEW_MPRN}: .*try back-filling'):
        h.run('latest', 'yesterday')
    assert requests(h, NEW_ELEC) == [('2026-11-25T00:00:00Z', '2026-11-25T23:30:00Z')]
    assert len(h.points_for('electricity_consumption')) == 48


def test_property_moved_out_before_range_is_skipped(tmp_path, capsys):
    h = run(tmp_path, '2026-12-01', '2026-12-01', '2026-12-03T09:00:00Z')
    assert requests(h, OLD_ELEC) == [] and requests(h, NEW_ELEC) != []
    assert 'Skipping property at 1 OLD STREET, AB1 2CD: moved out (at 2026-11-30 00:00:00+00:00) before the requested range' in capsys.readouterr().out


# --- edge cases: offsets, BST/GMT and timestamps that aren't on a boundary ---

def test_move_at_bst_local_midnight_with_offset(tmp_path):
    h = run(tmp_path, '2026-06-14', '2026-06-15', '2026-06-20T09:00:00Z', account=move_account('2026-06-15T00:00:00+01:00'))
    assert requests(h, OLD_ELEC) == [('2026-06-13T23:00:00Z', '2026-06-14T22:30:00Z')]
    assert requests(h, NEW_ELEC) == [('2026-06-14T23:00:00Z', '2026-06-15T22:30:00Z')]
    pricing = times(h, 'electricity_pricing', mp=NEW_MPAN)
    assert (pricing[0], pricing[-1]) == (ts('2026-06-14T23:00:00Z'), ts('2026-06-15T22:30:00Z'))


def test_move_in_utc_during_bst_is_not_a_local_day_boundary(tmp_path):
    h = run(tmp_path, '2026-06-14', '2026-06-15', '2026-06-20T09:00:00Z', account=move_account('2026-06-15T00:00:00Z'))
    assert requests(h, OLD_ELEC) == [('2026-06-13T23:00:00Z', '2026-06-14T23:30:00Z')]
    assert requests(h, NEW_ELEC) == [('2026-06-15T00:00:00Z', '2026-06-15T22:30:00Z')]


def test_move_part_way_through_an_interval_drops_the_partial_interval(tmp_path):
    h = run(tmp_path, '2026-06-14', '2026-06-15', '2026-06-20T09:00:00Z', account=move_account('2026-06-15T00:10:00Z'))
    assert requests(h, OLD_ELEC) == [('2026-06-13T23:00:00Z', '2026-06-14T23:30:00Z')]
    assert requests(h, NEW_ELEC) == [('2026-06-15T00:30:00Z', '2026-06-15T22:30:00Z')]
    assert ts('2026-06-15T00:00:00Z') not in times(h, 'electricity_pricing')


def test_move_across_bst_to_gmt_change(tmp_path):
    h = run(tmp_path, '2026-10-24', '2026-10-26', '2026-10-30T09:00:00Z', account=move_account('2026-10-25T00:00:00+01:00'))
    assert requests(h, OLD_ELEC) == [('2026-10-23T23:00:00Z', '2026-10-24T22:30:00Z')]
    assert requests(h, NEW_ELEC) == [('2026-10-24T23:00:00Z', '2026-10-26T23:30:00Z')]
    assert len(times(h, 'electricity_consumption', NEW_ELEC)) == 50 + 48


def test_latest_fallback_with_move_in_in_bst(tmp_path):
    latest = latest_by_location(Old_Home='2026-06-14T22:30:00+00:00', New_Home=None)
    h = run(tmp_path, 'latest', 'yesterday', '2026-06-16T10:00:00Z', account=move_account('2026-06-15T00:00:00+01:00'), latest=latest)
    assert requests(h, NEW_ELEC) == [('2026-06-14T23:00:00Z', '2026-06-15T22:30:00Z')]
    assert requests(h, OLD_ELEC) == []


@pytest.mark.parametrize('resolution, moved_in, expected', [
    (30, '2026-06-15T00:10:00Z', '2026-06-15T00:30:00+00:00'),
    (60, '2026-06-15T00:10:00Z', '2026-06-15T01:00:00+00:00'),
    (60, '2026-06-14T23:00:00Z', '2026-06-14T23:00:00+00:00'),
    (1440, '2026-06-15T00:00:00Z', '2026-06-15T23:00:00+00:00'),
    (1440, '2026-06-14T23:00:00Z', '2026-06-14T23:00:00+00:00'),
])
def test_window_alignment(tmp_path, resolution, moved_in, expected):
    h = Harness(tmp_path, CONFIG, move_account(), now=at('2026-06-20T00:00:00Z'))
    h.app._resolution_minutes = resolution
    assert h.app._align_to_interval(at(moved_in), up=True) == at(expected)


def test_properties_without_move_dates_are_always_active(tmp_path):
    account = move_account()
    for p in account['properties']:
        p.pop('moved_in_at'), p.pop('moved_out_at')
    h = run(tmp_path, '2026-11-28', '2026-11-28', '2026-12-03T09:00:00Z', account=account)
    assert {s for _, _, s, _, _ in h.consumption_requests} == {OLD_ELEC, OLD_GAS, NEW_ELEC, NEW_GAS}
