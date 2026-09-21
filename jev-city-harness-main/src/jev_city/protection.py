"""Local formation checks before the leader continues ordinary travel."""

import json
import math
import time
from pathlib import Path


def formation_report(leader, guard_states, minimum=6):
    points = []
    now = time.time()
    for directory in guard_states:
        try:
            status = json.loads((Path(directory) / "status.json").read_text())
            guard = status["self"]
            identity = json.loads((Path(directory) / "identity.json").read_text())["agent"]
        except (OSError, ValueError, KeyError):
            continue
        if (
            guard.get("id") != identity.get("id")
            or not status.get("running")
            or now - status.get("at", 0) > 5
            or guard.get("state") == "dead"
            or guard.get("hp", 0) <= 0
            or guard.get("equipped") in (None, "fists")
            or not leader.get("gangId")
            or guard.get("gangId") != leader["gangId"]
            or guard.get("vehicleId")
        ):
            continue
        dx, dz = guard["x"] - leader["x"], guard["z"] - leader["z"]
        if math.hypot(dx, dz) <= 18:
            points.append((dx, dz))
    if not points:
        return {
            "nearby": 0,
            "required": minimum,
            "centerOffset": None,
            "largestGap": None,
            "covered": False,
        }
    angles = sorted(math.atan2(z, x) for x, z in points)
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 2 * math.pi - angles[-1]]
    offset = math.hypot(
        sum(x for x, z in points) / len(points), sum(z for x, z in points) / len(points)
    )
    gap = max(gaps)
    return {
        "nearby": len(points),
        "required": minimum,
        "centerOffset": round(offset, 2),
        "largestGap": round(gap, 3),
        "covered": len(points) >= minimum and offset <= 5 and gap <= math.pi,
    }


def wait_for_formation(harness):
    config = harness.store.read("protection.json", {})
    if not config.get("enabled"):
        return False
    required = harness.store.read("tactics.json", {}).get("stationWeapon")
    if config.get("prepareWeaponFirst") and (
        (required and required not in harness.obs["self"].get("inventory", []))
        or (not required and harness.obs["self"].get("equipped") in (None, "fists"))
    ):
        return False
    report = formation_report(
        harness.obs["self"], config.get("guardStates", []), config.get("minimum", 6)
    )
    previous = harness.store.read("formation-status.json", {})
    now = time.time()
    waiting = previous.get("waiting", True)
    bad_since = previous.get("badSince")
    resumed_at = previous.get("resumedAt", 0)
    geometrically_covered = report["covered"]
    if waiting:
        waiting = not geometrically_covered
        if not waiting:
            resumed_at, bad_since = now, None
    else:
        # Lag and turns should not repeatedly cancel an otherwise safe route.
        acceptable = (
            report["nearby"] >= report["required"]
            and report["centerOffset"] <= 10
            and report["largestGap"] <= math.pi * 1.4
        )
        if acceptable:
            bad_since = None
        else:
            bad_since = now if bad_since is None else bad_since
            emergency = report["nearby"] < min(3, report["required"])
            waiting = (emergency and now - bad_since >= 1.5) or (
                now - bad_since >= 6 and now - resumed_at >= 12
            )
    report.update(
        at=now,
        waiting=waiting,
        badSince=bad_since,
        resumedAt=resumed_at,
        covered=geometrically_covered,
    )
    harness.store.save("formation-status.json", report)
    if not waiting:
        return False
    if harness.obs["self"].get("motion"):
        harness.action({"action": "stop"})
    harness.next_wait = 1
    return True
