#!/usr/bin/python

# ravendb_smuggler -- import / export RavenDB databases via .ravendbdump files.
#
# Smuggler is RavenDB's logical export/import tool.  It goes through the public REST API,
# so revisions get re-keyed in the target's PK form on import -- different from a snapshot
# restore (which preserves on-disk binary form).
#
# Two kinds:
#   * export -- POST /databases/<db>/smuggler/export, stream the .ravendbdump back to the
#               controller and write it to dump_path.
#   * import -- POST /databases/<db>/smuggler/import (multipart upload), push the local
#               .ravendbdump into an existing DB.
#
# Distinct from `ravendb_backup` (snapshot/backup/restore -- those preserve on-disk form
# and operate on container-local paths under /backups).

import os
import uuid

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request


def build_multipart(fields, files):
    boundary = "----RavendbSmugglerBoundary" + uuid.uuid4().hex
    boundary_bytes = b"--" + boundary.encode()
    parts = []
    for name, value in fields.items():
        parts.append(boundary_bytes)
        parts.append(b'Content-Disposition: form-data; name="' + name.encode() + b'"')
        parts.append(b"")
        if isinstance(value, str):
            value = value.encode()
        parts.append(value)
    for name, (filename, content, content_type) in files.items():
        parts.append(boundary_bytes)
        disp = 'Content-Disposition: form-data; name="%s"; filename="%s"' % (name, filename)
        parts.append(disp.encode())
        parts.append(("Content-Type: %s" % content_type).encode())
        parts.append(b"")
        parts.append(content)
    parts.append(boundary_bytes + b"--")
    parts.append(b"")
    body = b"\r\n".join(parts)
    return body, "multipart/form-data; boundary=" + boundary


def k_export(p):
    target = p["target"]
    db = p["db_name"]
    dump_path = p["dump_path"]

    if not dump_path:
        raise ValueError("kind=export requires `dump_path`")

    export_path = "/databases/%s/smuggler/export" % db
    status, body_bytes = request("POST", target, p["ravendb_domain"], export_path,
                                 p["client_cert"], p["ca_cert"], body={})
    if status != 200:
        raise RuntimeError("smuggler export returned HTTP %d" % status)

    out_dir = os.path.dirname(dump_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(dump_path, "wb") as f:
        f.write(body_bytes)

    return ("EXPORTED %s/%s -> %s (%d bytes, HTTP 200)" %
            (target, db, dump_path, len(body_bytes)))


def k_import(p):
    target = p["target"]
    db = p["db_name"]
    dump_path = p["dump_path"]
    skip_if_missing = bool(p["skip_if_missing"])

    if not dump_path:
        raise ValueError("kind=import requires `dump_path`")

    if not os.path.isfile(dump_path):
        if skip_if_missing:
            return ("SKIPPED smuggler import: dump file not found at %s "
                    "(skip_if_missing=true)" % dump_path)
        raise ValueError("dump file not found at %s "
                         "(pass skip_if_missing=true to make absence a no-op)" % dump_path)

    with open(dump_path, "rb") as f:
        dump_bytes = f.read()

    body, content_type = build_multipart(
        fields={"importOptions": "{}"},
        files={"file": (os.path.basename(dump_path), dump_bytes, "application/octet-stream")},
    )

    import_path = "/databases/%s/smuggler/import" % db
    status, _ = request("POST", target, p["ravendb_domain"], import_path,
                        p["client_cert"], p["ca_cert"],
                        body=body, content_type=content_type)
    if status != 200:
        raise RuntimeError("smuggler import returned HTTP %d" % status)

    return ("IMPORTED %s -> %s/%s (%d bytes, HTTP 200)" %
            (os.path.basename(dump_path), target, db, len(dump_bytes)))


KINDS = {
    "export": k_export,
    "import": k_import,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        target=dict(required=True),
        db_name=dict(required=True),
        dump_path=dict(type="path", required=True),
        # import only
        skip_if_missing=dict(type="bool", default=False),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
