#!/usr/bin/env python
import os
import asyncio
from configparser import ConfigParser
from datetime import datetime, date, timedelta

import click
import pytz
from influxdb_client import InfluxDBClient
from influxdb_client.client.write.point import Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.domain.write_precision import WritePrecision
from octopus_graphql_api_client import OctopusGraphQlApiClient
from intelligent_dispatches import IntelligentDispatchItem

from app.date_utils import DateUtils
from app.eco_unit_normaliser import EcoUnitNormaliser
from app.octopus_api_client import OctopusApiClient
from app.series_maker import SeriesMaker


class OctopusToInflux:

    def __init__(self, config_file=None):
        config = ConfigParser()
        config.read(config_file)

        self._account_number = config.get('octopus', 'account_number')
        if not self._account_number:
            raise click.ClickException('No Octopus account number set')

        self._timezone = pytz.timezone(config.get('octopus', 'timezone', fallback='Europe/London'))
        self._payment_method = config.get('octopus', 'payment_method', fallback='DIRECT_DEBIT')
        low_start = config.getint('octopus', 'unit_rate_low_start', fallback=1)
        low_end = config.getint('octopus', 'unit_rate_low_end', fallback=8)
        self._eco_unit_normaliser = EcoUnitNormaliser(self._timezone, low_start, low_end)

        gas_meter_types = config.get('octopus', 'gas_meter_types', fallback=None)
        self._gas_meter_types = dict(item.split('=') for item in gas_meter_types.split(',')) if gas_meter_types else {}
        g_vcf = config.getfloat('octopus', 'volume_correction_factor', fallback=1.02264)
        g_cv = config.getfloat('octopus', 'calorific_value', fallback=38.8)
        self._smets2_conversion_multiplier = (g_vcf * g_cv) / 3.6

        self._resolution_minutes = config.getint('octopus', 'resolution_minutes', fallback=30)
        self._series_maker = SeriesMaker(self._resolution_minutes)

        self._octopus_api = OctopusApiClient(
            config.get('octopus', 'api_prefix', fallback='https://api.octopus.energy/v1'),
            os.getenv(config.get('octopus', 'api_key_env_var', fallback='OCTOGRAPH_OCTOPUS_API_KEY')),
            self._resolution_minutes,
            config.get('octopus', 'cache_dir', fallback='/tmp/octopus_api_cache') if config.getboolean('octopus', 'enable_cache', fallback=False) else None
        )

        self._intelligent_tariff_meter = config.get('octopus', 'intelligent_tariff_meter', fallback=None)
        self._intelligent_low_rate_hour = config.getint('octopus', 'intelligent_low_rate_hour', fallback=3)

        if self._intelligent_tariff_meter is not None:
            self._octopus_ql_api = OctopusGraphQlApiClient(
                os.getenv(config.get('octopus', 'api_key_env_var', fallback='OCTOGRAPH_OCTOPUS_API_KEY'))
            )

        self._influx_bucket = config.get('influxdb', 'bucket', fallback='primary')
        influx_client = InfluxDBClient(
            url=config.get('influxdb', 'url', fallback='http://localhost:8086'),
            token=os.getenv(config.get('influxdb', 'token_env_var', fallback='OCTOGRAPH_INFLUXDB_TOKEN')),
            org=config.get('influxdb', 'org', fallback='primary')
        )
        self._influx_write = influx_client.write_api(write_options=SYNCHRONOUS)
        self._influx_query = influx_client.query_api()

        included_meters_str = config.get('octopus', 'included_meters', fallback=None)
        self._included_meters = included_meters_str.split(',') if included_meters_str else None

        included_tags_str = config.get('influxdb', 'included_tags', fallback=None)
        self._included_tags = included_tags_str.split(',') if included_tags_str else []

        additional_tags_str = config.get('influxdb', 'additional_tags', fallback=None)
        self._additional_tags = dict(item.split('=') for item in additional_tags_str.split(',')) if additional_tags_str else {}

    def find_latest_date(self, measurement: str, field: str, tags: dict[str, str]):
        tf = ' '.join([f' and r["{k}"] == "{v}"' for k, v in tags.items()])
        f = f'r["_measurement"] == "{measurement}" and r._field == "{field}"{tf})'
        q = f'from(bucket: "{self._influx_bucket}") |> range(start: -30d) |> filter(fn: (r) => {f} |> last()'
        r = self._influx_query.query(q)
        l = None
        for table in r:
            for record in table.records:
                l = record.values['_time'] if l is None or record.values['_time'] > l else l
        if not l:
            raise click.ClickException(f'No data found for {measurement} in last 30 days - try back-filling')
        return l

    def _find_intelligent_slots(self, collect_from: datetime, collect_to: datetime, tags: dict[str, str]):
        field = 'intelligent_slot'
        measurement = 'intelligent_slots'

        intelligent_slots = []

        tf = ' '.join([f' and r["{k}"] == "{v}"' for k, v in tags.items()])
        f = f'r["_measurement"] == "{measurement}" and r["account_number"] == "{self._account_number}" and r["_field"] == "{field}"{tf})'
        q = f'from(bucket: "{self._influx_bucket}") |> range(start: {DateUtils.iso8601(collect_from)}, stop: {DateUtils.iso8601(collect_to)}) |> filter(fn: (r) => {f}'
        r = self._influx_query.query(q)
        for table in r:
            for record in table.records:
                intelligent_slot = IntelligentDispatchItem(
                    record.values['start_time'],
                    record.values['end_time'],
                    0,
                    "",
                    record.values['location'],
                )
                intelligent_slots.append(intelligent_slot)
        for slot in intelligent_slots:
            print("Found intelligent slot: " + str(slot.start) + " to " + str(slot.end))
        return intelligent_slots

    def collect(self, from_str: str, to_str: str):
        click.echo(f'Collecting data between {from_str} and {to_str}')
        if from_str == 'yesterday':
            from_str = DateUtils.yesterday_date_string(self._timezone)
        if to_str == 'yesterday':
            to_str = DateUtils.yesterday_date_string(self._timezone)
        collect_from = None if from_str == 'latest' else DateUtils.local_midnight(date.fromisoformat(from_str), self._timezone)

        if to_str == 'tomorrow':
            to_str = DateUtils.tomorrow_date_string(self._timezone)
        collect_to = DateUtils.local_midnight(date.fromisoformat(to_str) + timedelta(days=1), self._timezone)

        account = self._octopus_api.retrieve_account(self._account_number)
        tags = self._additional_tags.copy()
        if 'account_number' in self._included_tags:
            tags['account_number'] = account['number']

        if self._intelligent_tariff_meter is not None:
            click.echo(f'Collecting recent intelligent slots')
            self._process_intelligent_slots(self._intelligent_tariff_meter, tags)

        for p in account['properties']:
            if 'moved_out_at' in p and p['moved_out_at']:
                click.echo(f"Skipping property at {p['address_line_1']}, {p['postcode']} due to move-out date being set.")
            else:
                self._process_property(p, collect_from, collect_to, tags)

    def _process_property(self, p, collect_from: datetime, collect_to: datetime, base_tags: dict[str, str]):
        tags = base_tags.copy()
        click.echo(f'Processing property: {p["address_line_1"]}, {p["postcode"]}')
        for field in ['address_line_1', 'address_line_2', 'address_line_3', 'town', 'postcode']:
            if f'property_{field}' in self._included_tags and p[field] != '':
                tags[f'property_{field}'] = p[field]
        if len(p['electricity_meter_points']) == 0:
            click.echo('No electricity meter points found in property')
        for emp in p['electricity_meter_points']:
            self._process_emp(emp, collect_from, collect_to, tags)
        if len(p['gas_meter_points']) == 0:
            click.echo('No gas meter points found in property')
        for emp in p['gas_meter_points']:
            self._process_gmp(emp, collect_from, collect_to, tags)

    def _process_emp(self, emp, collect_from: datetime, collect_to: datetime, base_tags: dict[str, str]):
        tags = base_tags.copy()
        click.echo(f'Processing electricity meter point: {emp["mpan"]}')
        if 'electricity_mpan' in self._included_tags:
            tags['electricity_mpan'] = emp["mpan"]

        if not collect_from:
            collect_from = self.find_latest_date('electricity_consumption', 'usage_kwh', tags) + timedelta(minutes=self._resolution_minutes)
        if DateUtils.minutes_between(collect_from, collect_to) < self._resolution_minutes:
            click.echo('No new data to collect for EMP')
            return

        pricing_dict = self._get_pricing(emp['agreements'], collect_from, collect_to)
        standard_unit_rows = []
        if 'day_unit_rates' in pricing_dict and 'night_unit_rates' in pricing_dict:
            standard_unit_rows += self._eco_unit_normaliser.normalise(pricing_dict['day_unit_rates'], pricing_dict['night_unit_rates'],
                                                                      DateUtils.local_date(collect_from, self._timezone),
                                                                      DateUtils.local_date(collect_to, self._timezone))
        if 'standard_unit_rates' in pricing_dict:
            standard_unit_rows += pricing_dict['standard_unit_rates']
            pass
        standard_unit_rates = self._series_maker.make_series(standard_unit_rows)
        if 'standing_charges' in pricing_dict:
            standing_charges = self._series_maker.make_series(pricing_dict['standing_charges'])
        else:
            click.echo(f'Could not find pricing for mpan: {emp["mpan"]}')
            standing_charges = {}

        if emp['mpan'] == self._intelligent_tariff_meter and not emp['is_export']:
            click.echo(f'Checking for intelligent dispatch slots for electricity meter {emp["mpan"]}')
            intelligent_slots = self._find_intelligent_slots(collect_from, collect_to, {'mpan': emp['mpan']})
            standard_unit_rates = self.apply_intelligent_slots(standard_unit_rates, intelligent_slots, self._intelligent_low_rate_hour)
        self._store_emp_pricing(standard_unit_rates, standing_charges, tags)

        for em in emp['meters']:
            if self._included_meters and em['serial_number'] not in self._included_meters:
                click.echo(f'Skipping electricity meter {em["serial_number"]} as it is not in octopus.included_meters')
            else:
                self._process_em(emp['mpan'], em, emp['is_export'], collect_from, collect_to, standard_unit_rates, standing_charges, tags)

    def _process_em(self, mpan: str, em, is_export: bool, collect_from: datetime, collect_to: datetime, standard_unit_rates, standing_charges, base_tags: dict[str, str]):
        tags = base_tags.copy()
        click.echo(f'Processing electricity meter: {em["serial_number"]}')
        if 'meter_serial_number' in self._included_tags:
            tags['meter_serial_number'] = em['serial_number']
        last_interval_start = DateUtils.iso8601(collect_to - timedelta(minutes=self._resolution_minutes))
        data = self._octopus_api.retrieve_electricity_consumption(mpan, em['serial_number'], DateUtils.iso8601(collect_from), last_interval_start)

        consumption = self._series_maker.make_series(data)
        self._store_em_consumption(is_export, consumption, standard_unit_rates, standing_charges, tags)

    def _store_em_consumption(self, is_export: bool, consumption: dict, standard_unit_rates, standing_charges, base_tags: [str, str]):
        self._store_consumption('electricity_consumption', consumption, standard_unit_rates, is_export, standing_charges, base_tags)

    def _store_gm_consumption(self, consumption: dict, standard_unit_rates, standing_charges, base_tags: [str, str]):
        self._store_consumption('gas_consumption', consumption, standard_unit_rates, False, standing_charges, base_tags)

    def _store_consumption(self, measurement: str, consumption: dict, standard_unit_rates, export_meter: bool, standing_charges, base_tags: [str, str]):
        points = []
        net_modifier = 1.0
        if export_meter:
          net_modifier = -1.0
        for t, c in consumption.items():
            usage_cost_exc_vat_pence = standard_unit_rates[t]['value_exc_vat'] * c['consumption'] * net_modifier
            usage_cost_inc_vat_pence = standard_unit_rates[t]['value_inc_vat'] * c['consumption'] * net_modifier
            standing_charge_cost_exc_vat_pence = standing_charges[t]['value_exc_vat'] / (60 * 24 / self._resolution_minutes)
            standing_charge_cost_inc_vat_pence = standing_charges[t]['value_inc_vat'] / (60 * 24 / self._resolution_minutes)
            points.append(Point.from_dict({
                'measurement': measurement,
                'time': t,
                'fields': {
                    'usage_kwh': c['consumption'] * net_modifier,
                    'usage_cost_exc_vat_pence': usage_cost_exc_vat_pence,
                    'usage_cost_inc_vat_pence': usage_cost_inc_vat_pence,
                    'standing_charge_cost_exc_vat_pence': standing_charge_cost_exc_vat_pence,
                    'standing_charge_cost_inc_vat_pence': standing_charge_cost_inc_vat_pence,
                    'total_cost_exc_vat_pence': usage_cost_exc_vat_pence + standing_charge_cost_exc_vat_pence,
                    'total_cost_inc_vat_pence': usage_cost_inc_vat_pence + standing_charge_cost_inc_vat_pence,
                },
                'tags': {'tariff_code': standard_unit_rates[t]['tariff_code']} | base_tags,
            }, write_precision=WritePrecision.S))
        click.echo(f'Storing {len(points)} points')
        self._influx_write.write(self._influx_bucket, record=points)

    def apply_intelligent_slots(self, standard_unit_rates, intelligent_slots, intelligent_low_rate_hour):
        for t in standard_unit_rates.keys():
            for intelligent_slot in intelligent_slots:
                if intelligent_slot.start == t:
                    # Use known off-peak time to find correct off-peak rate
                    intelligent_low_time = DateUtils.iso8601(datetime.fromisoformat(t).replace(hour=intelligent_low_rate_hour, minute=0))
                    # Check we actually have data for this day, otherwise use next day (e.g. collecting from midnight @ UTC+1 means collecting 23:00 from day before @ UTC)
                    if intelligent_low_time not in standard_unit_rates:
                      intelligent_low_time = DateUtils.iso8601(datetime.fromisoformat(t).replace(hour=intelligent_low_rate_hour, minute=0) + timedelta(days=1))
                    if standard_unit_rates[t]['value_inc_vat'] != standard_unit_rates[intelligent_low_time]['value_inc_vat'] or standard_unit_rates[t]['value_exc_vat'] != standard_unit_rates[intelligent_low_time]['value_exc_vat']:
                      print("Applying off-peak rate (" + str(standard_unit_rates[intelligent_low_time]['value_inc_vat']) + " p/kWh) to intelligent slot at " + str(t))
                      standard_unit_rates[t]['value_inc_vat'] = standard_unit_rates[intelligent_low_time]['value_inc_vat']
                      standard_unit_rates[t]['value_exc_vat'] = standard_unit_rates[intelligent_low_time]['value_exc_vat']
                    intelligent_slots.remove(intelligent_slot)
                    continue

        return standard_unit_rates

    def _process_gmp(self, gmp, collect_from: datetime, collect_to: datetime, base_tags: dict[str, str]):
        tags = base_tags.copy()
        click.echo(f'Processing gas meter point: {gmp["mprn"]}')
        if 'gas_mprn' in self._included_tags:
            tags['gas_mprn'] = gmp["mprn"]

        if not collect_from:
            collect_from = self.find_latest_date('gas_consumption', 'usage_kwh', tags) + timedelta(minutes=self._resolution_minutes)
        if DateUtils.minutes_between(collect_from, collect_to) < self._resolution_minutes:
            click.echo('No new data to collect for GMP')
            return

        pricing_dict = self._get_pricing(gmp['agreements'], collect_from, collect_to)
        standard_unit_rates = self._series_maker.make_series(pricing_dict['standard_unit_rates'])
        standing_charges = self._series_maker.make_series(pricing_dict['standing_charges'])
        self._store_gmp_pricing(standard_unit_rates, standing_charges, tags)

        for gm in gmp['meters']:
            if self._included_meters and gm['serial_number'] not in self._included_meters:
                click.echo(f'Skipping gas meter {gm["serial_number"]} as it is not in octopus.included_meters')
            else:
                self._process_gm(gmp["mprn"], gm, collect_from, collect_to, standard_unit_rates, standing_charges, tags)

    def _process_gm(self, mprn: str, gm, collect_from: datetime, collect_to: datetime, standard_unit_rates, standing_charges, base_tags: dict[str, str]):
        tags = base_tags.copy()
        click.echo(f'Processing gas meter: {gm["serial_number"]}')
        if 'meter_serial_number' in self._included_tags:
            tags['meter_serial_number'] = gm["serial_number"]
        last_interval_start = DateUtils.iso8601(collect_to - timedelta(minutes=self._resolution_minutes))
        data = self._octopus_api.retrieve_gas_consumption(mprn, gm['serial_number'], DateUtils.iso8601(collect_from), last_interval_start)
        if self._gas_meter_types.get(gm['serial_number'], 'SMETS1') == 'SMETS2':
            for r in data:
                r['consumption'] *= self._smets2_conversion_multiplier
        consumption = self._series_maker.make_series(data)
        self._store_gm_consumption(consumption, standard_unit_rates, standing_charges, tags)

    def _get_pricing(self, agreements, collect_from: datetime, collect_to: datetime):
        f = collect_from
        t = collect_to
        pricing = {}
        for a in sorted(agreements, key=lambda x: datetime.fromisoformat(x['valid_from'])):
            agreement_valid_from = datetime.fromisoformat(a['valid_from'])
            agreement_valid_to = datetime.fromisoformat(a['valid_to']) if a['valid_to'] else collect_to
            if agreement_valid_from <= f < agreement_valid_to:
                if agreement_valid_to < t:
                    t = agreement_valid_to
                agreement_rates = self._octopus_api.retrieve_tariff_pricing(a['tariff_code'], DateUtils.iso8601(f), DateUtils.iso8601(t))
                for component, results in agreement_rates.items():
                    pricing_rows = []
                    for r in [x for x in results if not x['payment_method'] or x['payment_method'] == self._payment_method]:
                        pricing_rows.append({
                            'tariff_code': a['tariff_code'],
                            'valid_from': DateUtils.iso8601(f if not r['valid_from'] else max(f, datetime.fromisoformat(r['valid_from']))),
                            'valid_to': DateUtils.iso8601(t if not r['valid_to'] else min(t, datetime.fromisoformat(r['valid_to']))),
                            'value_exc_vat': r['value_exc_vat'],
                            'value_inc_vat': r['value_inc_vat'],
                        })
                    if component not in pricing:
                        pricing[component] = []
                    pricing[component] += pricing_rows
                if t < collect_to:
                    f = t
                    t = collect_to
        return pricing

    def _process_intelligent_slots(self, mpan, base_tags: dict[str, str]):
        dispatches = asyncio.run(self._octopus_ql_api.async_get_intelligent_dispatches(self._account_number))
        intelligent_slots = {}
        for r in dispatches.completed:
            dt = r.start
            to = r.end
            while dt < to:
                intelligent_slots[DateUtils.iso8601(dt)] = {
                    'mpan': mpan,
                    'start_time': DateUtils.iso8601(r.start),
                    'end_time': DateUtils.iso8601(r.end),
                }
                dt = dt + timedelta(minutes=self._resolution_minutes)

        self._store_intelligent_slots(intelligent_slots, base_tags)

    def _store_intelligent_slots(self, intelligent_slots: dict, base_tags: dict[str, str]):
        points = [
            Point.from_dict({
                'measurement': 'intelligent_slots',
                'time': t,
                'fields': {
                    'intelligent_slot': True,
                },
                'tags': {
                    'mpan': intelligent_slots[t]['mpan'],
                    'start_time': intelligent_slots[t]['start_time'],
                    'end_time': intelligent_slots[t]['end_time'],
                } | base_tags,
            }, write_precision=WritePrecision.S)
            for t in intelligent_slots.keys()
        ]
        click.echo(f'Storing {len(points)} intelligent slots')
        self._influx_write.write(self._influx_bucket, record=points)

    def _store_emp_pricing(self, standard_unit_rates, standing_charges, base_tags: dict[str, str]):
        self._store_pricing('electricity_pricing', standard_unit_rates, standing_charges, base_tags)

    def _store_gmp_pricing(self, standard_unit_rates, standing_charges, base_tags: dict[str, str]):
        self._store_pricing('gas_pricing', standard_unit_rates, standing_charges, base_tags)

    def _store_pricing(self, measurement: str, standard_unit_rates, standing_charges, base_tags: dict[str, str]):
        points = [
            Point.from_dict({
                'measurement': measurement,
                'time': t,
                'fields': {
                    'unit_price_exc_vat_price': standard_unit_rates[t]['value_exc_vat'],
                    'unit_price_inc_vat_price': standard_unit_rates[t]['value_inc_vat'],
                    'standing_charge_exc_vat_price': standing_charges[t]['value_exc_vat'],
                    'standing_charge_inc_vat_price': standing_charges[t]['value_inc_vat'],
                },
                'tags': {'tariff_code': standard_unit_rates[t]['tariff_code']} | base_tags,
            }, write_precision=WritePrecision.S)
            for t in standard_unit_rates.keys()
        ]
        click.echo(f'Storing {len(points)} points')
        self._influx_write.write(self._influx_bucket, record=points)


@click.command()
@click.option(
    '--config-file',
    default="octograph.ini",
    type=click.Path(exists=True, dir_okay=True, readable=True),
)
@click.option('--from-date', default='latest', type=click.STRING)
@click.option('--to-date', default='tomorrow', type=click.STRING)
def cmd(config_file, from_date, to_date):
    app = OctopusToInflux(config_file)
    app.collect(from_date, to_date)


if __name__ == '__main__':
    cmd()
