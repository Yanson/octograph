import json
import aiohttp
from datetime import (datetime, timedelta, time)
from intelligent_dispatches import IntelligentDispatchItem, IntelligentDispatches
from contextlib import suppress
import ciso8601

intelligent_dispatches_query = '''query {{
  plannedDispatches(accountNumber: "{account_id}") {{
    startDt
    endDt
    delta
    meta {{
      source
      location
    }}
  }}
  completedDispatches(accountNumber: "{account_id}") {{
    startDt
    endDt
    delta
    meta {{
      source
      location
    }}
  }}
}}'''

def parse_datetime(dt_str: str) -> dt.datetime | None:
  """Parse a string and return a datetime.datetime.

  This function supports time zone offsets. When the input contains one,
  the output uses a timezone with a fixed offset from UTC.
  Raises ValueError if the input is well formatted but not a valid datetime.
  Returns None if the input isn't well formatted.
  """
  with suppress(ValueError, IndexError):
      return ciso8601.parse_datetime(dt_str)

  if not (match := DATETIME_RE.match(dt_str)):
    return None
  kws: dict[str, Any] = match.groupdict()
  if kws["microsecond"]:
    kws["microsecond"] = kws["microsecond"].ljust(6, "0")
  tzinfo_str = kws.pop("tzinfo")

  tzinfo: dt.tzinfo | None = None
  if tzinfo_str == "Z":
    tzinfo = UTC
  elif tzinfo_str is not None:
    offset_mins = int(tzinfo_str[-2:]) if len(tzinfo_str) > 3 else 0
    offset_hours = int(tzinfo_str[1:3])
    offset = dt.timedelta(hours=offset_hours, minutes=offset_mins)
    if tzinfo_str[0] == "-":
      offset = -offset
    tzinfo = dt.timezone(offset)
  kws = {k: int(v) for k, v in kws.items() if v is not None}
  kws["tzinfo"] = tzinfo
  return dt.datetime(**kws)

def as_utc(dattim: dt.datetime) -> dt.datetime:
  """Return a datetime as UTC time.

  Assumes datetime without tzinfo to be in the DEFAULT_TIME_ZONE.
  """
  if dattim.tzinfo == UTC:
    return dattim
  if dattim.tzinfo is None:
    dattim = dattim.replace(tzinfo=DEFAULT_TIME_ZONE)

  return dattim.astimezone(UTC)

class OctopusEnergyApiClient:
  def __init__(self, api_key, timeout_in_seconds = 15):
    if (api_key is None):
      raise click.ClickException("API KEY is not set")

    self._api_key = api_key
    self._base_url = 'https://api.octopus.energy'

    self._graphql_token = None
    self._graphql_expiration = None

    self.timeout = aiohttp.ClientTimeout(total=timeout_in_seconds)
    self.api_token_query = '''mutation {{
      obtainKrakenToken(input: {{ APIKey: "{api_key}" }}) {{
        token
      }}
    }}'''

  async def async_refresh_token(self):
    """Get the user's refresh token"""
    if (self._graphql_expiration is not None and (self._graphql_expiration - timedelta(minutes=5)) > datetime.now()):
      return

    async with aiohttp.ClientSession(timeout=self.timeout) as client:
      url = f'{self._base_url}/v1/graphql/'
      payload = { "query": self.api_token_query.format(api_key=self._api_key) }
      async with client.post(url, json=payload) as token_response:
        token_response_body = await self.__async_read_response__(token_response, url)
        if (token_response_body is not None and
            "data" in token_response_body and
            "obtainKrakenToken" in token_response_body["data"] and
            token_response_body["data"]["obtainKrakenToken"] is not None and
            "token" in token_response_body["data"]["obtainKrakenToken"]):

          self._graphql_token = token_response_body["data"]["obtainKrakenToken"]["token"]
          self._graphql_expiration = datetime.now() + timedelta(hours=1)
        else:
          raise click.ClickException("Failed to retrieve auth token")

  async def async_get_intelligent_dispatches(self, account_id: str):
    """Get the user's intelligent dispatches"""
    await self.async_refresh_token()

    async with aiohttp.ClientSession(timeout=self.timeout) as client:
      url = f'{self._base_url}/v1/graphql/'
      # Get account response
      payload = { "query": intelligent_dispatches_query.format(account_id=account_id) }
      headers = { "Authorization": f"JWT {self._graphql_token}" }
      async with client.post(url, json=payload, headers=headers) as response:
        response_body = await self.__async_read_response__(response, url)

        if (response_body is not None and "data" in response_body):
          return IntelligentDispatches(
            list(map(lambda ev: IntelligentDispatchItem(
                parse_datetime(ev["startDt"]),
                parse_datetime(ev["endDt"]),
                float(ev["delta"]) if "delta" in ev and ev["delta"] is not None else None,
                ev["meta"]["source"] if "meta" in ev and "source" in ev["meta"] else None,
                ev["meta"]["location"] if "meta" in ev and "location" in ev["meta"] else None,
              ), response_body["data"]["plannedDispatches"]
              if "plannedDispatches" in response_body["data"] and response_body["data"]["plannedDispatches"] is not None
              else [])
            ),
            list(map(lambda ev: IntelligentDispatchItem(
                parse_datetime(ev["startDt"]),
                parse_datetime(ev["endDt"]),
                float(ev["delta"]) if "delta" in ev and ev["delta"] is not None else None,
                ev["meta"]["source"] if "meta" in ev and "source" in ev["meta"] else None,
                ev["meta"]["location"] if "meta" in ev and "location" in ev["meta"] else None,
              ), response_body["data"]["completedDispatches"]
              if "completedDispatches" in response_body["data"] and response_body["data"]["completedDispatches"] is not None
              else [])
            )
          )
        else:
          raise click.ClickException("Failed to retrieve intelligent dispatches")

    return None

  async def __async_read_response__(self, response, url):
    """Reads the response, logging any json errors"""

    text = await response.text()

    if response.status >= 400:
      if response.status >= 500:
        raise click.ClickException(f'Octopus Energy server error ({url}): {response.status}; {text}')
      elif response.status not in [401, 403, 404]:
        raise click.ClickException(f'Failed to send request ({url}): {response.status}; {text}')
      return None

    data_as_json = None
    try:
      data_as_json = json.loads(text)
    except:
      raise click.ClickException(f'Failed to extract response json: {url}; {text}')

    if ("graphql" in url and "errors" in data_as_json):
      raise click.ClickException(f'Errors in request ({url}): {data_as_json["errors"]}')

    return data_as_json
