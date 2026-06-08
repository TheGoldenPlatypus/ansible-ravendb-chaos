#!/usr/bin/python

import datetime
import json
import uuid
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request


def k_docs(p):
    target = p["target"]
    db = p["db_name"]
    prefix = p["id_prefix"] or "micro/doc"
    count = p["count"]
    body_tag = p["body_tag"]

    ok = 0
    for n in range(count):
        doc_id = "%s/%d" % (prefix, n)
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        body = {"@metadata": {"@collection": "MicroDocs"}}
        # body_tag = mark each write with its origin so concurrent writers on the same
        # docId from different nodes produce DISTINCT content
        if body_tag:
            body["tag"] = body_tag
        status, _ = request("PUT", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=body)
        if status in (200, 201):
            ok += 1

    suffix = ", body_tag='%s'" % body_tag if body_tag else ""
    return "WROTE %d/%d docs to %s/%s (id prefix '%s'%s)" % (
        ok, count, target, db, prefix, suffix)


def k_docs_freeform(p):
    target = p["target"]
    db = p["db_name"]
    count = p["count"]

    ok = 0
    for n in range(count):
        doc_id = uuid.uuid4().hex
        path = "/databases/%s/docs?id=%s" % (db, doc_id)
        body = {
            "Name": "freeform-%d-on-%s" % (n, target),
            "@metadata": {"@collection": None},
        }
        status, _ = request("PUT", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=body)
        if status in (200, 201):
            ok += 1

    return "WROTE %d/%d freeform docs (random uuid ids) to %s/%s" % (
        ok, count, target, db)


def k_docs_revisions(p):
    target = p["target"]
    db = p["db_name"]
    count = p["count"]
    revs = p["revs_per_doc"]
    prefix = p["id_prefix"] or "seed"
    collection = p["collection"] or "MicroDocs"
    total = count * revs

    ok = 0
    for v in range(1, revs + 1):
        for n in range(count):
            doc_id = "%s/%d" % (prefix, n)
            path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
            body = {
                "v": v,
                "src": "write_docs_revisions",
                "@metadata": {"@collection": collection},
            }
            status, _ = request("PUT", target, p["ravendb_domain"], path,
                                p["client_cert"], p["ca_cert"], body=body)
            if status in (200, 201):
                ok += 1

    return ("WROTE %d/%d PUTs to %s/%s "
            "(%d docs x %d revisions each, id prefix '%s', collection '%s')" % (
        ok, total, target, db, count, revs, prefix, collection))


def k_docs_interleaved(p):
    target = p["target"]
    db = p["db_name"]
    count = p["count"]
    prefixes = p["prefixes"]
    if not prefixes:
        raise ValueError("kind=docs_interleaved requires `prefixes` (non-empty list)")

    ok = 0
    for n in range(count):
        which = prefixes[n % len(prefixes)]
        seq = n // len(prefixes)
        doc_id = "%s-%d" % (which, seq)
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        body = {"@metadata": {"@collection": "MicroDocs"}}
        status, _ = request("PUT", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=body)
        if status in (200, 201):
            ok += 1

    return "WROTE %d/%d docs to %s/%s (round-robin across prefixes %s)" % (
        ok, count, target, db, prefixes)


def k_attachments(p):
    target = p["target"]
    db = p["db_name"]
    count = p["count"]
    doc_prefix = p["doc_id_prefix"] or "micro/doc"
    att_name = p["attachment_name"] or "data"

    ok = 0
    for n in range(count):
        doc_id = "%s/%d" % (doc_prefix, n)
        name = "%s/%d" % (att_name, n)
        payload = p["payload"] if p["payload"] is not None else "blob-%d" % n
        path = "/databases/%s/attachments?id=%s&name=%s" % (
            db, quote(doc_id), quote(name))
        status, _ = request("PUT", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=payload)
        if status in (200, 201, 204):
            ok += 1

    return ("WROTE %d/%d attachments to %s/%s "
            "(docs '%s/0..%d', names '%s/0..%d')" % (
        ok, count, target, db,
        doc_prefix, count - 1, att_name, count - 1))


def k_counters(p):
    target = p["target"]
    db = p["db_name"]
    doc_id = p["doc_id"]
    name = p["counter_name"] or "Likes"
    delta = p["delta"]
    repeat = p["repeat"]

    body = {"Documents": [{
        "DocumentId": doc_id,
        "Operations": [{
            "Type": "Increment",
            "CounterName": name,
            "Delta": delta,
        }],
    }]}
    path = "/databases/%s/counters" % db

    ok = 0
    for _ in range(repeat):
        status, _b = request("POST", target, p["ravendb_domain"], path,
                             p["client_cert"], p["ca_cert"], body=body)
        if status in (200, 201, 204):
            ok += 1

    total_change = delta * repeat
    return ("INCREMENTED counter '%s' on %s by %+d "
            "(%d call(s) of delta %d, total change %+d) on %s/%s" % (
        name, doc_id, total_change, repeat, delta, total_change, target, db))


def k_timeseries(p):
    target = p["target"]
    db = p["db_name"]
    doc_id = p["doc_id"]
    name = p["ts_name"] or "Heartrate"

    # Delete-range mode.
    if p["delete_from"] and p["delete_to"]:
        body = {"Name": name, "Deletes": [{
            "From": p["delete_from"],
            "To": p["delete_to"],
        }]}
        path = "/databases/%s/timeseries?docId=%s" % (db, quote(doc_id))
        status, _ = request("POST", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=body)
        return ("DELETED timeseries '%s' range [%s .. %s] on doc %s "
                "(HTTP %d) on %s/%s" % (
            name, p["delete_from"], p["delete_to"], doc_id, status, target, db))

    # Append mode.
    if p["start_timestamp"]:
        start = datetime.datetime.strptime(
            p["start_timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
    else:
        start = datetime.datetime.utcnow() - datetime.timedelta(hours=1)

    count = p["count"]
    interval = p["interval_seconds"]
    path = "/databases/%s/timeseries?docId=%s" % (db, quote(doc_id))

    ok = 0
    for n in range(count):
        ts = start + datetime.timedelta(seconds=n * interval)
        body = {"Name": name, "Appends": [{
            "Timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "Values": [72.0],
            "Tag": None,
        }]}
        status, _ = request("POST", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=body)
        if status in (200, 201, 204):
            ok += 1

    return ("APPENDED %d/%d timeseries '%s' entries to doc %s "
            "(every %ds starting %s) on %s/%s" % (
        ok, count, name, doc_id, interval,
        start.strftime("%Y-%m-%dT%H:%M:%SZ"), target, db))


def k_delete(p):
    target = p["target"]
    db = p["db_name"]

    if p["ids"]:
        ids = p["ids"]
    elif p["id_prefix"] and p["count"]:
        ids = []
        for n in range(p["count"]):
            ids.append("%s/%d" % (p["id_prefix"], n))
    else:
        raise ValueError("kind=delete requires `ids` OR (`id_prefix` and `count`)")

    ok = 0
    errors = []
    for doc_id in ids:
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        status, _ = request("DELETE", target, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"])
        if status == 204:
            ok += 1
        else:
            errors.append("%s -> HTTP %s" % (doc_id, status))

    if errors:
        return ("DELETE on %s/%s: %d/%d returned 204; %d unexpected: %s" %
                (target, db, ok, len(ids), len(errors), errors))
    return "DELETE on %s/%s: %d/%d returned 204 (Raven does not distinguish missing vs deleted)" % (
        target, db, ok, len(ids))


def k_restore_revision(p):
    target = p["target"]
    db = p["db_name"]
    doc_id = p["doc_id"]
    revision_cv = p["revision_cv"]

    cv_encoded = quote(revision_cv, safe="")
    get_path = "/databases/%s/revisions?changeVector=%s" % (db, cv_encoded)
    status, body_bytes = request("GET", target, p["ravendb_domain"], get_path,
                                 p["client_cert"], p["ca_cert"])
    results = json.loads(body_bytes).get("Results", [])
    if not results:
        raise ValueError("no revision found for cv=%s on %s" % (revision_cv, doc_id))
    revision_body = results[0]

    put_path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
    put_status, _ = request("PUT", target, p["ravendb_domain"], put_path,
                            p["client_cert"], p["ca_cert"], body=revision_body)

    return "RESTORED revision %s as live doc %s on %s/%s (PUT HTTP %d)" % (
        revision_cv, doc_id, target, db, put_status)


KINDS = {
    "docs":             k_docs,
    "docs_freeform":    k_docs_freeform,
    "docs_revisions":   k_docs_revisions,
    "docs_interleaved": k_docs_interleaved,
    "attachments":      k_attachments,
    "counters":         k_counters,
    "timeseries":       k_timeseries,
    "delete":           k_delete,
    "restore_revision": k_restore_revision,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        target=dict(required=True),
        db_name=dict(required=True),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        # docs / freeform / interleaved
        count=dict(type="int", default=0),
        id_prefix=dict(default=None),
        prefixes=dict(type="list", elements="str", default=None),
        body_tag=dict(default=None),
        # docs_revisions
        revs_per_doc=dict(type="int", default=1),
        collection=dict(default=None),
        # attachments
        doc_id_prefix=dict(default=None),
        attachment_name=dict(default=None),
        payload=dict(default=None),
        # counters / timeseries / restore_revision
        doc_id=dict(default=None),
        counter_name=dict(default=None),
        delta=dict(type="int", default=1),
        repeat=dict(type="int", default=1),
        ts_name=dict(default=None),
        start_timestamp=dict(default=None),
        interval_seconds=dict(type="int", default=6),
        delete_from=dict(default=None),
        delete_to=dict(default=None),
        # delete
        ids=dict(type="list", elements="str", default=None),
        # restore_revision
        revision_cv=dict(default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
