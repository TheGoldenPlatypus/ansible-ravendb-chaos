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
from ansible.module_utils.ravendb_client import request, resolve_db_admin_route


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

    # Sharded dbs require orchestrator routing -- non-orchestrator members
    # return HTTP 410 DatabaseNotRelevantException.  Non-sharded passes through.
    target = resolve_db_admin_route(target, db, p["ravendb_domain"],
                                    p["client_cert"], p["ca_cert"])

    export_path = "/databases/%s/smuggler/export" % db
    status, body_bytes = request("POST", target, p["ravendb_domain"], export_path,
                                 p["client_cert"], p["ca_cert"], body={})
    if status != 200:
        raise RuntimeError("smuggler export returned HTTP %d" % status)

    if not body_bytes:
        raise RuntimeError(
            "smuggler export: %s/%s returned HTTP 200 with empty body -- "
            "refusing to write a 0-byte dump that would silently pass on import"
            % (target, db))

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

    if not dump_path:
        raise ValueError("kind=import requires `dump_path`")

    # Hard-fail on a missing dump file.  Previous versions accepted a
    # `skip_if_missing=true` escape hatch that turned absence into a soft PASS;
    # that hid configuration mistakes (wrong path, dump never produced) behind
    # a "SKIPPED ..." message.  Scenarios that genuinely need to no-op on
    # absence should gate the call upstream with an explicit `when:` test in
    # the playbook instead of asking the kind to lie about success.
    if not os.path.isfile(dump_path):
        raise FileNotFoundError(
            "smuggler import: dump file not found at %s -- "
            "scenarios must gate this call with an explicit when-clause if "
            "absence is expected; the kind will not claim success on a no-op"
            % dump_path)

    dump_size = os.path.getsize(dump_path)
    if dump_size == 0:
        raise RuntimeError(
            "smuggler import: dump file %s is 0 bytes -- "
            "refusing to import an empty payload that would silently pass"
            % dump_path)

    with open(dump_path, "rb") as f:
        dump_bytes = f.read()

    # Sharded dbs route through orchestrator -- same routing as admin/<db>/.
    target = resolve_db_admin_route(target, db, p["ravendb_domain"],
                                    p["client_cert"], p["ca_cert"])

    body, content_type = build_multipart(
        fields={"importOptions": "{}"},
        files={"file": (os.path.basename(dump_path), dump_bytes, "application/octet-stream")},
    )

    import_path = "/databases/%s/smuggler/import" % db
    status, resp = request("POST", target, p["ravendb_domain"], import_path,
                           p["client_cert"], p["ca_cert"],
                           body=body, content_type=content_type)
    if status != 200:
        raise RuntimeError(
            "smuggler import returned HTTP %d on %s/%s: body=%s"
            % (status, target, db, (resp or b"")[:300]))

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
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
