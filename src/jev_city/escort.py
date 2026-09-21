"""One explicitly enrolled bodyguard; deterministic formation and threat response."""

import fcntl
import json
import math
import time
from pathlib import Path

from .controller import Harness
from .settings import Settings
from .storage import Store
from .transport import API


class Escort(Harness):
    def __init__(
        self,
        settings,
        store,
        api,
        control,
        leader_state,
        squad_states=(),
        engage_on_sight=False,
        required_weapon="sidearm",
        screen_distance=None,
        formation="screen",
        circle_radius=10,
    ):
        super().__init__(settings, store, api, control)
        self.leader_store = Store(Path(leader_state).expanduser().resolve())
        self.squad_stores = [self.leader_store, store] + [
            Store(Path(p).expanduser().resolve()) for p in squad_states
        ]
        self.engage_on_sight = engage_on_sight
        guard_paths = sorted({str(store.root), *(str(s.root) for s in self.squad_stores[2:])})
        if formation not in {"screen", "circle"} or not 5 <= circle_radius <= 20:
            raise ValueError("Use screen or circle formation with radius 5–20 metres")
        self.formation = formation
        self.circle_radius = circle_radius
        self.slot_index = guard_paths.index(str(store.root))
        self.slot_count = len(guard_paths)
        self.ring_variant = 0
        self.ring_retry_at = 0
        if required_weapon not in {w["id"] for w in self.catalog["weapons"]}:
            raise ValueError("Required weapon is missing from the live catalog")
        self.required_weapon = required_weapon
        self.advance = (
            screen_distance
            if screen_distance is not None
            else min(35, 10 + 6 * guard_paths.index(str(store.root)))
        )
        if not 5 <= self.advance <= 35:
            raise ValueError("Screen distance must be between 5 and 35 metres")
        self.last_heading = None
        self.obstructed = {}
        self.membership_at = 0
        self.follow_at = 0
        self.follow_point = None
        self.mem.update(focusCombat=True, desiredWeapon=required_weapon, humanMode=None)

    def leader(self):
        identity = self.leader_store.read("identity.json", {}).get("agent", {})
        status = self.leader_store.read("status.json", {})
        current = status.get("self", {})
        if (
            current.get("id") != identity.get("id")
            or time.time() - status.get("at", 0) > 12
            or current.get("state") == "dead"
        ):
            return None
        return current

    def membership(self):
        """Only managed identities may receive invitations; never arbitrary game messages."""
        leader_id = self.leader_store.read("identity.json", {}).get("agent", {}).get("id")
        members = (self.obs.get("gang") or {}).get("members", [])
        if any(m.get("id", m.get("agentId")) == leader_id for m in members):
            return True
        if time.time() - self.membership_at < 5:
            return False
        self.membership_at = time.time()
        gangs = self.api.request("/api/gangs", auth=False).get("gangs", [])
        managed = {
            s.read("identity.json", {}).get("agent", {}).get("id"): s for s in self.squad_stores
        }
        gang = next((g for g in gangs if any(m.get("id") in managed for m in g["members"])), None)
        if not gang:
            self.store.log("escort_wait", reason="Create a shared gang before escorting")
            return False
        owner = managed.get(gang.get("leaderId"))
        if not owner:
            self.store.log("escort_wait", reason="Gang leader is not managed by this squad")
            return False
        ids = {m["id"] for m in gang["members"]}
        owner_api = API(Settings(owner.root, base_url=self.settings.base_url), owner, self.control)
        # The squad's oldest living member can invite a replacement leader back.
        for member_id in (leader_id, self.obs["self"]["id"]):
            if not member_id or member_id in ids:
                continue
            target_store = managed[member_id]
            target_api = (
                self.api
                if target_store.root == self.store.root
                else API(
                    Settings(target_store.root, base_url=self.settings.base_url),
                    target_store,
                    self.control,
                )
            )
            import uuid

            invited = owner_api.request(
                "/api/action",
                {"action": "gang_invite", "targetId": member_id, "requestId": str(uuid.uuid4())},
            )
            if invited.get("ok"):
                joined = target_api.request(
                    "/api/action",
                    {"action": "gang_join", "gangId": gang["id"], "requestId": str(uuid.uuid4())},
                )
                self.store.log("escort_enrollment", memberId=member_id, ok=bool(joined.get("ok")))
        # Verify membership on the next observation before movement or combat.
        return False

    def circle_point(self, leader):
        # Stable world-relative sectors avoid guards crossing each other whenever
        # the leader turns. Small radial variation prevents a rigid parade ring.
        angle = 2 * math.pi * self.slot_index / self.slot_count
        radius = self.circle_radius + 0.4 * math.sin(self.slot_index * 2.3)
        if self.ring_variant and time.time() >= self.ring_retry_at:
            self.ring_variant = 0
        variants = [(0, 1), (0.18, 0.75), (-0.18, 0.75), (0, 1.2)]
        offset, scale = variants[self.ring_variant % len(variants)]
        return {
            "x": leader["x"] + math.cos(angle + offset) * radius * scale,
            "z": leader["z"] + math.sin(angle + offset) * radius * scale,
        }

    def formation_point(self, leader):
        """Project onto the remaining server route, then lead by a distinct distance."""
        if self.formation == "circle":
            point = self.circle_point(leader)
            if self.store.read("tactics.json", {}).get("rangedAssault"):
                target_id = self.leader_store.read("harness-state.json", {}).get("targetId")
                target = next(
                    (
                        a
                        for a in self.obs.get("nearby", [])
                        if a["id"] == target_id and self.eligible_opponent(a)
                    ),
                    None,
                )
                if target:
                    weapon = next(
                        (
                            w
                            for w in self.catalog["weapons"]
                            if w["id"] == self.obs["self"].get("equipped")
                        ),
                        {},
                    )
                    enemy = next(
                        (w for w in self.catalog["weapons"] if w["id"] == target.get("equipped")),
                        {},
                    )
                    minimum = enemy.get("range", 2) + 10
                    dx, dz = point["x"] - target["x"], point["z"] - target["z"]
                    distance = math.hypot(dx, dz)
                    if weapon.get("range", 2) <= minimum:
                        dx, dz = leader["x"] - target["x"], leader["z"] - target["z"]
                        distance = math.hypot(dx, dz)
                        desired = max(distance + 8, minimum + 8)
                    else:
                        desired = max(minimum, min(distance, weapon["range"] * 0.93))
                    if distance > 0.1:
                        point = {
                            "x": target["x"] + dx / distance * desired,
                            "z": target["z"] + dz / distance * desired,
                        }
            return point
        motion = leader.get("motion") or {}
        path = [motion.get("origin", leader), *motion.get("path", [])]
        if len(path) > 1:
            segments = []
            for i, (a, b) in enumerate(zip(path, path[1:])):
                dx, dz = b["x"] - a["x"], b["z"] - a["z"]
                length = math.hypot(dx, dz)
                if not length:
                    continue
                t = max(
                    0,
                    min(1, ((leader["x"] - a["x"]) * dx + (leader["z"] - a["z"]) * dz) / length**2),
                )
                point = {"x": a["x"] + t * dx, "z": a["z"] + t * dz}
                segments.append(
                    (math.hypot(point["x"] - leader["x"], point["z"] - leader["z"]), i, point)
                )
            if segments:
                _, index, point = min(segments, key=lambda row: (row[0], -row[1]))
                remaining = self.advance
                for end in path[index + 1 :]:
                    dx, dz = end["x"] - point["x"], end["z"] - point["z"]
                    length = math.hypot(dx, dz)
                    if length:
                        self.last_heading = (dx / length, dz / length)
                        if remaining <= length:
                            return {
                                "x": point["x"] + dx * remaining / length,
                                "z": point["z"] + dz * remaining / length,
                            }
                        remaining -= length
                    point = end
                return {"x": point["x"], "z": point["z"]}
        heading = self.last_heading
        if heading is None and "angle" in leader:
            heading = (math.sin(leader["angle"]), math.cos(leader["angle"]))
        if heading:
            return {
                "x": leader["x"] + heading[0] * self.advance,
                "z": leader["z"] + heading[1] * self.advance,
            }
        return {"x": leader["x"], "z": leader["z"]}

    def engage(self, leader):
        s, now = self.obs["self"], self.obs["serverTime"]
        weapon = next((w for w in self.catalog["weapons"] if w["id"] == s.get("equipped")), None)
        if not weapon or s.get("vehicleId") or self.dist(leader) > 45:
            return False
        friendly = self.friendly_ids()
        friendly.update({s["id"], leader["id"]})
        threat = leader.get("lastThreat") or {}
        shooter = threat.get("attackerId") if now - threat.get("at", 0) < 15000 else None
        leader_target = self.leader_store.read("harness-state.json", {}).get("targetId")
        candidates = [
            a
            for a in self.obs.get("nearby", [])
            if self.eligible_opponent(a)
            and a.get("state") != "dead"
            and a["id"] not in friendly
            and not a.get("vehicleId")
            and now >= a.get("protectedUntil", 0)
            and (self.engage_on_sight or a["id"] == shooter)
            and self.obstructed.get(a["id"], 0) <= time.time()
            and self.dist(a) <= weapon["range"]
        ]
        if not candidates:
            return False
        target = min(
            candidates,
            key=lambda a: (
                a["id"] != shooter,
                a["id"] != leader_target,
                a.get("hp", 100),
                self.dist(a),
            ),
        )
        if self.store.read("tactics.json", {}).get("rangedAssault"):
            enemy = next(
                (w for w in self.catalog["weapons"] if w["id"] == target.get("equipped")), {}
            )
            if weapon["range"] <= enemy.get("range", 2) + 8:
                return False
            if self.hold_range(target, weapon):
                return True
        self.mem.update(plan="screen-engage", targetId=target["id"], targetName=target["name"])
        self.persist()
        if s.get("combatResponse") != "hold":
            self.action({"action": "set_combat_response", "mode": "hold"})
        elif s.get("motion"):
            self.action({"action": "stop"})
        elif now >= s.get("lastCombat", 0) + weapon["cooldownMs"]:
            result = self.action({"action": "attack", "targetId": target["id"]})
            if not result.get("ok"):
                # The server is authoritative for walls. Resume screening instead of
                # repeatedly shooting through the same obstruction or chasing away.
                self.obstructed[target["id"]] = time.time() + 4
        self.next_wait = 1
        return True

    def recover(self):
        s = self.obs["self"]
        hp = s.get("hp", 100)
        if hp >= 100:
            if self.mem.pop("guardHealing", None):
                self.persist()
            return False
        if not self.mem.get("guardHealing"):
            if hp > 65:
                return False
            # Admission is shared across processes, separately from controller locks.
            with self.leader_store.path("healing.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                healing = 0
                for store in self.squad_stores[2:]:
                    status = store.read("status.json", {})
                    if (
                        time.time() - status.get("at", 0) < 5
                        and status.get("self", {}).get("state") != "dead"
                        and store.read("harness-state.json", {}).get("guardHealing")
                    ):
                        healing += 1
                if hp > 30 and healing >= 2:
                    return False
                self.mem["guardHealing"] = True
                self.persist()
                self.store.log("guard_recovery", hp=hp, emergency=hp <= 30)
        self.mem["plan"] = "guard-heal"
        self.persist()
        hospital = self.treatment()
        point = hospital["point"]
        if self.dist(point) <= hospital["radius"]:
            if s.get("motion"):
                self.action({"action": "stop"})
            elif self.obs["serverTime"] >= s.get("lastCombat", 0) + 10000:
                action = {"action": "heal"}
                if self.blocked.get(json.dumps(action, sort_keys=True), 0) <= time.time():
                    self.action(action)
        else:
            motion = s.get("motion") or {}
            destination = (motion.get("path") or [{}])[-1]
            if (
                not s.get("motion")
                or math.hypot(
                    destination.get("x", 1e6) - point["x"], destination.get("z", 1e6) - point["z"]
                )
                > 2
            ):
                self.move(point, gait="run")
        self.next_wait = 1
        return True

    def step(self):
        s = self.obs["self"]
        if s["state"] == "dead":
            self.replace()
            return
        if not self.membership():
            self.next_wait = 1
            return
        leader = self.leader()
        if self.defend():
            return
        if self.recover():
            return
        if self.store.read("tactics.json", {}).get("workAndFight"):
            if self.mem.get("humanMode") != "farm_combat":
                self.mem.update(
                    humanMode="farm_combat",
                    focusCombat=False,
                    goalComplete=True,
                    plan=None,
                    targetId=None,
                    targetName=None,
                )
                self.persist()
            super().step()
            return
        if self.station_patrol():
            return
        required = self.store.read("tactics.json", {}).get("stationWeapon", self.required_weapon)
        equipped_for_role = required in s.get("inventory", []) or (
            required == "sidearm"
            and any(w["id"] in s.get("inventory", []) for w in self.catalog["weapons"])
        )
        if not equipped_for_role:
            self.mem.update(goalComplete=False, targetId=None, plan=None)
            super().step()
            return
        self.mem.update(goalComplete=True, plan="screen", targetId=None, targetName=None)
        if not leader:
            if s.get("motion"):
                self.action({"action": "stop"})
            self.next_wait = 1
            return
        if self.engage(leader):
            return
        if s.get("combatResponse", "flee") != "flee":
            self.action({"action": "set_combat_response", "mode": "flee"})
            return
        point = self.formation_point(leader)
        self.mem["formation"] = {
            "destination": point,
            "aheadMetres": self.advance if self.formation == "screen" else None,
            "style": self.formation,
            "radius": self.circle_radius if self.formation == "circle" else None,
            "slot": self.slot_index,
            "slots": self.slot_count,
            "engageOnSight": self.engage_on_sight,
        }
        self.persist()
        if self.dist(point) <= 2.5:
            if s.get("motion") and not leader.get("motion"):
                self.action({"action": "stop"})
            self.next_wait = 1
            return
        shifted = (
            not self.follow_point
            or math.hypot(point["x"] - self.follow_point[0], point["z"] - self.follow_point[1]) > 5
        )
        if time.time() - self.follow_at >= 1 and (not s.get("motion") or shifted):
            self.follow_at = time.time()
            gait = (
                "run"
                if (leader.get("motion") and self.dist(point) > 4) or self.dist(point) > 9
                else "walk"
            )
            move = {"action": "move", "gait": gait, **point}
            if (
                self.formation != "circle"
                and self.blocked.get(json.dumps(move, sort_keys=True), 0) > time.time()
            ):
                point = {"x": leader["x"], "z": leader["z"]}
            result = self.move(point, gait=gait)
            if result.get("ok"):
                self.follow_point = (point["x"], point["z"])
            elif self.formation == "circle":
                self.ring_variant = (self.ring_variant + 1) % 4
                self.ring_retry_at = time.time() + 15
        self.next_wait = 1
