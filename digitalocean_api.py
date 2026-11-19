"""Small DigitalOcean HTTP boundary: bounded requests, safe pagination, no POST retries."""
from urllib.parse import urlsplit
import ipaddress
import requests


class DigitalOceanAPIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'DigitalOcean API returned HTTP {status}')


class DigitalOceanAPI:
    BASE = 'https://api.digitalocean.com'

    def __init__(self, token, session=None, *, base_url=None):
        if not token:
            raise ValueError('DIGITALOCEAN_TOKEN is required')
        self.session = session or requests.Session()
        self.base_url = (base_url or self.BASE).rstrip('/')
        parsed = urlsplit(self.base_url)
        if self.base_url != self.BASE:
            try:
                local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = False
            if not local or parsed.scheme != 'http' or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError('Alternate DigitalOcean endpoints must be loopback HTTP test servers')
        self.headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    def request(self, method, path, *, body=None, params=None, missing_ok=False):
        if not path.startswith('/v2/') or urlsplit(path).netloc:
            raise ValueError('Invalid DigitalOcean API path')
        try:
            response = self.session.request(method, self.base_url + path, headers=self.headers,
                json=body, params=params, timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            # requests exceptions can retain credential-bearing request bodies.
            raise RuntimeError('DigitalOcean transport failed; submission outcome may be unknown') from None
        if response.status_code == 404 and missing_ok:
            return None
        if not 200 <= response.status_code < 300:
            raise DigitalOceanAPIError(response.status_code)
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            raise RuntimeError('DigitalOcean returned invalid JSON') from None

    def pages(self, path, key, **params):
        seen = set()
        current, query = path, {'per_page': 200, **params}
        while current:
            if current in seen:
                raise RuntimeError('DigitalOcean pagination loop')
            seen.add(current)
            data = self.request('GET', current, params=query)
            items = data.get(key)
            if not isinstance(items, list):
                raise RuntimeError('DigitalOcean inventory response is incomplete')
            yield from items
            next_url = data.get('links', {}).get('pages', {}).get('next')
            if not next_url:
                break
            parsed = urlsplit(next_url)
            expected = urlsplit(self.base_url)
            if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc
                    or parsed.path != path or parsed.fragment):
                raise RuntimeError('Refusing unexpected DigitalOcean pagination URL')
            current = parsed.path + ('?' + parsed.query if parsed.query else '')
            query = None
