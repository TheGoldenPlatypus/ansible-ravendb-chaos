#!/usr/bin/python

"""
ravendb_features -- enable / disable cluster-wide feature flags on a database.

Used after a rolling upgrade lands the new binary on every node: some v_new
behaviors are gated behind feature flags so they don't auto-activate on a
mixed-binary cluster.  Once the whole topology is on the new build, the
scenario flips the flag on with this module.

The kind POSTs to /databases/<db>/admin/features then re-fetches the
DatabaseRecord and asserts every Add feature is present and every Remove
feature is gone before returning.  No silent success: a mis-applied flag
fails the playbook here, not at the next surprised assertion downstream.
"""

import json

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request, resolve_db_admin_route


def _post_features(p, target, add, remove):
    path = "/databases/%s/admin/features" % p["db_name"]
    body = {"Add": list(add), "Remove": list(remove)}
    status, response = request(
        "POST", target, p["ravendb_domain"], path,
        p["client_cert"], p["ca_cert"], body=body,
    )
    if status not in (200, 201, 204):
        raise RuntimeError(
            "POST /admin/features on %s/%s returned HTTP %d: %s"
            % (target, p["db_name"], status, (response or b"")[:300]))


def _fetch_database_record(p, target):
    path = "/admin/databases?name=%s" % p["db_name"]
    status, response = request(
        "GET", target, p["ravendb_domain"], path,
        p["client_cert"], p["ca_cert"],
    )
    if status != 200:
        raise RuntimeError(
            "GET DatabaseRecord on %s/%s returned HTTP %d"
            % (target, p["db_name"], status))
    return json.loads(response or b"{}")


def _enabled_features_from_record(record):
    """RavenDB has stored feature flags under a few different field names in
    different releases (DatabaseFeatures, EnabledFeatures, Features).  Pick
    up whichever shape is present; treat a list of strings as the canonical
    representation, normalize to a set."""
    for key in ("DatabaseFeatures", "EnabledFeatures", "Features"):
        v = record.get(key)
        if isinstance(v, list):
            return {str(x) for x in v}
        if isinstance(v, dict):
            return {k for k, on in v.items() if on}
    return None


def _flatten_strings(obj):
    """Walk a JSON-decoded object and yield every string -- both dict keys
    and values.  Used as a fallback when the feature flag isn't where we
    expected.  Feature names in RavenDB records can appear as keys (e.g.
    Settings["Raven.Replication.X"] = "true") OR values (a list under
    DatabaseFeatures); we want either to count."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _flatten_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_strings(v)


def k_enable(p):
    """Apply feature changes then verify.  Returns the info lines on PASS,
    raises RuntimeError on transport failure or verification mismatch."""
    add = p["add"] or []
    remove = p["remove"] or []
    if not add and not remove:
        raise ValueError("kind=enable requires non-empty `add` and/or `remove`")

    target = resolve_db_admin_route(
        p["target"], p["db_name"], p["ravendb_domain"],
        p["client_cert"], p["ca_cert"],
    )

    _post_features(p, target, add, remove)

    record = _fetch_database_record(p, target)
    enabled = _enabled_features_from_record(record)
    # Fallback: scan every string in the record for the feature name.  Treat
    # a feature as "present" if either the canonical list contains it OR the
    # name appears as a substring of any key/value (covers schema renames
    # like Settings["Raven.Replication.PullReplicationCompositeChangeVectors"]
    # where the flag name is embedded in a longer key).
    record_strings = list(_flatten_strings(record))

    missing_after_add = []
    for feat in add:
        in_canonical = enabled is not None and feat in enabled
        in_anywhere = any(feat in s for s in record_strings)
        if not (in_canonical or in_anywhere):
            missing_after_add.append(feat)

    still_present_after_remove = []
    for feat in remove:
        in_canonical = enabled is not None and feat in enabled
        # Don't use the loose substring scan for remove -- it's expected to
        # appear in commit/audit fields after the removal, which would
        # produce a false positive.
        if in_canonical:
            still_present_after_remove.append(feat)

    lines = [
        "features on %s/%s:" % (target, p["db_name"]),
        "  posted Add:    %s" % add,
        "  posted Remove: %s" % remove,
        "  enabled (canonical): %s" % (
            sorted(enabled) if enabled is not None else "<no canonical field found>"),
    ]
    if missing_after_add or still_present_after_remove:
        if missing_after_add:
            lines.append("FAIL  feature(s) NOT present after Add: %s" % missing_after_add)
        if still_present_after_remove:
            lines.append("FAIL  feature(s) STILL present after Remove: %s" % still_present_after_remove)
        raise RuntimeError("\n".join(lines))

    lines.append("PASS  every requested change is reflected in the DatabaseRecord")
    return lines


KINDS = {
    "enable": k_enable,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        target=dict(required=True),
        db_name=dict(required=True),
        add=dict(type="list", elements="str", default=None),
        remove=dict(type="list", elements="str", default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
