import json
import ssl
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


# Test-only escape hatch: if a target appears in this dict, its mapped URL is
# used VERBATIM instead of composing https://<target>.<domain>:443.  Production
# leaves this empty.  Integration tests (which spin embedded RavenDB servers on
# random ports) populate it like:
#     TARGET_URL_OVERRIDES["1a"] = "http://127.0.0.1:46181"
# When the override is http://, SSL setup is skipped (no cert needed).
TARGET_URL_OVERRIDES: dict = {}


def request(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
    override = TARGET_URL_OVERRIDES.get(target)
    if override:
        url = override.rstrip("/") + path
        ctx = None
        if url.startswith("https://"):
            ctx = ssl.create_default_context(cafile=ca_cert)
            ctx.load_cert_chain(certfile=client_cert)
    else:
        url = "https://" + target + "." + domain + ":443" + path
        ctx = ssl.create_default_context(cafile=ca_cert)
        ctx.load_cert_chain(certfile=client_cert)

    data = None
    headers = {}
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type or "application/json"
    elif isinstance(body, str):
        data = body.encode()
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif isinstance(body, bytes):
        data = body
        headers["Content-Type"] = content_type or "application/octet-stream"

    req = Request(url, data=data, method=method, headers=headers)
    try:
        if ctx is not None:
            with urlopen(req, context=ctx, timeout=timeout) as response:
                return response.status, response.read()
        else:
            with urlopen(req, timeout=timeout) as response:
                return response.status, response.read()
    except HTTPError as e:
        return e.code, e.read()


def request_per_node(method, targets, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
    def call_one(target):
        try:
            status, response = request(method, target, domain, path,
                                       client_cert, ca_cert,
                                       body=body,
                                       content_type=content_type,
                                       timeout=timeout)
            return (target, status, response)
        except Exception as e:
            return (target, None, repr(e))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        for result in pool.map(call_one, targets):
            results.append(result)
    return results
