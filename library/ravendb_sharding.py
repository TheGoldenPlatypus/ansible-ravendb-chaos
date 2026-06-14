#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request


def k_create_sharded(p):
    leader = p["cluster_leader"]
    db = p["db_name"]
    shards = p["shards"]

    if not shards:
        raise ValueError("kind=create_sharded requires `shards` (dict: shard_id -> [node_tags])")

    shard_topology = {}
    for shard_id, members in shards.items():
        shard_topology[str(shard_id)] = {"Members": list(members)}

    body = {
        "DatabaseName": db,
        "Sharding": {
            "Shards": shard_topology,
        },
    }

    path = "/admin/databases?name=%s&replicationFactor=1" % db
    status, resp = request("PUT", leader, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"], body=body)
    if status not in (200, 201):
        raise RuntimeError("create_sharded failed: HTTP %d  body=%s" % (status, resp[:500]))

    s2, b2 = request("GET", leader, p["ravendb_domain"],
                     "/admin/databases?name=%s" % db,
                     p["client_cert"], p["ca_cert"])
    picked = "?"
    if s2 == 200:
        import json as _json
        rec = _json.loads(b2)
        orch_topo = ((rec.get("Sharding") or {}).get("Orchestrator") or {}).get("Topology") or {}
        picked = "members=%s rf=%s" % (
            orch_topo.get("Members"), orch_topo.get("ReplicationFactor"))

    return "SHARDED DB created  %s on %s  shards=%s  orchestrator(auto)=%s" % (
        db, leader, list(shards.keys()), picked)


KINDS = {
    "create_sharded": k_create_sharded,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        cluster_leader=dict(required=True),
        db_name=dict(required=True),
        # create_sharded
        shards=dict(type="dict", default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
