"""Explicit enrollment of already registered, locally managed characters."""

import uuid
from pathlib import Path

from .settings import Settings
from .storage import Store
from .transport import API


def enroll(settings, leader_store, leader_api, control, guard_states, name, tag, color):
    stores = [Store(Path(p).expanduser().resolve()) for p in guard_states]
    identities = [s.read("identity.json") for s in [leader_store, *stores]]
    if any(not i or not i.get("token") for i in identities):
        raise ValueError("Register each distinct character before squad enrollment")
    ids = [i["agent"]["id"] for i in identities]
    if len(set(ids)) != len(ids):
        raise ValueError("Squad members must be distinct saved characters")

    def action(api, **data):
        result = api.request("/api/action", dict(data, requestId=str(uuid.uuid4())))
        if not result.get("ok"):
            raise RuntimeError(str(leader_store.redact(result)))
        return result

    gangs = leader_api.request("/api/gangs", auth=False)["gangs"]
    gang = next((g for g in gangs if any(m["id"] == ids[0] for m in g["members"])), None)
    if not gang:
        action(leader_api, action="gang_create", name=name, tag=tag, color=color)
        gangs = leader_api.request("/api/gangs", auth=False)["gangs"]
        gang = next(g for g in gangs if g["leaderId"] == ids[0])
    if gang["leaderId"] != ids[0]:
        raise ValueError("The selected character must lead the gang to enroll guards")
    result = []
    for store, identity in zip(stores, identities[1:], strict=True):
        api = API(Settings(store.root, base_url=settings.base_url), store, control)
        obs = api.request("/api/observe?catalog=0")
        if obs["self"]["state"] == "dead":
            raise ValueError("A guard is permanently dead; replace it before enrollment")
        if obs.get("gang") and obs["gang"]["id"] != gang["id"]:
            raise ValueError("A guard already belongs to another gang")
        if not obs.get("gang"):
            action(leader_api, action="gang_invite", targetId=identity["agent"]["id"])
            action(api, action="gang_join", gangId=gang["id"])
            obs = api.request("/api/observe?catalog=0&since=" + str(obs.get("nextEventId", 0)))
        if (obs.get("gang") or {}).get("id") != gang["id"]:
            raise RuntimeError("Could not verify shared gang membership")
        result.append({"name": identity["agent"]["name"], "gang": gang["name"]})
    return result
