from app.octopus_api_client import OctopusApiClient


def test_to_url_relative_path():
    client = OctopusApiClient('https://api.octopus.energy/v1', 'key')
    assert client._to_url('products/X/') == 'https://api.octopus.energy/v1/products/X/'


def test_to_url_absolute_same_host():
    client = OctopusApiClient('https://api.octopus.energy/v1', 'key')
    url = 'https://api.octopus.energy/v1/products/X/'
    assert client._to_url(url) == url


def test_to_url_absolute_different_host():
    client = OctopusApiClient('https://api.octopus.energy/v1', 'key')
    url = 'https://api.oegb-kraken.energy/v1/products/X/electricity-tariffs/E-1R-X-A/standing-charges/'
    assert client._to_url(url) == url
