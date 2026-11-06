import unittest
from datetime import datetime, timedelta

import pytz
from parameterized import parameterized

from app.date_utils import DateUtils
from app.eco_unit_normaliser import EcoUnitNormaliser
from app.series_maker import SeriesMaker

LONDON_TZ = pytz.timezone('Europe/London')


class TestStartOfLocalDay(unittest.TestCase):

    @parameterized.expand([
        ('bst_mid_day', '2026-09-30T09:30:00+00:00', '2026-09-29T23:00:00Z'),
        ('bst_after_local_midnight', '2026-09-29T23:30:00+00:00', '2026-09-29T23:00:00Z'),
        ('bst_already_local_midnight', '2026-09-29T23:00:00+00:00', '2026-09-29T23:00:00Z'),
        ('gmt_mid_day', '2026-01-15T09:30:00+00:00', '2026-01-15T00:00:00Z'),
        ('clocks_go_back', '2026-10-25T12:00:00+00:00', '2026-10-24T23:00:00Z'),
        ('clocks_go_forward', '2026-03-29T12:00:00+00:00', '2026-03-29T00:00:00Z'),
    ])
    def test_start_of_local_day(self, _, dt, expected):
        result = DateUtils.start_of_local_day(datetime.fromisoformat(dt), LONDON_TZ)
        self.assertEqual(expected, DateUtils.iso8601(result))


class TestLatestCollectFromAlignment(unittest.TestCase):
    """
    Reproduces the KeyError from Yanson/octograph#18: with --from-date latest, collect_from was the last
    stored point + 30 mins, but EcoUnitNormaliser emits unit rates from local midnight, so unit rate keys
    preceded the standing charge keys. Rounding collect_from to the start of the local day keeps them aligned.
    """

    @staticmethod
    def _clipped_row(f: datetime, t: datetime, value: float):
        # Mirrors the clipping applied to pricing rows in OctopusToInflux._get_pricing
        return {'tariff_code': 'E-1R-TEST-A', 'valid_from': DateUtils.iso8601(f), 'valid_to': DateUtils.iso8601(t),
                'value_exc_vat': value, 'value_inc_vat': value}

    @parameterized.expand([
        ('bst', '2026-09-30T09:00:00+00:00', '2026-09-29T23:00:00Z'),
        ('gmt', '2026-01-15T09:00:00+00:00', '2026-01-15T00:00:00Z'),
    ])
    def test_unit_rates_and_standing_charges_aligned(self, _, latest, expected_first_key):
        latest = datetime.fromisoformat(latest)
        f = DateUtils.start_of_local_day(latest + timedelta(minutes=30), LONDON_TZ)
        t = f + timedelta(days=1)

        normaliser = EcoUnitNormaliser(LONDON_TZ, 0, 5)
        unit_rows = normaliser.normalise([self._clipped_row(f, t, 30)], [self._clipped_row(f, t, 7)],
                                         DateUtils.local_date(f, LONDON_TZ), DateUtils.local_date(t, LONDON_TZ))
        series_maker = SeriesMaker(30)
        standard_unit_rates = series_maker.make_series(unit_rows)
        standing_charges = series_maker.make_series([self._clipped_row(f, t, 50)])

        self.assertEqual(set(standing_charges.keys()), set(standard_unit_rates.keys()))
        self.assertEqual(expected_first_key, min(standing_charges.keys()))


if __name__ == '__main__':
    unittest.main()
