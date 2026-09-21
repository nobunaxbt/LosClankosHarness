import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_controller import CATALOG, observation

from jev_city.control import Control
from jev_city.escort import Escort
from jev_city.settings import Settings
from jev_city.storage import Store


class EscortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "guard")
        self.leader = Store(Path(self.temp.name) / "leader")
        self.leader.save("identity.json", {"agent": {"id": "leader"}})
        self.leader.save(
            "status.json",
            {"at": time.time(), "self": {"id": "leader", "x": 30, "z": 0, "state": "idle"}},
        )
        catalog = copy.deepcopy(CATALOG)
        catalog["weapons"].append(
            {"id": "sidearm", "pricePoints": 150, "range": 24, "cooldownMs": 650}
        )
        self.store.save("catalog.json", catalog)
        self.api = Mock()
        self.h = Escort(
            Settings(self.store.root), self.store, self.api, Control(self.store), self.leader.root
        )
        self.addCleanup(self.h.model_pool.shutdown, wait=True)
        self.h.obs = observation()
        self.h.obs["self"]["combatResponse"] = "flee"
        self.h.obs["gang"] = {"members": [{"id": "leader"}, {"id": "self"}]}
        self.h.action = Mock(return_value={"ok": True})

    def test_follows_observed_leader_without_inference(self):
        self.h.choose = Mock(side_effect=AssertionError("formation must not wait for model"))
        self.h.step()
        self.h.action.assert_called_once_with({"action": "move", "gait": "run", "x": 30, "z": 0})

    def test_stale_leader_position_stops_movement(self):
        self.leader.save("status.json", {"at": 0, "self": {"id": "leader"}})
        self.h.obs["self"]["motion"] = {"x": 30}
        self.h.step()
        self.h.action.assert_called_once_with({"action": "stop"})

    def test_replacement_identity_never_uses_old_position(self):
        self.leader.save("identity.json", {"agent": {"id": "replacement"}})
        self.assertIsNone(self.h.leader())

    def test_protects_leader_from_actual_shooter(self):
        leader = self.leader.read("status.json")
        leader["self"]["lastThreat"] = {"attackerId": "opponent-current", "at": 100000}
        self.leader.save("status.json", leader)
        self.h.obs["self"]["combatResponse"] = "hold"
        self.h.step()
        self.h.action.assert_called_once_with({"action": "attack", "targetId": "opponent-current"})

    def test_never_attacks_own_gang(self):
        leader = self.leader.read("status.json")
        leader["self"]["lastThreat"] = {"attackerId": "opponent-current", "at": 100000}
        self.leader.save("status.json", leader)
        self.h.obs["gang"]["members"].append({"id": "opponent-current"})
        self.h.step()
        self.assertNotEqual(self.h.action.call_args.args[0]["action"], "attack")

    def test_waits_for_gang_verification_before_following(self):
        self.h.obs["gang"] = None
        self.api.request.return_value = {"gangs": []}
        self.h.step()
        self.h.action.assert_not_called()

    def test_lookahead_follows_corner_not_straight_line(self):
        leader = {
            "x": 8,
            "z": 0,
            "motion": {"origin": {"x": 0, "z": 0}, "path": [{"x": 10, "z": 0}, {"x": 10, "z": 30}]},
        }
        self.assertEqual(self.h.formation_point(leader), {"x": 10, "z": 8})

    def test_lookahead_skips_already_travelled_segments(self):
        leader = {
            "x": 25,
            "z": 0,
            "motion": {"origin": {"x": 0, "z": 0}, "path": [{"x": 10, "z": 0}, {"x": 50, "z": 0}]},
        }
        self.assertEqual(self.h.formation_point(leader), {"x": 35, "z": 0})

    def test_on_sight_attacks_without_a_threat(self):
        self.h.engage_on_sight = True
        self.h.obs["self"]["combatResponse"] = "hold"
        self.h.step()
        self.h.action.assert_called_once_with({"action": "attack", "targetId": "opponent-current"})

    def test_on_sight_ignores_protected_and_vehicle_targets(self):
        self.h.engage_on_sight = True
        for changes in [{"protectedUntil": 200000}, {"vehicleId": "car"}]:
            target = self.h.obs["nearby"][0]
            target.update(changes)
            self.assertFalse(self.h.engage(self.h.leader()))
            for key in changes:
                target.pop(key)

    def test_blocked_shot_resumes_formation(self):
        self.h.engage_on_sight = True
        self.h.obs["self"]["combatResponse"] = "hold"
        self.h.action.return_value = {"ok": False, "error": "LINE_OF_SIGHT"}
        self.assertTrue(self.h.engage(self.h.leader()))
        self.assertFalse(self.h.engage(self.h.leader()))

    def test_carbine_guard_keeps_working_when_only_sidearm_owned(self):
        self.h.required_weapon = "carbine"
        self.h.mem["desiredWeapon"] = "carbine"
        self.h.obs["self"]["inventory"] = ["sidearm"]
        self.h.obs["self"]["equipped"] = "sidearm"
        with patch("jev_city.controller.Harness.step") as work:
            self.h.step()
            work.assert_called_once()
        self.h.action.assert_not_called()

    def test_carbine_guard_enters_screen_after_purchase(self):
        self.h.required_weapon = "carbine"
        with patch("jev_city.controller.Harness.step") as work:
            self.h.step()
            work.assert_not_called()
        self.assertEqual(self.h.mem["plan"], "screen")

    def test_circle_spreads_nine_guards_around_leader(self):
        import math

        self.h.formation = "circle"
        self.h.slot_count = 9
        leader = {"x": 100, "z": 100}
        positions = []
        for i in range(9):
            self.h.slot_index = i
            p = self.h.formation_point(leader)
            self.assertAlmostEqual(math.hypot(p["x"] - 100, p["z"] - 100), 10, delta=0.5)
            positions.append((p["x"], p["z"]))
        self.assertEqual(len(set(positions)), 9)
        self.assertTrue(any(x < 100 for x, z in positions))
        self.assertTrue(any(x > 100 for x, z in positions))

    def test_circle_does_not_swap_sectors_on_leader_turn(self):
        self.h.formation = "circle"
        a = self.h.formation_point({"x": 30, "z": 20, "angle": 0})
        b = self.h.formation_point({"x": 30, "z": 20, "angle": 3})
        self.assertEqual(a, b)

    def test_blocked_circle_position_tries_neighboring_sector(self):
        self.h.formation = "circle"
        original = self.h.formation_point(self.h.leader())
        self.h.action.return_value = {"ok": False, "error": "DESTINATION_BLOCKED"}
        self.h.step()
        self.assertNotEqual(self.h.formation_point(self.h.leader()), original)

    def test_wounded_guard_heals_at_counter(self):
        self.h.obs["self"].update(hp=50, lastCombat=0)
        self.h.step()
        self.h.action.assert_called_once_with({"action": "heal"})
        self.assertTrue(self.h.mem["guardHealing"])

    def test_heal_respects_recent_combat(self):
        self.h.obs["self"].update(hp=50, lastCombat=99000)
        self.assertTrue(self.h.recover())
        self.h.action.assert_not_called()

    def test_healed_guard_returns_to_role(self):
        self.h.mem["guardHealing"] = True
        self.h.obs["self"]["hp"] = 100
        self.assertFalse(self.h.recover())
        self.assertNotIn("guardHealing", self.h.mem)

    def test_recovery_does_not_restart_existing_heal_route(self):
        self.h.obs["self"].update(hp=40, x=30, motion={"path": [{"x": 0, "z": 0}]})
        self.assertTrue(self.h.recover())
        self.h.action.assert_not_called()

    def test_routine_healing_waits_when_two_guards_are_recovering(self):
        for i in range(2):
            store = Store(Path(self.temp.name) / f"recovery-{i}")
            store.save("status.json", {"at": time.time(), "self": {"state": "moving"}})
            store.save("harness-state.json", {"guardHealing": True})
            self.h.squad_stores.append(store)
        self.h.obs["self"]["hp"] = 60
        self.assertFalse(self.h.recover())
        self.h.obs["self"]["hp"] = 20
        self.assertTrue(self.h.recover())

    def test_perimeter_carbine_requirement_overrides_sidearm_role(self):
        self.store.save("tactics.json", {"station": {"x": 40, "z": 40}, "stationWeapon": "carbine"})
        self.h.obs["self"].update(inventory=["sidearm"], equipped="sidearm")
        with patch("jev_city.controller.Harness.step") as work:
            self.h.step()
            work.assert_called_once()

    def test_work_and_fight_mode_returns_armed_guard_to_missions(self):
        self.store.save("tactics.json", {"workAndFight": True})
        self.h.mem.update(plan="perimeter", humanMode=None, focusCombat=True)
        with patch("jev_city.controller.Harness.step") as work:
            self.h.step()
            work.assert_called_once()
        self.assertEqual(self.h.mem["humanMode"], "farm_combat")
        self.assertFalse(self.h.mem["focusCombat"])
        self.assertIsNone(self.h.mem["plan"])
