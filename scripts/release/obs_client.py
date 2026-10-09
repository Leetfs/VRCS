"""Small OBS API client; credentials remain in the caller's environment."""
import base64
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class Client:
    def __init__(self):
        self.api = os.getenv("OBS_API_URL", "https://api.opensuse.org").rstrip("/")
        if not self.api.startswith("https://"):
            raise ValueError("OBS API must use HTTPS")
        username, password = os.environ["OBS_USERNAME"], os.environ["OBS_PASSWORD"]
        if not username or not password:
            raise ValueError("OBS credentials are missing")
        self.authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def request(self, path, method="GET", data=None, content_type="application/octet-stream"):
        if isinstance(data, str):
            data = data.encode()
        for attempt in range(5):
            headers = {"Authorization": self.authorization, "Content-Type": content_type}
            if data is not None:
                headers["Content-Length"] = str(len(data))
            body = data
            if data is not None and len(data) > 1024 * 1024:
                body = (memoryview(data)[n:n + 65536] for n in range(0, len(data), 65536))
            request = urllib.request.Request(self.api + path, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == 4:
                    # Do not echo headers or arbitrary authentication error bodies.
                    raise RuntimeError(f"OBS {method} {path}: HTTP {error.code}") from error
            except (urllib.error.URLError, OSError):
                if attempt == 4:
                    raise
            time.sleep(2 ** attempt)


def path(*parts):
    return "/" + "/".join(urllib.parse.quote(str(p), safe="") for p in parts)


def query(**parameters):
    return "?" + urllib.parse.urlencode(parameters)
