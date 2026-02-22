import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytz
from parameterized import parameterized

from app.date_utils import DateUtils

LONDON_TZ = pytz.timezone('Europe/London')


def get_pricing(agreements, collect_from: datetime, collect_to: datetime,
                octopus_api, payment_method: str = 'DIRECT_DEBIT'):
    """
    Extract of the _get_pricing method for testing purposes.

    This function is equivalent to OctopusToInflux._get_pricing but can be
    tested without instantiating the full class.
    """
    f = collect_from
    t = collect_to
    pricing = {}
    for a in sorted(agreements, key=lambda x: datetime.fromisoformat(x['valid_from'])):
        agreement_valid_from = datetime.fromisoformat(a['valid_from'])
        agreement_valid_to = datetime.fromisoformat(a['valid_to']) if a['valid_to'] else collect_to
        # Check if agreement overlaps with the remaining collection period [f, t)
        # An agreement overlaps if it starts before t and ends after f
        if agreement_valid_from < t and agreement_valid_to > f:
            # Determine the effective period to fetch pricing for
            effective_from = max(f, agreement_valid_from)
            effective_to = min(t, agreement_valid_to)
            agreement_rates = octopus_api.retrieve_tariff_pricing(a['tariff_code'], DateUtils.iso8601(effective_from), DateUtils.iso8601(effective_to))
            for component, results in agreement_rates.items():
                pricing_rows = []
                for r in [x for x in results if not x['payment_method'] or x['payment_method'] == payment_method]:
                    pricing_rows.append({
                        'tariff_code': a['tariff_code'],
                        'valid_from': DateUtils.iso8601(effective_from if not r['valid_from'] else max(effective_from, datetime.fromisoformat(r['valid_from']))),
                        'valid_to': DateUtils.iso8601(effective_to if not r['valid_to'] else min(effective_to, datetime.fromisoformat(r['valid_to']))),
                        'value_exc_vat': r['value_exc_vat'],
                        'value_inc_vat': r['value_inc_vat'],
                    })
                if component not in pricing:
                    pricing[component] = []
                pricing[component] += pricing_rows
            # Update f to continue from the end of this agreement's effective period
            if effective_to > f:
                f = effective_to
                t = collect_to
    return pricing


# For comparison, here's the OLD buggy version to demonstrate the fix
def get_pricing_old_buggy(agreements, collect_from: datetime, collect_to: datetime,
                          octopus_api, payment_method: str = 'DIRECT_DEBIT'):
    """
    The OLD buggy implementation for comparison/regression testing.

    BUG: The condition `agreement_valid_from <= f` fails when an agreement
    starts AFTER collect_from but BEFORE collect_to.
    """
    f = collect_from
    t = collect_to
    pricing = {}
    for a in sorted(agreements, key=lambda x: datetime.fromisoformat(x['valid_from'])):
        agreement_valid_from = datetime.fromisoformat(a['valid_from'])
        agreement_valid_to = datetime.fromisoformat(a['valid_to']) if a['valid_to'] else collect_to
        # OLD BUGGY CONDITION: requires f to be within agreement bounds
        if agreement_valid_from <= f < agreement_valid_to:
            if agreement_valid_to < t:
                t = agreement_valid_to
            agreement_rates = octopus_api.retrieve_tariff_pricing(a['tariff_code'], DateUtils.iso8601(f), DateUtils.iso8601(t))
            for component, results in agreement_rates.items():
                pricing_rows = []
                for r in [x for x in results if not x['payment_method'] or x['payment_method'] == payment_method]:
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


class TestGetPricing(unittest.TestCase):
    """
    Tests for the _get_pricing method logic.

    These tests verify that pricing is correctly fetched for various agreement scenarios,
    particularly focusing on the fix for agreements that start during the collection period.
    """

    def _create_mock_api(self):
        """Create a mock Octopus API client."""
        return MagicMock()

    @parameterized.expand([
        # Test case name, month (2=winter/GMT, 8=summer/BST)
        ('winter', 2),
        ('summer', 8),
    ])
    def test_single_agreement_covers_entire_period(self, name, month):
        """Test that a single agreement covering the entire collection period works correctly."""
        mock_api = self._create_mock_api()

        # Collection period: 2 days
        collect_from = LONDON_TZ.localize(datetime(2025, month, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, month, 23, 0, 0, 0))

        # Agreement that covers the entire period and beyond
        agreements = [{
            'tariff_code': 'E-1R-AGILE-24-10-01-L',
            'valid_from': '2025-02-15T00:00:00Z',
            'valid_to': '2026-02-15T00:00:00Z',
        }]

        # Mock API response
        mock_api.retrieve_tariff_pricing.return_value = {
            'standard_unit_rates': [{
                'valid_from': DateUtils.iso8601(collect_from),
                'valid_to': DateUtils.iso8601(collect_to),
                'value_exc_vat': 10.0,
                'value_inc_vat': 10.5,
                'payment_method': None,
            }],
            'standing_charges': [{
                'valid_from': DateUtils.iso8601(collect_from),
                'valid_to': DateUtils.iso8601(collect_to),
                'value_exc_vat': 40.0,
                'value_inc_vat': 42.0,
                'payment_method': None,
            }],
        }

        result = get_pricing(agreements, collect_from, collect_to, mock_api)

        # Verify API was called with correct date range
        mock_api.retrieve_tariff_pricing.assert_called_once()
        call_args = mock_api.retrieve_tariff_pricing.call_args
        self.assertEqual(call_args[0][0], 'E-1R-AGILE-24-10-01-L')
        self.assertEqual(call_args[0][1], DateUtils.iso8601(collect_from))
        self.assertEqual(call_args[0][2], DateUtils.iso8601(collect_to))

        # Verify result contains pricing
        self.assertIn('standard_unit_rates', result)
        self.assertIn('standing_charges', result)
        self.assertEqual(len(result['standard_unit_rates']), 1)

    @parameterized.expand([
        # Test case name, month (2=winter/GMT, 8=summer/BST)
        ('winter', 2),
        ('summer', 8),
    ])
    def test_agreement_starts_during_collection_period(self, name, month):
        """
        Test the bug fix: agreement that starts AFTER collect_from but BEFORE collect_to.

        This was the original bug - when backfilling from 2025-07-21 to 2025-07-22,
        an export meter with agreement starting 2025-07-22 would be skipped entirely.
        """
        mock_api = self._create_mock_api()

        # Collection period: 2 days (21st and 22nd)
        collect_from = LONDON_TZ.localize(datetime(2025, month, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, month, 23, 0, 0, 0))

        # Agreement starts on the 22nd (one day into the collection period)
        agreement_start = LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0))

        agreements = [{
            'tariff_code': 'E-1R-OUTGOING-VAR-24-10-26-L',
            'valid_from': agreement_start.isoformat(),
            'valid_to': None,  # Open-ended
        }]

        # Mock API response for the period the agreement covers
        mock_api.retrieve_tariff_pricing.return_value = {
            'standard_unit_rates': [{
                'valid_from': DateUtils.iso8601(agreement_start),
                'valid_to': DateUtils.iso8601(collect_to),
                'value_exc_vat': 15.0,
                'value_inc_vat': 15.75,
                'payment_method': None,
            }],
            'standing_charges': [{
                'valid_from': DateUtils.iso8601(agreement_start),
                'valid_to': DateUtils.iso8601(collect_to),
                'value_exc_vat': 40.0,
                'value_inc_vat': 42.0,
                'payment_method': None,
            }],
        }

        result = get_pricing(agreements, collect_from, collect_to, mock_api)

        # THE KEY ASSERTION: With the fix, the API should be called
        # Before the fix, the agreement would be skipped and API never called
        mock_api.retrieve_tariff_pricing.assert_called_once()

        # Verify API was called with the correct effective date range
        # (starting from agreement start, not collect_from)
        call_args = mock_api.retrieve_tariff_pricing.call_args
        self.assertEqual(call_args[0][0], 'E-1R-OUTGOING-VAR-24-10-26-L')
        self.assertEqual(call_args[0][1], DateUtils.iso8601(agreement_start))
        self.assertEqual(call_args[0][2], DateUtils.iso8601(collect_to))

        # Verify result contains pricing
        self.assertIn('standard_unit_rates', result)
        self.assertEqual(len(result['standard_unit_rates']), 1)

    @parameterized.expand([
        ('winter', 2),
        ('summer', 8),
    ])
    def test_old_buggy_version_fails_for_mid_period_agreement(self, name, month):
        """
        Demonstrate that the OLD buggy code fails for agreements starting mid-period.

        This test proves the bug existed and the fix is necessary.
        """
        mock_api = self._create_mock_api()

        collect_from = LONDON_TZ.localize(datetime(2025, month, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, month, 23, 0, 0, 0))

        # Agreement starts on the 22nd
        agreement_start = LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0))

        agreements = [{
            'tariff_code': 'E-1R-OUTGOING-VAR-24-10-26-L',
            'valid_from': agreement_start.isoformat(),
            'valid_to': None,
        }]

        mock_api.retrieve_tariff_pricing.return_value = {
            'standard_unit_rates': [{
                'valid_from': DateUtils.iso8601(agreement_start),
                'valid_to': DateUtils.iso8601(collect_to),
                'value_exc_vat': 15.0,
                'value_inc_vat': 15.75,
                'payment_method': None,
            }],
        }

        # The OLD buggy version should NOT call the API (agreement skipped)
        result_old = get_pricing_old_buggy(agreements, collect_from, collect_to, mock_api)

        # With the old code, API was never called because agreement was skipped
        # Reset mock to test new version
        mock_api.reset_mock()

        # The NEW fixed version SHOULD call the API
        result_new = get_pricing(agreements, collect_from, collect_to, mock_api)

        # Old version returns empty result (BUG!)
        self.assertEqual(result_old, {})

        # New version returns pricing (FIXED!)
        self.assertIn('standard_unit_rates', result_new)
        self.assertEqual(len(result_new['standard_unit_rates']), 1)

    @parameterized.expand([
        ('winter', 2),
        ('summer', 8),
    ])
    def test_agreement_ends_during_collection_period(self, name, month):
        """Test agreement that ends during the collection period."""
        mock_api = self._create_mock_api()

        collect_from = LONDON_TZ.localize(datetime(2025, month, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, month, 23, 0, 0, 0))

        # Agreement ends on the 22nd
        agreement_end = LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0))

        agreements = [{
            'tariff_code': 'E-1R-OLD-TARIFF',
            'valid_from': '2024-01-01T00:00:00Z',
            'valid_to': agreement_end.isoformat(),
        }]

        mock_api.retrieve_tariff_pricing.return_value = {
            'standard_unit_rates': [{
                'valid_from': DateUtils.iso8601(collect_from),
                'valid_to': DateUtils.iso8601(agreement_end),
                'value_exc_vat': 10.0,
                'value_inc_vat': 10.5,
                'payment_method': None,
            }],
            'standing_charges': [{
                'valid_from': DateUtils.iso8601(collect_from),
                'valid_to': DateUtils.iso8601(agreement_end),
                'value_exc_vat': 40.0,
                'value_inc_vat': 42.0,
                'payment_method': None,
            }],
        }

        result = get_pricing(agreements, collect_from, collect_to, mock_api)

        # Verify API was called with effective_to = agreement_end
        call_args = mock_api.retrieve_tariff_pricing.call_args
        self.assertEqual(call_args[0][2], DateUtils.iso8601(agreement_end))

        self.assertIn('standard_unit_rates', result)

    @parameterized.expand([
        ('winter', 2),
        ('summer', 8),
    ])
    def test_multiple_consecutive_agreements(self, name, month):
        """Test multiple agreements that together cover the collection period."""
        mock_api = self._create_mock_api()

        collect_from = LONDON_TZ.localize(datetime(2025, month, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, month, 23, 0, 0, 0))

        # Boundary between agreements
        boundary = LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0))

        agreements = [
            {
                'tariff_code': 'E-1R-OLD-TARIFF',
                'valid_from': '2024-01-01T00:00:00Z',
                'valid_to': boundary.isoformat(),
            },
            {
                'tariff_code': 'E-1R-NEW-TARIFF',
                'valid_from': boundary.isoformat(),
                'valid_to': '2026-01-01T00:00:00Z',
            },
        ]

        def mock_pricing(tariff_code, from_date, to_date):
            return {
                'standard_unit_rates': [{
                    'valid_from': from_date,
                    'valid_to': to_date,
                    'value_exc_vat': 10.0 if 'OLD' in tariff_code else 12.0,
                    'value_inc_vat': 10.5 if 'OLD' in tariff_code else 12.6,
                    'payment_method': None,
                }],
                'standing_charges': [{
                    'valid_from': from_date,
                    'valid_to': to_date,
                    'value_exc_vat': 40.0,
                    'value_inc_vat': 42.0,
                    'payment_method': None,
                }],
            }

        mock_api.retrieve_tariff_pricing.side_effect = mock_pricing

        result = get_pricing(agreements, collect_from, collect_to, mock_api)

        # Both agreements should be processed
        self.assertEqual(mock_api.retrieve_tariff_pricing.call_count, 2)

        # Verify we have pricing from both tariffs
        self.assertEqual(len(result['standard_unit_rates']), 2)
        tariff_codes = [r['tariff_code'] for r in result['standard_unit_rates']]
        self.assertIn('E-1R-OLD-TARIFF', tariff_codes)
        self.assertIn('E-1R-NEW-TARIFF', tariff_codes)

    def test_no_matching_agreements(self):
        """Test that no pricing is returned when no agreements cover the period."""
        mock_api = self._create_mock_api()

        collect_from = LONDON_TZ.localize(datetime(2025, 7, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, 7, 23, 0, 0, 0))

        # Agreement is entirely before the collection period
        agreements = [{
            'tariff_code': 'E-1R-OLD-TARIFF',
            'valid_from': '2024-01-01T00:00:00Z',
            'valid_to': '2025-01-01T00:00:00Z',
        }]

        result = get_pricing(agreements, collect_from, collect_to, mock_api)

        # API should not be called
        mock_api.retrieve_tariff_pricing.assert_not_called()

        # Result should be empty
        self.assertEqual(result, {})

    def test_agreement_starts_after_collection_period(self):
        """Test that agreements starting after the collection period are ignored."""
        mock_api = self._create_mock_api()

        collect_from = LONDON_TZ.localize(datetime(2025, 7, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, 7, 23, 0, 0, 0))

        # Agreement starts after collection period
        agreements = [{
            'tariff_code': 'E-1R-FUTURE-TARIFF',
            'valid_from': '2025-08-01T00:00:00Z',
            'valid_to': None,
        }]

        result = get_pricing(agreements, collect_from, collect_to, mock_api)

        mock_api.retrieve_tariff_pricing.assert_not_called()
        self.assertEqual(result, {})

    @parameterized.expand([
        ('winter', 2),
        ('summer', 8),
    ])
    def test_real_world_scenario_export_meter_bug(self, name, month):
        """
        Reproduce the exact bug scenario from the issue.

        This tests the specific case where:
        - Backfilling from 2025-07-21 to 2025-07-22
        - Export meter has agreement starting 2025-07-22T00:00:00+01:00
        - Import meter has agreement covering the entire period

        Before the fix, the export meter's agreement would be skipped,
        causing KeyError when looking up pricing for consumption data.
        """
        mock_api = self._create_mock_api()

        # Simulate --from-date=2025-07-21 --to-date=2025-07-22
        # This gives collect_from = midnight July 21, collect_to = midnight July 23
        collect_from = LONDON_TZ.localize(datetime(2025, month, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, month, 23, 0, 0, 0))

        # Export meter agreement - starts on the 22nd (local midnight)
        export_agreements = [{
            'tariff_code': 'E-1R-OUTGOING-VAR-24-10-26-L',
            'valid_from': LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0)).isoformat(),
            'valid_to': None,
        }]

        mock_api.retrieve_tariff_pricing.return_value = {
            'standard_unit_rates': [{
                'valid_from': LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0)).isoformat(),
                'valid_to': collect_to.isoformat(),
                'value_exc_vat': 15.0,
                'value_inc_vat': 15.75,
                'payment_method': None,
            }],
            'standing_charges': [{
                'valid_from': LONDON_TZ.localize(datetime(2025, month, 22, 0, 0, 0)).isoformat(),
                'valid_to': collect_to.isoformat(),
                'value_exc_vat': 0.0,
                'value_inc_vat': 0.0,
                'payment_method': None,
            }],
        }

        result = get_pricing(export_agreements, collect_from, collect_to, mock_api)

        # THE CRITICAL TEST: Before the fix, this would fail because
        # the agreement starting on the 22nd would be skipped when
        # collect_from is the 21st
        self.assertIn('standard_unit_rates', result)
        self.assertGreater(len(result['standard_unit_rates']), 0)

        # Verify the pricing covers the correct period (22nd only)
        pricing_row = result['standard_unit_rates'][0]
        self.assertEqual(pricing_row['tariff_code'], 'E-1R-OUTGOING-VAR-24-10-26-L')

    def test_payment_method_filtering(self):
        """Test that pricing with wrong payment method is filtered out."""
        mock_api = self._create_mock_api()

        collect_from = LONDON_TZ.localize(datetime(2025, 7, 21, 0, 0, 0))
        collect_to = LONDON_TZ.localize(datetime(2025, 7, 22, 0, 0, 0))

        agreements = [{
            'tariff_code': 'E-1R-TEST',
            'valid_from': '2024-01-01T00:00:00Z',
            'valid_to': '2026-01-01T00:00:00Z',
        }]

        mock_api.retrieve_tariff_pricing.return_value = {
            'standard_unit_rates': [
                {
                    'valid_from': DateUtils.iso8601(collect_from),
                    'valid_to': DateUtils.iso8601(collect_to),
                    'value_exc_vat': 10.0,
                    'value_inc_vat': 10.5,
                    'payment_method': 'DIRECT_DEBIT',
                },
                {
                    'valid_from': DateUtils.iso8601(collect_from),
                    'valid_to': DateUtils.iso8601(collect_to),
                    'value_exc_vat': 12.0,
                    'value_inc_vat': 12.6,
                    'payment_method': 'NON_DIRECT_DEBIT',
                },
            ],
            'standing_charges': [],
        }

        result = get_pricing(agreements, collect_from, collect_to, mock_api,
                            payment_method='DIRECT_DEBIT')

        # Only DIRECT_DEBIT pricing should be included
        self.assertEqual(len(result['standard_unit_rates']), 1)
        self.assertEqual(result['standard_unit_rates'][0]['value_exc_vat'], 10.0)


if __name__ == '__main__':
    unittest.main()


