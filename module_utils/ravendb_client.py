import json
import ssl
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
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
        with urlopen(req, context=ctx, timeout=timeout) as response:
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
