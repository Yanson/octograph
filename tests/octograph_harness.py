"""Test harness that runs OctopusToInflux end-to-end against fake Octopus and InfluxDB clients."""
import copy
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# octopus_to_influxdb imports some sibling modules without the `app.` prefix (it is run as a script)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app')))

from app import octopus_to_influxdb as o2i  # noqa: E402
from app.date_utils import DateUtils  # noqa: E402
from intelligent_dispatches import IntelligentDispatchItem, IntelligentDispatches  # noqa: E402

PRODUCTION_CONFIG = """\
[influxdb]
url = http://influxdb:8086
token_env_var = OCTOGRAPH_INFLUXDB_TOKEN
org = monitoring
bucket = energy-metrics
included_tags = account_number,property_address_line_1,property_address_line_2,property_address_line_3,property_town,property_postcode,electricity_mpan,gas_mprn,meter_serial_number,agreement_tariff_code
additional_tags = location=Old Home

[octopus]
account_number = A-1234ABCD
api_key_env_var = OCTOGRAPH_OCTOPUS_API_KEY
included_meters = 20L0000001,E6S00000000001
timezone = Europe/London
unit_rate_low_start = 1
unit_rate_low_end = 8
enable_cache = false
gas_meter_types = E6S00000000001=SMETS2
"""

OLD_MPAN = '1900000000001'
OLD_MPRN = '9000000001'
NEW_MPAN = '1900000000002'
NEW_SERIAL = '24E0000001'  # placeholder for the new property's meter serial number

OLD_PROPERTY = {
    'id': 1000001,
    'moved_in_at': '2021-02-05T00:00:00Z',
    'moved_out_at': None,
    'address_line_1': '1',
    'address_line_2': 'OLD STREET',
    'address_line_3': 'SOMEWHERE',
    'town': 'TOWNSHIRE',
    'county': '',
    'postcode': 'AB1 2CD',
    'electricity_meter_points': [{
        'mpan': OLD_MPAN,
        'meters': [{'serial_number': 'S00E000001'}, {'serial_number': '20L0000001'}],
        'agreements': [
            {'tariff_code': 'E-1R-AGILE-FLEX-BB-23-02-08-A', 'valid_from': '2023-05-09T00:00:00+01:00', 'valid_to': None},
        ],
        'is_export': False,
    }],
    'gas_meter_points': [{
        'mprn': OLD_MPRN,
        'meters': [{'serial_number': '000001'}, {'serial_number': 'E6S00000000001'}],
        'agreements': [
            {'tariff_code': 'G-1R-VAR-BB-23-04-01-A', 'valid_from': '2023-04-01T00:00:00+01:00', 'valid_to': None},
        ],
    }],
}

NEW_PROPERTY = {
    'id': 7000001,
    'moved_in_at': '2026-10-01T00:00:00Z',
    'moved_out_at': None,
    'address_line_1': '2 NEW ROAD',
    'address_line_2': '',
    'address_line_3': 'SOMEWHERE',
    'town': 'TOWNSHIRE',
    'county': '',
    'postcode': 'AB3 4EF',
    'electricity_meter_points': [{
        'mpan': NEW_MPAN,
        'meters': [{'serial_number': NEW_SERIAL}],
        'agreements': [
            {'tariff_code': 'E-1R-AGILE-24-10-01-C', 'valid_from': '2026-10-01T00:00:00Z', 'valid_to': None},
        ],
        'is_export': False,
    }],
    'gas_meter_points': [],
}


def account_before_move():
    return {'number': 'A-1234ABCD', 'properties': [copy.deepcopy(OLD_PROPERTY)]}


def account_without_move_dates():
    p = copy.deepcopy(OLD_PROPERTY)
    del p['moved_in_at'], p['moved_out_at']
    return {'number': 'A-1234ABCD', 'properties': [p]}


def account_after_move(old_moved_out=True):
    old = copy.deepcopy(OLD_PROPERTY)
    if old_moved_out:
        old['moved_out_at'] = '2026-10-01T00:00:00Z'
    return {'number': 'A-1234ABCD', 'properties': [old, copy.deepcopy(NEW_PROPERTY)]}


def _fake_pricing(tariff_code: str, from_date: str, to_date: str):
    base = float(len(tariff_code))
    row = {'valid_from': None, 'valid_to': None, 'payment_method': None}
    return {
        'standard_unit_rates': [row | {'value_exc_vat': base, 'value_inc_vat': base * 1.05}],
        'standing_charges': [row | {'value_exc_vat': base * 2, 'value_inc_vat': base * 2.1}],
    }


def _fake_consumption(serial: str, from_date: str, to_date: str):
    t = datetime.fromisoformat(from_date)
    last = datetime.fromisoformat(to_date)
    rows = []
    i = 0
    while t <= last:
        end = t + timedelta(minutes=30)
        rows.append({'interval_start': DateUtils.iso8601(t), 'interval_end': DateUtils.iso8601(end),
                     'consumption': round((i % 7 + 1) * 0.1 + len(serial) * 0.01, 3)})
        t = end
        i += 1
    return rows


NOW = datetime.fromisoformat('2026-11-26T16:00:00+00:00')


class Harness:
    def __init__(self, tmp_path, config_text: str, account: dict, latest=None, dispatches=None, now: datetime = NOW):
        """`latest` is a callable taking a Flux query and returning the latest datetime (or None)."""
        self.points: list[str] = []
        self.queries: list[str] = []
        self.consumption_requests: list[tuple] = []
        self._latest = latest or (lambda q: datetime.fromisoformat('2026-11-24T10:00:00+00:00'))

        cfg = tmp_path / 'octograph.ini'
        cfg.write_text(config_text)
        env = {'OCTOGRAPH_INFLUXDB_TOKEN': 'token', 'OCTOGRAPH_OCTOPUS_API_KEY': 'key'}
        with patch.object(o2i, 'InfluxDBClient') as influx_cls, \
                patch.object(o2i, 'OctopusGraphQlApiClient') as ql_cls, \
                patch.dict(os.environ, env):
            influx = influx_cls.return_value
            influx.write_api.return_value.write.side_effect = self._write
            influx.query_api.return_value.query.side_effect = self._query
            ql_cls.return_value.async_get_intelligent_dispatches = AsyncMock(
                return_value=dispatches or IntelligentDispatches([], []))
            self.app = o2i.OctopusToInflux(str(cfg))
        self.app._now = lambda: now

        api = self.app._octopus_api
        api.retrieve_account = lambda number: copy.deepcopy(account)
        api.retrieve_tariff_pricing = _fake_pricing
        api.retrieve_electricity_consumption = self._elec
        api.retrieve_gas_consumption = self._gas

    def _elec(self, mpan, serial, f, t):
        self.consumption_requests.append(('electricity', mpan, serial, f, t))
        return _fake_consumption(serial, f, t)

    def _gas(self, mprn, serial, f, t):
        self.consumption_requests.append(('gas', mprn, serial, f, t))
        return _fake_consumption(serial, f, t)

    def _write(self, bucket, record):
        self.points.extend(p.to_line_protocol() for p in record)

    def _query(self, q):
        self.queries.append(q)
        if 'intelligent_slots' in q:
            return []
        latest = self._latest(q)
        if latest is None:
            return []
        return [SimpleNamespace(records=[SimpleNamespace(values={'_time': latest})])]

    def run(self, from_date: str, to_date: str):
        self.app.collect(from_date, to_date)
        return self

    def output(self):
        return {'queries': self.queries, 'consumption_requests': [list(r) for r in self.consumption_requests],
                'points': self.points}

    def points_for(self, measurement: str):
        return [p for p in self.points if p.startswith(measurement + ',')]


def intelligent_dispatches():
    start = datetime.fromisoformat('2026-11-24T23:30:00+00:00')
    return IntelligentDispatches([], [IntelligentDispatchItem(start, start + timedelta(hours=1), 0, '', '')])
