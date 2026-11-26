# Octograph

Python tool for downloading energy consumption data from the
_Octopus Energy API_ and loading it into _InfluxDB_.

If you think you'd find this useful, but haven't switched to Octopus yet, then
you can follow my referrer link _https://share.octopus.energy/wind-gecko-169_.
You'll receive a £50 bill credit, and so will I :-D

Thanks to [stevenewey/octograph](https://github.com/stevenewey/octograph) for inspiration for this project,
though there is very little left from the original codebase.

![](dashboard-electricity.png)
![](dashboard-gas.png)

## Octograph Query

See also the [Yanson/octograph-query](https://github.com/Yanson/octograph-query) sister project for visualising today's electricity pricing.

<img src="https://github.com/Yanson/octograph-query/raw/main/README_example.png" width="360" alt="Example chart" />

## Docker Compose Quick Start

You can get started with docker compose to give you an InfluxDB server and Grafana instance:

```shell
docker compose up -d
```

Create your own copy of the config file, and replace your `account_number` as appropriate:
```shell
cp octograph.example.ini octograph.home.ini
```

Run the ingestion, backfilling from the beginning of 2025:

```shell
docker run --rm --name octograph --network octograph \
  -v "$PWD/octograph.home.ini":/etc/octograph.ini:ro \
  -e OCTOGRAPH_INFLUXDB_TOKEN="secret_admin_token" \
  -e OCTOGRAPH_OCTOPUS_API_KEY="sk_live_..." \
  yansonb/octograph:latest \
  --config-file=/etc/octograph.ini \
  --from-date=2025-01-01
```

Open the Grafana UI and login with the user / pass `admin` / `admin`.
* http://localhost:13000/

There will be one dashboard automatically installed called _"Energy Usage"_.
* http://localhost:13000/d/energy-usage/energy-usage

You can also view the [octograph-query](https://github.com/Yanson/octograph-query) visualisation of today's electricity pricing at:
* http://localhost:18080/query?location=Home&format=html&width=800&height=400
               
> [!NOTE]  
> `ocotograph-query` depends on a `location` tag which is specified in the example config file: `additional_tags = location=Home`

## Usage

This project can be run directly or from the published Docker image.
If you want to run the code locally, see [Developer set-up](#developer-set-up) below.

The default configuration requires two secrets pass through environment variables:

* `OCTOGRAPH_INFLUXDB_TOKEN`: The InfluxDB token for writing metrics (note, this must have `read` and `write` scope).
* `OCTOGRAPH_OCTOPUS_API_KEY`: Your Octopus API key found in your [account developer settings](https://octopus.energy/dashboard/new/accounts/personal-details/api-access).

Assuming you want to use all other default configuration values, the only other required config to set is your Octopus **account number**.
See [below](#configuration) for a full list of configuration options.
[docker-compose.yml](docker-compose.yml)
### Command-line options

The application takes three command-line options:

| Option          | Description                                                                                            | Default         |
|-----------------|--------------------------------------------------------------------------------------------------------|-----------------|
| `--config-file` | File to load the configuration from                                                                    | `octograph.ini` |
| `--from-date`   | Date to collect fill data from in the format `YYY-MM-DD` or the special values `latest` or `yesterday` | `latest`        |
| `--to-date`     | Date to collect data to in the format `YYY-MM-DD` or the special values `yesterday` or `tomorrow`      | `tomorrow`      |

As this project was primarily built to query Agile rates - which fluctuate, and the pricing for which is published in advance - the default collection period is up to "tomorrow".
If you are not on an agile tariff, you should use a `--to-date` of `yesterday` (consumption is not realtime and will only be available at some point the next day).

In the event consumption data for your meter is not yet available when the program is run, it can "catch up" on missing data by using the `--from-date=latest` option.
This will look back to see when the last datapoints is available from the last 30 days and query for new consumption (and pricing) from the start of that (local) day.
NOTE: You must back-fill some data from a fixed date before using the `latest` option, unless the property was moved into within the last 30 days (see [Moving house](#example-moving-house)).
If a meter point has no data in the last 30 days, the run reports `No data found ... - try back-filling` for it, carries on with the other meter points and then exits with an error.

### Back-filling

You can populate the metrics with all historic data in your account by specifying a `--from-date` equal to when your account was opened.

From then-on, you can use the defaults `--from-date=latest` and `--to-date=tomorrow` to collect fresh data on a daily basis.

### Scheduling jobs

Consumption data for your meters will be available, usually, at some point in the day following the consumption. 
Hence, you should run the ingestion a couple of times with the configuration `--from-date=latest --to-date=yesterday` to collect yesterday's data.
If you are on an agile tariff, you might prefer to specify `--to-date=tomorrow` to get pricing data covering today and tomorrow (tomorrow's data is usually available at around 5pm).
If you are on an intelligent tariff, you should run this script at least once every 12 hours because the Octopus GraphQL API only provides information about intelligent dispatch slots that occurred during the previous 12 hours. Note that information about dispatch slots from the API might not align exactly to what appears on your bill (there is often an extra dispatch slot at the end of an EV charging period that is not reflected on your bill as a low-rate period). This is an issue with the API data, not this tool.

## Configuration

| Section    | Option                      | Description                                                                                                                       | Default                                             |
|------------|-----------------------------|-----------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------|
| `influxdb` | `url`                       | InfluxDB connection URL                                                                                                           | `http://localhost:8086`                             |
|            | `org`                       | Influx organisation to use                                                                                                        | `primary`                                           |
|            | `bucket`                    | Influx bucket to store the metrics in                                                                                             | `primary`                                           |
|            | `token_env_var`             | Environment variable to lookup to find InfluxDB authentication token                                                              | `OCTOGRAPH_INFLUXDB_TOKEN`                          |
|            | `included_tags`             | Tags to add to the InfluxDB data points based on account and meter information (see [below](#available-tags) for list of options) | _None_                                              |
|            | `additional_tags`           | Comma-separated key=value list of tags to add to the InfluxDB data points (specify a `location` tag to use the sample dashboard)  | _None_                                              |
| `additional_tags:<id>` | _any tag key_   | Optional section per MPAN, MPRN or meter serial number adding or overriding tags for that meter point / meter (see [below](#per-meter-tags)) | _None_                              |
| `octopus`  | `account_number`            | Octopus account number (something like `A-XXXXXXXX`)                                                                              |                                                     |
|            | `timezone`                  | Timezone to work in when converting dates and times (see timezone information [below](#timezone-information)                      | `Europe/London`                                     |
|            | `payment_method`            | Pricing to select when there is more than one payment option for an agreement                                                     | `DIRECT_DEBIT`                                      |
|            | `unit_rate_low_start`       | Hour of day (`0-23`) when "night" rate starts (if you have an Economy 7 meter)                                                    | `1`                                                 |
|            | `unit_rate_low_end`         | Hour of day (`0-23`) when "night" rate ends                                                                                       | `8`                                                 |
|            | `gas_meter_types`           | Comma-separated key=value list of meter types where key is the meter serial number and value is the type (`SMETS1` or `SMETS2`)   | `SMETS1` is assumed when serial number is not found |
|            | `volume_correction_factor`  | Volume correction factor for `SMETS2` meters which need to be converted from cubic meters to kWh                                  | `1.02264`                                           |
|            | `calorific_value`           | Calorific value for `SMETS2` meter unit conversion to kWh                                                                         | `38.8`                                              |
|            | `resolution_minutes`        | Resolution of data to query and store                                                                                             | `30`                                                |
|            | `api_prefix`                | Octopus API URI prefix                                                                                                            | `https://api.octopus.energy/v1`                     |
|            | `api_key_env_var`           | Environment variable to lookup to find the Octopus API key                                                                        | `OCTOGRAPH_OCTOPUS_API_KEY`                         |
|            | `enable_cache`              | Cache results from the Octopus API on the local filesystem (useful for development)                                               | `/tmp/octopus_api_cache`                            |
|            | `cache_dir`                 | Directory to store API responses in for caching purposes (if enabled)                                                             | `false`                                             |
|            | `included_meters`           | Comma-separated list of meter serial numbers to include (useful when some meters in your account are not active)                  | All meters found in your account if not provided    |
|            | `intelligent_tariff_meter`  | Meter serial number for intelligent import tariff (so low rate periods can be applied to this meter's consumption)                | _None_                                              |
|            | `intelligent_low_rate_hour` | Hour of day (`0-23`) to copy "off-peak" rate from for intelligent dispatch slots                                                  | `3`                                                 |

### Available Tags

Tags can be added to the InfluxDB data points based on the account, meter and agreement information looked up.

* `account_number`: Account number, as specified by `octopus.account_number`
* `property_address_line_1`: Property information returned by the Octopus API
* `property_address_line_2`
* `property_address_line_3`
* `property_town`
* `property_postcode`
* `electricity_mpan`: Electricity meter point administration number
* `gas_mprn`: Gas meter point reference number
* `meter_serial_number`: Serial number of the electricity or gas meter
* `agreement_tariff_code`: Electricity or gas tariff code applicable at the time of the consumption

To include all tags, you can use the setting:

```text
account_number,property_address_line_1,property_address_line_2,property_address_line_3,property_town,property_postcode,electricity_mpan,gas_mprn,meter_serial_number,agreement_tariff_code
```

### Additional Tags

If you want to use the sample dashboard, you will need to include a `location` tag through the `influxdb.additional_tags` config. For example:

```text
additional_tags = location=Home
```

If your account has more than one property, you can either run separate ingestions with different additional tags filter based on `octopus.included_meters`,
use [per-meter tags](#per-meter-tags), or use `influxdb.included_tags` to include some of the property address information and add additional filters to the dashboard.

### Per-meter tags

Tags can be added or overridden for individual meter points and meters with one optional `[additional_tags:<identifier>]` section per identifier,
where `<identifier>` is an electricity MPAN, a gas MPRN or a meter serial number. Each section can set any number of tags:

```ini
[additional_tags:1900000000002]
location = New Home
```

Tags are resolved as follows (later steps win for the same key):

1. The global `influxdb.additional_tags`.
2. The section for the meter point's MPAN / MPRN. This applies to the meter point's data (`electricity_pricing`, `gas_pricing`) **and is inherited by every meter under it**.
3. The section for the meter's serial number. This applies only to that meter's consumption (`electricity_consumption`, `gas_consumption`).

Intelligent dispatch slots use the section matching `octopus.intelligent_tariff_meter`, if any.
Tags derived from the account (those listed in [Available Tags](#available-tags), plus `tariff_code`, `mpan`, `start_time` and `end_time`) cannot be set in these sections.
Tag keys keep their case. A config without any of these sections behaves exactly as before.

`--from-date=latest` looks for the latest consumption using the resolved tags, so a meter point whose tags change (e.g. a new `location`) has to be back-filled once from a fixed date before `latest` can be used for it.
The exception is a property moved into within the last 30 days, which `latest` collects from its move-in date automatically (see below).

#### Example: moving house

Each property in your Octopus account is collected only while you live there: from its `moved_in_at` (inclusive) to its `moved_out_at` (exclusive), where a missing date means no limit.

* A property is skipped (with the reason logged) only if this window doesn't overlap the requested dates. This happens if it was moved out of before the range or hasn't been moved into yet.
* Otherwise the range is cut to the window, so the old property is collected up to its move-out and the new one from its move-in, even within one run. No points are written outside the window. If a move date falls part-way through an interval, that interval is left out.
* With `--from-date=latest`, a meter point with no data in the last 30 days is collected from its property's move-in date if that was within the last 30 days. If the move-in date is still in the future, the meter point is skipped for that run. A property you moved out of within the last 30 days is still caught up to its move-out date.

The old property keeps the global `location`, and the new property's meter point and meter get their own:

```ini
[influxdb]
additional_tags = location=Old Home

[octopus]
; add the new meter's serial number if you use included_meters
included_meters = <old electricity serial>,<old gas serial>,<new electricity serial>,<new gas serial>

[additional_tags:<new MPAN>]
location = New Home

[additional_tags:<new MPRN>]
location = New Home
```

Because the meter inherits from its MPAN, a separate `[additional_tags:<new meter serial>]` section is optional - add one only if the meter needs different tags from its meter point.
No manual back-fill is needed: the scheduled `--from-date=latest` run keeps collecting the old property until its move-out date and starts the new one from its move-in date.
You only need to back-fill (with `--from-date=YYYY-MM-DD`) if a meter point has had no data for more than 30 days, for example if you add the config more than 30 days after moving in.

## Timezone Information

To align with information available from the API, inputs and requests are made aligned to days according to the timezone provided.
This is also used to calculate the relevant price when a day/night meter and tariff is being used.

## Developer set-up

* Create and activate a virtual environment, e.g. `python3 -m venv venv` ([docs](https://docs.python.org/3/library/venv.html))
* `python -m pip install --upgrade pip poetry`
* `poetry install`

### Run unit tests

* `poetry run pytest -v tests`

### Run app locally

* Create a valid config file
* Set environment variables containing credentials (`OCTOGRAPH_INFLUXDB_TOKEN` and `OCTOGRAPH_OCTOPUS_API_KEY`)
* Run `python app/octopus_to_influxdb.py --config-file=octograph.ini`

### Build docker image locally

* `docker build --build-arg VERSION=$(git rev-parse --short HEAD) -t octograph:dev .`

### Run docker image locally

Create a config file (see the [docker-compose quick start](#docker-compose-quick-start)).

```shell
OCTOGRAPH_INFLUXDB_TOKEN="..."
OCTOGRAPH_OCTOPUS_API_KEY="..."
docker run --rm --name octograph \
  -v "$PWD/octograph.home.ini":/etc/octograph.ini:ro \
  -e OCTOGRAPH_INFLUXDB_TOKEN="$OCTOGRAPH_INFLUXDB_TOKEN" \
  -e OCTOGRAPH_OCTOPUS_API_KEY="$OCTOGRAPH_OCTOPUS_API_KEY" \
  octograph:dev \
  --config-file=/etc/octograph.ini
```

If you are using docker-compose with the example config, add the `--network octograph` option.

If you have not built the image locally, change the image and tag to one from [Docker Hub](https://hub.docker.com/repository/docker/yansonb/octograph), e.g. `yansonb/octograph:latest`.

## Known Issues

* The cubic meter to kWh conversion used fixed values whereas they actually fluctuate (see link below).

## Links

* [Octopus Energy API](https://developer.octopus.energy/docs/api/)
* [InfluxDB](https://docs.influxdata.com/influxdb/v2/) | [Docker Image](https://hub.docker.com/_/influxdb)
* [Grafana](https://grafana.com/docs/grafana/latest/) | [Docker Image](https://hub.docker.com/r/grafana/grafana)
* [Octograph Docker Image](https://hub.docker.com/repository/docker/yansonb/octograph/general)
* [Calorific value](https://www.nationalgas.com/data-and-operations/calorific-value-cv)
* _[Guide to the Octopus API](https://www.guylipman.com/octopus/api_guide.html)_