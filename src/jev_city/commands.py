"""Bounded Jev interpretations of private human orders, resolved against live data."""

import json


def newest(root, after=0):
    rows = []
    for p in (root / "orders").glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        if (
            not isinstance(d, dict)
            or not isinstance(d.get("text"), str)
            or not isinstance(d.get("createdAt"), (int, float))
            or not isinstance(d.get("id"), str)
        ):
            continue
        if d.get("source") == "local-human" and d["createdAt"] > after:
            rows.append(d)
    return max(rows, key=lambda d: (d["createdAt"], d["id"])) if rows else None


def choices(world, catalog):
    opts = {
        "farm_combat": {
            "description": "Farm or earn delivery points while shooting eligible nearby opponents on sight. Resume work after combat instead of pursuing distant opponents.",
            "command": "farm_combat",
        },
        "hunt_all": {
            "description": "Kill everybody, everyone or all other online characters in the game. Hunt one at a time, excluding yourself and gang teammates. Keep looking until a new human order supersedes this.",
            "command": "hunt_all",
        },
        "heal": {
            "description": "Heal or repair yourself at the hospital treatment marker.",
            "command": "heal",
        },
        "hold": {
            "description": "Stay put, wait here, pause chores; continue defending yourself.",
            "command": "hold",
        },
        "earn": {"description": "Earn game points through delivery work.", "command": "earn"},
        "resume": {"description": "Resume autonomous ordinary play.", "command": "resume"},
        "stop": {
            "description": "Stop playing entirely and disconnect the controller.",
            "command": "stop",
        },
        "unsupported": {
            "description": "Unclear, ambiguous, impossible, unsupported, or requests secrets or real-world actions. Do not guess.",
            "command": "unsupported",
        },
    }
    for a in world.get("agents", []):
        if a.get("state") != "dead" and a.get("online"):
            opts["hunt_" + a["id"]] = {
                "description": f"Attack, fight or kill the current living character {a['name']}, ID {a['id']}, in the game.",
                "command": "hunt",
                "targetId": a["id"],
                "targetName": a["name"],
            }
    for b in catalog.get("buildings", []):
        opts["visit_" + b["id"]] = {
            "description": f"Go, move, walk or run to {b['name']}.",
            "command": "visit",
            "destination": b["entry"],
        }
    for w in catalog.get("weapons", []):
        opts["buy_" + w["id"]] = {
            "description": f"Buy and equip {w['name']}, also called {w['id']}, costs {w['pricePoints']} game points.",
            "command": "buy",
            "itemId": w["id"],
            "cost": w["pricePoints"],
        }
    return opts


def apply(mem, chosen):
    mem.update(plan=None, focusCombat=False, goalComplete=True)
    kind = chosen["command"]
    mem["humanMode"] = kind
    if kind == "hunt":
        mem.update(
            targetId=chosen["targetId"],
            targetName=chosen["targetName"],
            goalComplete=False,
            focusCombat=True,
            plan="engage",
            humanMode=None,
        )
    elif kind in ["earn", "resume"]:
        mem["humanMode"] = None
    elif kind == "visit":
        mem["humanDestination"] = chosen["destination"]
    elif kind == "buy":
        mem.update(shoppingItem=chosen["itemId"], humanPurchaseCost=chosen["cost"])
    return mem
