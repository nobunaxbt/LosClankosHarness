"""Combat behavior for the single-character controller."""

import json
import math
import time
from pathlib import Path


class CombatMixin:
    def friendly_ids(self):
        ids = {
            m.get("id", m.get("agentId")) for m in (self.obs.get("gang") or {}).get("members", [])
        }
        ids.add(self.obs.get("self", {}).get("id"))
        for directory in self.store.read("allies.json", {}).get("stateDirs", []):
            try:
                identity = json.loads((Path(directory) / "identity.json").read_text())
                ids.add(identity["agent"]["id"])
            except (OSError, ValueError, KeyError):
                continue
        return ids

    def eligible_opponent(self, agent):
        target_gang = self.store.read("tactics.json", {}).get("targetGangId")
        return (
            (not target_gang or agent.get("gangId") == target_gang)
            and agent.get("online")
            and agent.get("state") != "dead"
            and agent.get("id") not in self.friendly_ids()
            and (
                not self.store.read("tactics.json", {}).get("gangOpponentsOnly")
                or bool(agent.get("gangId"))
            )
        )

    def station_patrol(self):
        tactics = self.store.read("tactics.json", {})
        station = tactics.get("station")
        if not station:
            return False
        required = tactics.get("stationWeapon", "sidearm")
        if required not in self.obs["self"].get("inventory", []):
            # A replacement must earn equipment before resuming its saved deployment.
            self.mem.update(
                humanMode=None,
                focusCombat=True,
                goalComplete=False,
                desiredWeapon=required,
                plan=None,
            )
            self.persist()
            return False
        if self.opportunistic_combat():
            return True
        s = self.obs["self"]
        self.mem["plan"] = "perimeter"
        self.persist()
        if s.get("combatResponse", "flee") != "flee":
            self.action({"action": "set_combat_response", "mode": "flee"})
            return True
        point = dict(station)
        variant = self.mem.get("stationVariant", 0)
        center = tactics.get("stationCenter")
        if center and variant:
            dx, dz = point["x"] - center["x"], point["z"] - center["z"]
            angle = math.atan2(dz, dx) + (0.12 if variant % 2 else -0.12) * (1 + (variant - 1) // 2)
            radius = math.hypot(dx, dz)
            point = {
                "x": center["x"] + math.cos(angle) * radius,
                "z": center["z"] + math.sin(angle) * radius,
            }
        if self.dist(point) <= 2.5:
            if s.get("motion"):
                self.action({"action": "stop"})
        else:
            end = ((s.get("motion") or {}).get("path") or [{}])[-1]
            if (
                not s.get("motion")
                or math.hypot(end.get("x", 1e6) - point["x"], end.get("z", 1e6) - point["z"]) > 3
            ):
                result = self.move(point, gait="run")
                if not result.get("ok"):
                    self.mem["stationVariant"] = (variant + 1) % 7
                    self.persist()
        self.next_wait = 1
        return True

    def target(self):
        return next(
            (
                a
                for a in self.obs.get("nearby", [])
                if a["id"] == self.mem["targetId"] and a.get("online") and (a["state"] != "dead")
            ),
            None,
        )

    def approach(self, target, weapon_range):
        s = self.obs["self"]
        distance = self.dist(target)
        stand = max(
            6, weapon_range * self.store.read("tactics.json", {}).get("standoffRatio", 0.72)
        )
        if distance <= stand:
            return
        fraction = (distance - stand) / distance
        self.move(
            {
                "x": s["x"] + (target["x"] - s["x"]) * fraction,
                "z": s["z"] + (target["z"] - s["z"]) * fraction,
            }
        )

    def hold_range(self, target, weapon):
        tactics = self.store.read("tactics.json", {})
        if not tactics.get("rangedAssault"):
            return False
        enemy = next((w for w in self.catalog["weapons"] if w["id"] == target.get("equipped")), {})
        minimum = min(enemy.get("range", 2) + 8, weapon["range"] * 0.8)
        distance = self.dist(target)
        if distance >= minimum:
            return False
        s = self.obs["self"]
        motion = s.get("motion") or {}
        endpoint = (motion.get("path") or [{}])[-1]
        if (
            "x" in endpoint
            and math.hypot(endpoint["x"] - target["x"], endpoint["z"] - target["z"]) >= minimum
        ):
            self.next_wait = 1
            return True
        desired = weapon["range"] * 0.9
        dx, dz = s["x"] - target["x"], s["z"] - target["z"]
        if distance < 0.1:
            dx, dz, distance = 1, 0, 1
        self.action(
            {
                "action": "move",
                "gait": "run",
                "x": target["x"] + dx / distance * desired,
                "z": target["z"] + dz / distance * desired,
            }
        )
        self.next_wait = 1
        return True

    def danger(self):
        threat = self.obs.get("threat") or self.mem.get("defense")
        if threat and threat["attackerId"] in self.mem.get("eliminatedTargets", []):
            return False
        return bool(threat and self.obs["serverTime"] - threat["at"] < 30000)

    def defend(self):
        if not self.danger():
            self.mem.pop("defense", None)
            self.mem.pop("defenseAnnounced", None)
            return False
        s = self.obs["self"]
        now = self.obs["serverTime"]
        threat = self.obs.get("threat") or self.mem["defense"]
        self.mem["defense"] = threat
        attacker = next(
            (
                a
                for a in self.obs.get("nearby", [])
                if a["id"] == threat["attackerId"] and a.get("online") and (a["state"] != "dead")
            ),
            None,
        )
        weapon = next((w for w in self.catalog["weapons"] if w["id"] == s.get("equipped")), None)
        teammates = self.friendly_ids()
        can_return_fire = (
            attacker
            and weapon
            and (self.dist(attacker) <= weapon["range"])
            and (not attacker.get("vehicleId"))
            and (now >= attacker.get("protectedUntil", 0))
            and (attacker["id"] not in teammates)
        )
        stand = bool(can_return_fire and s["hp"] >= 40)
        if self.mem.get("defenseAnnounced") != threat["attackerId"]:
            self.mem["defenseAnnounced"] = threat["attackerId"]
            self.persist()
            line = (
                f"{threat['attackerName']}, that hit landed. I’m firing back."
                if stand
                else f"{threat['attackerName']}, that hit landed. I’m getting clear."
            )
            self.action({"action": "talk", "message": line[:180]})
            return True
        desired = "hold" if stand else "flee"
        if s.get("combatResponse", "flee") != desired:
            self.action({"action": "set_combat_response", "mode": desired})
            return True
        if s.get("motion"):
            if stand:
                self.action({"action": "stop"})
            else:
                self.next_wait = 1
            return True
        if stand:
            a = {"action": "attack", "targetId": attacker["id"]}
            if self.blocked.get(json.dumps(a, sort_keys=True), 0) <= time.time():
                self.control.pause(
                    max(0, (s.get("lastCombat", 0) + weapon["cooldownMs"] - now) / 1000)
                )
                self.action(a)
                return True
        flee = {"action": "flee", "targetId": threat["attackerId"]}
        if attacker and self.blocked.get(json.dumps(flee, sort_keys=True), 0) <= time.time():
            self.action(flee)
            return True
        choices = sorted(
            (b["entry"] for b in self.catalog["buildings"]),
            key=lambda p: math.hypot(p["x"] - s["x"], p["z"] - s["z"]),
        )
        safe = next(
            (
                p
                for p in choices
                if self.dist(p) > 12
                and (
                    not attacker
                    or math.hypot(p["x"] - attacker["x"], p["z"] - attacker["z"])
                    > self.dist(attacker) + 8
                )
            ),
            None,
        )
        if safe:
            self.move(safe)
        else:
            self.next_wait = 1
        return True

    def opportunistic_combat(self):
        """Interrupt a delivery for a real nearby opponent, retaining the mission."""
        s, now = self.obs["self"], self.obs["serverTime"]
        weapon = next((w for w in self.catalog["weapons"] if w["id"] == s.get("equipped")), None)
        if not weapon or s.get("vehicleId"):
            return False
        friends = self.friendly_ids()
        candidates = [
            a
            for a in self.obs.get("nearby", [])
            if a.get("online")
            and a.get("state") != "dead"
            and a["id"] not in friends
            and a["id"] != s["id"]
            and not a.get("vehicleId")
            and now >= a.get("protectedUntil", 0)
            and self.dist(a) <= weapon["range"]
            and self.blocked.get(
                json.dumps({"action": "attack", "targetId": a["id"]}, sort_keys=True), 0
            )
            <= time.time()
        ]
        if not candidates:
            self.mem.pop("opportunityTarget", None)
            return False
        target = min(candidates, key=lambda a: (a.get("hp", 100), self.dist(a)))
        if self.hold_range(target, weapon):
            self.next_wait = 1
            return True
        self.mem["opportunityTarget"] = target["id"]
        self.persist()
        if s.get("combatResponse") != "hold":
            self.action({"action": "set_combat_response", "mode": "hold"})
        elif s.get("motion"):
            self.action({"action": "stop"})
        elif now >= s.get("lastCombat", 0) + weapon["cooldownMs"]:
            a = {"action": "attack", "targetId": target["id"]}
            result = self.action(a)
            if not result.get("ok"):
                self.blocked[json.dumps(a, sort_keys=True)] = time.time() + 4
        self.next_wait = 1
        return True
