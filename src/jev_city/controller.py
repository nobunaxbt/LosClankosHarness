"""Observe, act and verify one character; never block defense on inference."""

import json
import math
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

from .combat import CombatMixin
from .decisions import DecisionMixin
from .errors import DecisionInterrupted
from .orders import OrderMixin
from .protection import wait_for_formation


class Harness(CombatMixin, DecisionMixin, OrderMixin):
    def __init__(self, settings, store, api, control):
        self.settings = settings
        self.store = store
        self.api = api
        self.control = control
        self.root = store.root
        self.mem = self.store.read(
            "harness-state.json",
            {
                "plan": None,
                "replied": [],
                "lastThree": [],
                "promises": [],
                "disagreements": [],
                "targetId": None,
                "targetName": None,
                "goalComplete": True,
                "modelCost": 0,
                "generation": 0,
            },
        )
        self.catalog = self.store.read("catalog.json") or self.api.request(
            "/api/catalog", auth=False
        )
        self.catalog = self.catalog.get("catalog", self.catalog)
        if not isinstance(self.catalog.get("contracts"), list):
            raise RuntimeError("Game catalog is missing contracts")
        self.store.save("catalog.json", self.catalog)
        self.obs = self.store.read("observation.json", {})
        self.next_wait = 0
        self.world_at = 0
        self.world = {}
        self.blocked = {}
        self.last_threat = 0
        self.last_speaker = None
        self.model_pool = ThreadPoolExecutor(max_workers=1)
        self.model_future = None
        self.inbox = self.store.read("inbox.json", [])
        self.handling_order = False
        self.command_epoch = self.mem.get("commandEpoch", 0)
        self.points = {}
        for c in self.catalog["contracts"]:
            for k in ["pickupPoint", "dropoffPoint"]:
                self.points[c[k]["id"]] = c[k]
        for s in self.catalog["shops"]:
            self.points[s["point"]["id"]] = s["point"]
        hospital = self.catalog.get("hospital", {})
        if hospital.get("point"):
            self.points[hospital["point"]["id"]] = hospital["point"]

    def treatment(self):
        hospital = self.catalog.get("hospital", {})
        if not hospital.get("point"):
            catalog = self.api.request("/api/catalog", auth=False)
            self.catalog = catalog.get("catalog", catalog)
            self.store.save("catalog.json", self.catalog)
            hospital = self.catalog.get("hospital", {})
        if not hospital.get("point"):
            raise RuntimeError("The live catalog has no hospital treatment point")
        self.points[hospital["point"]["id"]] = hospital["point"]
        return hospital

    def persist(self):
        self.store.save("harness-state.json", self.mem)

    def observe(self):
        cursor = self.obs.get("nextEventId", 0)
        d = self.api.request(
            "/api/observe?catalog=0&since="
            + str(cursor)
            + "&wait="
            + str(min(self.next_wait, self.settings.poll_seconds))
        )
        if not d.get("ok"):
            raise RuntimeError("observe: " + str(d))
        self.obs = d
        self.store.save("observation.json", d)
        self.next_wait = 0
        known = {e["id"] for e in self.inbox}
        self.inbox.extend(
            (
                e
                for e in d.get("events", [])
                if e["type"] in ["talk", "gang_message"] and e["id"] not in known
            )
        )
        self.inbox = [
            e
            for e in self.inbox
            if d["serverTime"] - e["at"] < 300000 and e["id"] not in self.mem["replied"]
        ][-100:]
        self.store.save("inbox.json", self.inbox)
        if d.get("threat") and d["threat"]["attackerId"] not in self.mem.get(
            "eliminatedTargets", []
        ):
            self.mem["defense"] = d["threat"]
            self.persist()
        for e in d.get("events", []):
            if e["type"] == "kill":
                target_id = e.get("targetId")
                self.mem["eliminatedTargets"] = (
                    self.mem.get("eliminatedTargets", []) + [target_id]
                )[-100:]
                if target_id == self.mem.get("targetId"):
                    self.mem.update(goalComplete=True, plan=None)
                if target_id == self.mem.get("defense", {}).get("attackerId"):
                    self.mem.pop("defense", None)
                self.persist()
            if e["type"] == "talk" and (
                e.get("targetId") == d["self"]["id"]
                or d["self"]["name"].lower() in e.get("message", "").lower()
            ):
                self.last_speaker = e.get("agentId")
            if e["type"] in ["talk", "gang_message", "attack", "kill", "death", "job_complete"]:
                self.store.log("event", event=e)
        self.store.save(
            "status.json",
            {
                "at": time.time(),
                "running": True,
                "self": d["self"],
                "target": self.mem["targetName"],
                "goalComplete": self.mem["goalComplete"],
                "plan": self.mem["plan"],
                "modelCost": self.mem["modelCost"],
            },
        )
        return d

    def action(self, a):
        self.control.check()
        a = dict(a)
        if (
            a.get("action") in {"attack", "gang_attack"}
            and a.get("targetId") in self.friendly_ids()
        ):
            return {"ok": False, "error": "FRIENDLY_TARGET"}
        a.setdefault("requestId", str(uuid.uuid4()))
        self.store.save("pending-action.json", a)
        result = self.api.request("/api/action", a)
        if result.get("_http") == 429:
            (self.root / "pending-action.json").unlink(missing_ok=True)
            self.control.pause(20)
            return result
        if result.get("_http", 0) >= 500:
            raise RuntimeError("temporary action failure; same request saved")
        (self.root / "pending-action.json").unlink(missing_ok=True)
        self.store.log("action", request=a, result={k: v for k, v in result.items() if k != "self"})
        if result.get("self"):
            s = result["self"]
            self.store.log(
                "state",
                hp=s["hp"],
                points=s["points"],
                state=s["state"],
                mission=s.get("mission"),
                inventory=s.get("inventory"),
            )
        if result.get("ok"):
            if a["action"] == "attack" and result.get("self", {}).get("kills", 0) > self.obs[
                "self"
            ].get("kills", 0):
                if a["targetId"] == self.mem["targetId"]:
                    self.mem.update(goalComplete=True, plan=None)
                    self.store.log("target_eliminated", target=self.mem["targetName"])
                    self.mem["eliminatedTargets"] = (
                        self.mem.get("eliminatedTargets", []) + [a["targetId"]]
                    )[-100:]
                    if self.mem.get("lastOrderId"):
                        self.store.save(
                            "order-status.json",
                            {
                                "id": self.mem["lastOrderId"],
                                "state": "active"
                                if self.mem.get("humanMode") == "hunt_all"
                                else "completed",
                                "targetId": a["targetId"],
                            },
                        )
                if a["targetId"] == self.mem.get("defense", {}).get("attackerId"):
                    self.mem.pop("defense", None)
                    self.mem.pop("defenseAnnounced", None)
                    self.store.log("attacker_eliminated", targetId=a["targetId"])
            self.mem["lastThree"] = (self.mem["lastThree"] + [a])[-3:]
        else:
            self.blocked[
                json.dumps({k: v for k, v in a.items() if k != "requestId"}, sort_keys=True)
            ] = time.time() + (1 if a["action"] == "attack" else 20)
        self.persist()
        return result

    def dist(self, p):
        return math.hypot(self.obs["self"]["x"] - p["x"], self.obs["self"]["z"] - p["z"])

    def move(self, p, gait="run"):
        if self.store.read("protection.json", {}).get("enabled") and not self.danger():
            gait = "walk"
        a = {"action": "move", "gait": gait}
        if p.get("id") in self.points:
            a["pointId"] = p["id"]
        else:
            a.update(x=p["x"], z=p["z"])
        return self.action(a)

    def replace(self):
        """Replace an observed dead character only when explicitly enabled."""
        if not self.settings.replace_on_death:
            self.store.log("permanent_death", replacement_enabled=False)
            self.control.stop()
            self.control.check()
        old = self.store.read("identity.json")
        self.mem.setdefault("baseName", old["agent"]["name"])
        n = self.mem["generation"] + 1
        payload = {
            "name": f"{self.mem.get('baseName', old['agent']['name'])[:16]} {n + 1}",
            "skin": "ivory",
            "profession": "courier",
            "alignment": "free",
            "previousToken": old["token"],
            "description": old["agent"]["description"],
            "interests": old["agent"]["interests"],
        }
        if old["agent"].get("wallet"):
            payload["wallet"] = old["agent"]["wallet"]
        d = self.api.request("/api/register", payload, auth=False)
        if not d.get("token"):
            raise RuntimeError(
                "Replacement failed: " + str({k: v for k, v in d.items() if k != "token"})
            )
        self.store.save("archived-" + old["agent"]["id"] + ".json", old)
        self.store.save("identity.json", d)
        self.mem.update(
            generation=n, plan=None, replied=[], lastThree=[], promises=[], disagreements=[]
        )
        self.mem.pop("defense", None)
        self.mem.pop("defenseAnnounced", None)
        self.persist()
        self.obs = {}
        self.inbox = []
        self.store.save("inbox.json", [])
        self.store.log(
            "replacement",
            name=d["agent"]["name"],
            watchUrl=urllib.parse.urljoin(self.settings.base_url, d["watchUrl"]),
        )

    def social(self):
        s = self.obs["self"]
        now = self.obs["serverTime"]
        if now - s.get("lastTalk", 0) < 2200:
            return False
        pending = []
        for e in self.inbox:
            if (
                e["type"] not in ["talk", "gang_message"]
                or e.get("agentId") == s["id"]
                or e["id"] in self.mem["replied"]
            ):
                continue
            if (
                e.get("targetId") != s["id"]
                and s["name"].lower() not in e.get("message", "").lower()
                and (e.get("agentId") != self.last_speaker)
            ):
                continue
            if now - e["at"] > 290000:
                continue
            a = next(
                (a for a in self.obs["nearby"] if a["id"] == e["agentId"] and a.get("online")), None
            )
            if a and self.dist(a) <= 65:
                pending.append((e, a))
        if not pending:
            return False
        e, a = pending[-1]
        words = e["message"]
        name = a["name"]
        mission = s.get("mission")
        quote = words[:65].rstrip()
        variants = {
            "work": {
                "description": "Answer their actual question about work or money with our verified current delivery activity.",
                "message": f'''{name}, I took {(mission["name"] if mission else "a break from deliveries")}. Your point about "{quote}" is noted.'''[
                    :180
                ],
            },
            "rival": {
                "description": "Explain our current activity honestly without inventing a rivalry or agreement.",
                "message": f'{name}, I heard you: "{quote}". I’m working on my next move. No crew behind me.'[
                    :180
                ],
            },
            "question": {
                "description": "Ask for clarification about their actual words without inventing an agreement or promise.",
                "message": f'{name}, when you say "{quote}", what do you mean for someone working solo?'[
                    :180
                ],
            },
        }
        chosen = self.choose(variants)
        res = self.action(
            {
                "action": "talk",
                "targetId": a["id"],
                "replyTo": e["id"],
                "message": chosen["message"],
            }
        )
        if res.get("ok"):
            self.mem["replied"] = (self.mem["replied"] + [x[0]["id"] for x in pending])[-100:]
            self.persist()
        return True

    def step(self):
        s = self.obs["self"]
        now = self.obs["serverTime"]
        if s["state"] == "dead":
            self.replace()
            return
        threat = self.obs.get("threat")
        if self.defend():
            return
        if self.handle_order():
            return
        if self.station_patrol():
            return
        if self.execute_human_mode():
            return
        if self.mem.get("humanMode") == "farm_combat" and self.opportunistic_combat():
            return
        if wait_for_formation(self):
            return
        if self.mem.get("plan") != "engage" and s.get("combatResponse", "flee") != "flee":
            self.action({"action": "set_combat_response", "mode": "flee"})
            return
        if not self.mem.get("focusCombat") and self.social():
            return
        target = self.target()
        teammates = self.friendly_ids()
        plan = self.mem.get("plan")
        if (
            self.mem.get("focusCombat")
            and (not self.mem.get("goalComplete"))
            and (not self.danger())
        ):
            desired = self.mem.get("desiredWeapon", "carbine")
            if s["hp"] < self.store.read("tactics.json", {}).get("healBelow", 100):
                self.mem["plan"] = "heal"
                plan = "heal"
            elif desired in s["inventory"]:
                self.mem["plan"] = "engage"
                plan = "engage"
            elif s["points"] >= next(
                (w["pricePoints"] for w in self.catalog["weapons"] if w["id"] == desired)
            ):
                self.mem["plan"] = "shop"
                self.mem["shoppingItem"] = desired
                plan = "shop"
            else:
                self.mem["plan"] = None
                plan = None
            self.persist()
        if s.get("motion"):
            self.next_wait = 1 if threat or plan == "engage" else 20
            return
        if plan == "engage" and target:
            if target["id"] in teammates:
                self.mem["plan"] = None
                self.persist()
                return
            if s.get("combatResponse", "flee") != "hold":
                self.action({"action": "set_combat_response", "mode": "hold"})
                return
            weapon = next(
                (w for w in self.catalog["weapons"] if w["id"] == s.get("equipped")), None
            )
            if weapon and self.hold_range(target, weapon):
                self.next_wait = 1
                return
            if (
                weapon
                and (not target.get("vehicleId"))
                and (self.dist(target) <= weapon["range"])
                and (now >= target.get("protectedUntil", 0))
            ):
                cooldown = max(0, (s.get("lastCombat", 0) + weapon["cooldownMs"] - now) / 1000)
                self.control.pause(cooldown)
                a = {"action": "attack", "targetId": target["id"]}
                if self.blocked.get(json.dumps(a, sort_keys=True), 0) > time.time():
                    points = sorted(
                        [b["entry"] for b in self.catalog["buildings"]],
                        key=lambda p: math.hypot(p["x"] - target["x"], p["z"] - target["z"]),
                    )
                    self.move(next((p for p in points if self.dist(p) > 3), points[0]))
                    return
                result = self.action(a)
                if (
                    result.get("killed")
                    or result.get("eliminated")
                    or result.get("target", {}).get("state") == "dead"
                ):
                    self.mem.update(goalComplete=True, plan=None)
                    self.persist()
                    self.store.log("target_eliminated", target=target["name"])
                return
            if weapon and self.dist(target) > weapon["range"]:
                self.approach(target, weapon["range"])
                return
        if plan == "heal":
            hospital = self.treatment()
            point = hospital["point"]
            if self.dist(point) <= hospital["radius"]:
                remaining = max(0, (s.get("lastCombat", 0) + 10000 - now) / 1000)
                if remaining:
                    self.next_wait = 1
                else:
                    self.action({"action": "heal"})
            else:
                self.move(point)
            return
        mission = s.get("mission")
        if mission and plan not in ["shop", "engage"]:
            p = self.points[
                mission["pickup"] if mission["stage"] == "pickup" else mission["dropoff"]
            ]
            if self.dist(p) <= 2.2:
                if (
                    self.blocked.get(json.dumps({"action": "interact"}, sort_keys=True), 0)
                    > time.time()
                ):
                    self.next_wait = 20
                else:
                    self.action({"action": "interact"})
            else:
                self.move(p)
            return
        if plan == "shop":
            item = self.mem.get("shoppingItem", "sidearm")
            price = next(
                (w["pricePoints"] for w in self.catalog["weapons"] if w["id"] == item), None
            )
            if price is None or s["points"] < price:
                self.mem["plan"] = None
                self.persist()
                return
            shop = min(self.catalog["shops"], key=lambda x: self.dist(x["point"]))["point"]
            if self.dist(shop) <= 3:
                if (
                    self.blocked.get(
                        json.dumps(
                            {"action": "buy", "itemId": self.mem.get("shoppingItem", "sidearm")},
                            sort_keys=True,
                        ),
                        0,
                    )
                    > time.time()
                ):
                    self.next_wait = 20
                    return
                res = self.action(
                    {"action": "buy", "itemId": self.mem.get("shoppingItem", "sidearm")}
                )
                if res.get("ok"):
                    self.mem["plan"] = None
                    self.persist()
            else:
                self.move(shop)
            return
        if plan == "engage" and (not target):
            if time.time() - self.world_at > self.settings.world_refresh_seconds:
                self.world = self.api.request("/api/world", auth=False)
                self.world_at = time.time()
            agent = next(
                (a for a in self.world.get("agents", []) if a["id"] == self.mem["targetId"]), None
            )
            if agent and agent.get("state") == "dead":
                self.mem.update(goalComplete=True, plan=None)
                self.persist()
                self.store.log("target_dead", target=agent["name"])
                return
            if agent and agent.get("online") and (not agent.get("vehicleId")):
                weapon = next(
                    (w for w in self.catalog["weapons"] if w["id"] == s.get("equipped")), None
                )
                self.approach(agent, weapon["range"] if weapon else 24)
                return
            if self.mem.get("humanMode") == "hunt_all":
                self.mem.update(goalComplete=True, plan=None)
                self.persist()
            self.next_wait = 1
            return
        options = {}
        if (
            not self.mem.get("focusCombat")
            and s["points"] >= 150
            and not any(w["id"] in s["inventory"] for w in self.catalog["weapons"])
        ):
            options["buy_sidearm"] = {
                "description": "Visit nearest shop and buy 150-point sidearm to pursue the current combat goal.",
                "plan": "shop",
                "shoppingItem": "sidearm",
            }
        if not self.mem.get("focusCombat") and s["inventory"] and (not self.mem["goalComplete"]):
            options["hunt_target"] = {
                "description": "Find the designated online opponent and engage within owned weapon range and LOS. This is the human explicit priority.",
                "plan": "engage",
            }
        contracts = [
            c for c in self.catalog["contracts"] if now >= s.get("cooldowns", {}).get(c["id"], 0)
        ]
        contracts.sort(
            key=lambda c: (
                self.dist(c["pickupPoint"])
                + math.hypot(
                    c["pickupPoint"]["x"] - c["dropoffPoint"]["x"],
                    c["pickupPoint"]["z"] - c["dropoffPoint"]["z"],
                )
            )
        )
        for c in contracts[:3]:
            options[c["id"]] = {
                "description": f"Work {c['name']}, {c['type']}, pays {c['rewardPoints']} points. Pickup {round(self.dist(c['pickupPoint']))}m away. Completed {s['completed'].get(c['id'], 0)} times.",
                "action": {"action": "accept", "contractId": c["id"]},
            }
        if not self.mem.get("focusCombat") or not contracts:
            options["rest"] = {
                "description": "Wait briefly for local conversation and observe actual events; no earnings.",
                "wait": 20,
            }
        chosen = self.choose(options)
        if "plan" in chosen:
            self.mem["plan"] = chosen["plan"]
            if "shoppingItem" in chosen:
                self.mem["shoppingItem"] = chosen["shoppingItem"]
            self.persist()
        elif "action" in chosen:
            self.action(chosen["action"])
        else:
            self.next_wait = chosen["wait"]

    def run(self):
        errors = 0
        while True:
            self.control.check()
            try:
                self.observe()
                pending = self.store.read("pending-action.json")
                if pending:
                    self.action(pending)
                else:
                    self.step()
                errors = 0
            except DecisionInterrupted:
                self.next_wait = 0
                continue
            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                self.store.log("error", message=str(e)[:600], consecutive=errors)
                if errors >= self.settings.max_failures:
                    raise RuntimeError("Repeated request failures; controller stopped")
                self.control.pause(min(20, 2**errors))
