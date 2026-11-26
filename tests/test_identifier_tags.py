import re
from datetime import datetime

import click
import pytest

from tests.octograph_harness import (Harness, PRODUCTION_CONFIG, account_after_move, intelligent_dispatches,
                                     NEW_MPAN, NEW_SERIAL, OLD_MPAN, OLD_MPRN, o2i)

OctopusToInflux = o2i.OctopusToInflux

NEW_HOME_TAGS = f"""
[additional_tags:{NEW_MPAN}]
location = New Home

[additional_tags:{NEW_SERIAL}]
location = New Home
"""

# The new meter must be in included_meters for its consumption to be collected
MOVED_CONFIG = PRODUCTION_CONFIG.replace('included_meters = 20L0000001,E6S00000000001',
                                         f'included_meters = 20L0000001,E6S00000000001,{NEW_SERIAL}')


def parse_config(tmp_path, text):
    cfg = tmp_path / 'octograph.ini'
    cfg.write_text(text)
    return OctopusToInflux.parse_identifier_tags(str(cfg))


def tags_of(line: str) -> dict[str, str]:
    series = re.split(r'(?<!\\) ', line)[0]
    parts = re.split(r'(?<!\\),', series)[1:]
    return dict(p.replace('\\ ', ' ').replace('\\,', ',').split('=', 1) for p in parts)


def distinct_tags(points: list[str], *keys) -> set:
    return {tuple(tags_of(p).get(k) for k in keys) for p in points}


# --- config parsing ---

def test_parse_no_identifier_sections(tmp_path):
    assert parse_config(tmp_path, PRODUCTION_CONFIG) == {}


def test_parse_meter_point_section(tmp_path):
    assert parse_config(tmp_path, PRODUCTION_CONFIG + f'[additional_tags:{NEW_MPAN}]\nlocation = New Home\n') == \
           {NEW_MPAN: {'location': 'New Home'}}


def test_parse_meter_section_with_several_tags_preserving_case(tmp_path):
    text = PRODUCTION_CONFIG + f'[additional_tags: {NEW_SERIAL} ]\nlocation = New Home\nFloorArea = 90m2 %\n'
    assert parse_config(tmp_path, text) == {NEW_SERIAL: {'location': 'New Home', 'FloorArea': '90m2 %'}}


def test_parse_ignores_default_section_and_other_sections(tmp_path):
    text = '[DEFAULT]\nfoo = bar\n' + PRODUCTION_CONFIG + f'[additional_tags:{NEW_MPAN}]\nlocation = New Home\n'
    assert parse_config(tmp_path, text) == {NEW_MPAN: {'location': 'New Home'}}


@pytest.mark.parametrize('section', [
    '[additional_tags:X]\nmeter_serial_number = Y\n',
    '[additional_tags:X]\ntariff_code = Y\n',
    '[additional_tags:X]\nlocation =\n',
    '[additional_tags: ]\nlocation = Y\n',
])
def test_parse_rejects_invalid_sections(tmp_path, section):
    with pytest.raises(click.ClickException):
        parse_config(tmp_path, PRODUCTION_CONFIG + section)


def test_existing_options_unaffected_by_identifier_sections(tmp_path):
    h = Harness(tmp_path, MOVED_CONFIG + NEW_HOME_TAGS, account_after_move())
    assert h.app._additional_tags == {'location': 'Old Home'}
    assert h.app._gas_meter_types == {'E6S00000000001': 'SMETS2'}
    assert h.app._identifier_tags == {NEW_MPAN: {'location': 'New Home'}, NEW_SERIAL: {'location': 'New Home'}}


# --- tag resolution on written points ---

def run(tmp_path, extra, account=None, from_date='2026-11-24', to_date='2026-11-24', **kwargs):
    return Harness(tmp_path, MOVED_CONFIG + extra, account or account_after_move(old_moved_out=False), **kwargs).run(from_date, to_date)


def test_identifier_tags_override_global_for_meter_point_and_meter(tmp_path):
    h = run(tmp_path, NEW_HOME_TAGS)
    assert distinct_tags(h.points_for('electricity_pricing'), 'electricity_mpan', 'location') == {
        (OLD_MPAN, 'Old Home'), (NEW_MPAN, 'New Home')}
    assert distinct_tags(h.points_for('electricity_consumption'), 'meter_serial_number', 'location') == {
        ('20L0000001', 'Old Home'), (NEW_SERIAL, 'New Home')}
    assert distinct_tags(h.points_for('gas_consumption') + h.points_for('gas_pricing'), 'location') == {('Old Home',)}


def test_derived_tags_still_present_with_override(tmp_path):
    h = run(tmp_path, NEW_HOME_TAGS)
    tags = tags_of(next(p for p in h.points_for('electricity_consumption') if NEW_SERIAL in p))
    assert tags == {'account_number': 'A-1234ABCD', 'electricity_mpan': NEW_MPAN, 'location': 'New Home',
                    'meter_serial_number': NEW_SERIAL, 'property_address_line_1': '2 NEW ROAD',
                    'property_address_line_3': 'SOMEWHERE', 'property_postcode': 'AB3 4EF',
                    'property_town': 'TOWNSHIRE', 'tariff_code': 'E-1R-AGILE-24-10-01-C'}


def test_meter_inherits_meter_point_tags(tmp_path):
    h = run(tmp_path, f'[additional_tags:{NEW_MPAN}]\nlocation = New Home\nsite = new\n')
    assert distinct_tags(h.points_for('electricity_consumption'), 'meter_serial_number', 'location', 'site') == {
        ('20L0000001', 'Old Home', None), (NEW_SERIAL, 'New Home', 'new')}


def test_meter_tags_take_precedence_over_meter_point_tags(tmp_path):
    h = run(tmp_path, f'[additional_tags:{NEW_MPAN}]\nlocation = New Home\nsite = new\n'
                      f'[additional_tags:{NEW_SERIAL}]\nlocation = Garage\n')
    assert distinct_tags(h.points_for('electricity_pricing'), 'electricity_mpan', 'location', 'site') == {
        (OLD_MPAN, 'Old Home', None), (NEW_MPAN, 'New Home', 'new')}
    assert distinct_tags(h.points_for('electricity_consumption'), 'meter_serial_number', 'location', 'site') == {
        ('20L0000001', 'Old Home', None), (NEW_SERIAL, 'Garage', 'new')}


def test_gas_meter_point_and_meter_tags(tmp_path):
    h = run(tmp_path, f'[additional_tags:{OLD_MPRN}]\nlocation = Gas Cupboard\n'
                      f'[additional_tags:E6S00000000001]\nmeter = smart\n')
    assert distinct_tags(h.points_for('gas_pricing'), 'location', 'meter') == {('Gas Cupboard', None)}
    assert distinct_tags(h.points_for('gas_consumption'), 'location', 'meter') == {('Gas Cupboard', 'smart')}
    assert distinct_tags(h.points_for('electricity_consumption'), 'location') == {('Old Home',)}


def test_meter_tags_only_affect_that_meter(tmp_path):
    config = PRODUCTION_CONFIG.replace('included_meters = 20L0000001,E6S00000000001', 'included_meters = 20L0000001,S00E000001')
    h = Harness(tmp_path, config + '[additional_tags:S00E000001]\nlocation = Loft\n',
                account_after_move(old_moved_out=False)).run('2026-11-24', '2026-11-24')
    assert distinct_tags(h.points_for('electricity_consumption'), 'meter_serial_number', 'location') == {
        ('20L0000001', 'Old Home'), ('S00E000001', 'Loft')}
    assert distinct_tags(h.points_for('electricity_pricing'), 'electricity_mpan', 'location') == {
        (OLD_MPAN, 'Old Home'), (NEW_MPAN, 'Old Home')}


def test_intelligent_slots_use_meter_point_tags(tmp_path):
    h = run(tmp_path, f'intelligent_tariff_meter = {NEW_MPAN}\n' + NEW_HOME_TAGS, dispatches=intelligent_dispatches())
    assert distinct_tags(h.points_for('intelligent_slots'), 'mpan', 'location') == {(NEW_MPAN, 'New Home')}


# --- --from-date=latest ---

def test_latest_lookup_uses_resolved_tags(tmp_path):
    def latest(q):
        if 'New Home' in q:
            return datetime.fromisoformat('2026-11-24T10:00:00+00:00')
        if 'Old Home' in q and NEW_MPAN not in q:
            return datetime.fromisoformat('2026-11-20T10:00:00+00:00')
        return None

    h = run(tmp_path, NEW_HOME_TAGS, from_date='latest', to_date='2026-11-25', latest=latest)
    elec_queries = [q for q in h.queries if 'electricity_consumption' in q]
    assert len(elec_queries) == 2
    assert any(f'r["location"] == "New Home"' in q and NEW_MPAN in q for q in elec_queries)
    assert not any(f'r["location"] == "Old Home"' in q and NEW_MPAN in q for q in elec_queries)
    requests = {r[2]: r[3] for r in h.consumption_requests}
    assert requests[NEW_SERIAL] == '2026-11-24T00:00:00Z'
    assert requests['20L0000001'] == '2026-11-20T00:00:00Z'


def test_latest_lookup_searches_meter_series_with_own_tags(tmp_path):
    extra = f'[additional_tags:{NEW_MPAN}]\nlocation = New Home\n[additional_tags:{NEW_SERIAL}]\nlocation = Garage\n'

    def latest(q):
        return datetime.fromisoformat('2026-11-23T10:00:00+00:00') if 'Garage' in q else None

    h = run(tmp_path, extra, account=account_after_move(), from_date='latest', to_date='2026-11-25', latest=latest)
    assert len(h.queries) == 1 and 'r["location"] == "Garage"' in h.queries[0]
    assert h.consumption_requests[0][3] == '2026-11-23T00:00:00Z'


def test_latest_lookup_with_no_history_still_asks_for_back_fill(tmp_path):
    with pytest.raises(click.ClickException, match='try back-filling'):
        run(tmp_path, NEW_HOME_TAGS, account=account_after_move(), from_date='latest', to_date='2026-11-25', latest=lambda q: None)
