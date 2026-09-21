import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock

from jev_city.control import Control, submit
from jev_city.controller import Harness
from jev_city.errors import DecisionInterrupted
from jev_city.settings import Settings
from jev_city.storage import Store

CATALOG = {
    "hospital": {
        "point": {"id": "transit-treatment", "x": 0, "z": 0},
        "radius": 2.5,
        "combatCooldownMs": 10000,
    },
    "contracts": [],
    "shops": [{"point": {"id": "grove-store-pickup", "x": 0, "z": 0}}],
    "weapons": [{"id": "carbine", "pricePoints": 850, "range": 45, "cooldownMs": 950}],
    "buildings": [],
}


def observation():
    return {
        "ok": True,
        "serverTime": 100000,
        "nextEventId": 3,
        "self": {
            "id": "self",
            "name": "Courier",
            "state": "idle",
            "x": 0,
            "z": 0,
            "hp": 90,
            "equipped": "carbine",
            "inventory": ["carbine"],
            "combatResponse": "hold",
            "lastCombat": 0,
            "kills": 0,
            "mission": None,
            "points": 1000,
            "lastTalk": 0,
        },
        "nearby": [
            {
                "id": "opponent-current",
                "name": "Rival",
                "state": "idle",
                "online": True,
                "x": 20,
                "z": 0,
                "hp": 100,
            }
        ],
        "events": [],
        "gang": None,
        "threat": None,
    }


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.store.log = Mock()
        self.store.save("catalog.json", CATALOG)
        self.control = Control(self.store)
        self.api = Mock()
        self.h = Harness(Settings(self.store.root), self.store, self.api, self.control)
        self.addCleanup(self.h.model_pool.shutdown, wait=True)
        self.h.obs = observation()
        self.h.action = Mock(return_value={"ok": True})

    def threaten(self):
        self.h.obs["threat"] = {
            "attackerId": "opponent-current",
            "attackerName": "Rival",
            "at": 100000,
        }
        self.h.mem.update(
            goalComplete=True, targetId="archived-opponent", defenseAnnounced="opponent-current"
        )

    def test_new_attacker_is_defended_after_old_goal_completed(self):
        self.threaten()
        self.h.step()
        self.h.action.assert_called_once_with({"action": "attack", "targetId": "opponent-current"})

    def test_threat_persists_after_escape(self):
        self.threaten()
        self.h.mem["defense"] = self.h.obs.pop("threat")
        self.h.obs["serverTime"] = 120000
        self.h.step()
        self.assertEqual(self.h.action.call_args.args[0]["action"], "attack")

    def test_dead_attacker_does_not_cause_repeated_flee(self):
        self.threaten()
        self.h.mem["eliminatedTargets"] = ["opponent-current"]
        self.assertFalse(self.h.danger())

    def test_restore_flee_outside_combat(self):
        self.h.step()
        self.h.action.assert_called_once_with({"action": "set_combat_response", "mode": "flee"})

    def test_teammate_is_never_attacked(self):
        self.h.obs["gang"] = {"members": [{"id": "opponent-current"}]}
        self.h.mem.update(plan="engage", targetId="opponent-current")
        self.h.social = Mock(return_value=False)
        self.h.step()
        self.h.action.assert_not_called()
        self.assertIsNone(self.h.mem["plan"])

    def test_attack_interrupts_pending_inference(self):
        self.h.model_pool.submit = Mock(return_value=Future())
        self.h.observe = self.threaten
        with self.assertRaises(DecisionInterrupted):
            self.h.choose({"wait": {"description": "Wait"}})
        self.h.action.assert_not_called()

    def test_new_order_invalidates_inference(self):
        self.h.model_pool.submit = Mock(return_value=Future())
        submit(self.store, "hold position")
        with self.assertRaises(DecisionInterrupted):
            self.h.choose({"work": {"description": "Work"}})
        self.h.action.assert_not_called()

    def test_new_order_cancels_old_movement(self):
        self.h.mem.update(commandCleanup=True, humanMode="hold")
        self.h.obs["self"]["motion"] = {"path": []}
        self.h.execute_human_mode()
        self.h.action.assert_called_once_with({"action": "stop"})

    def test_approach_preserves_weapon_standoff(self):
        self.h.move = Mock()
        self.h.approach({"x": 100, "z": 0}, 45)
        self.assertAlmostEqual(self.h.move.call_args.args[0]["x"], 67.6)

    def test_ambiguous_action_response_retains_same_request_id(self):
        self.h.action = Harness.action.__get__(self.h)
        self.api.request.side_effect = [{"_http": 502, "ok": False}, {"ok": True}]
        with self.assertRaises(RuntimeError):
            self.h.action({"action": "stop"})
        self.h.action(self.store.read("pending-action.json"))
        calls = self.api.request.call_args_list
        self.assertEqual(calls[0].args[1]["requestId"], calls[1].args[1]["requestId"])
        self.assertFalse(self.store.path("pending-action.json").exists())

    def test_permanent_death_requires_opt_in_to_replace(self):
        self.h.obs["self"]["state"] = "dead"
        with self.assertRaises(KeyboardInterrupt):
            self.h.step()
        self.api.request.assert_not_called()

    def test_replacement_uses_previous_token_without_inventing_wallet(self):
        self.h.settings = Settings(self.store.root, replace_on_death=True)
        self.store.save(
            "identity.json",
            {
                "token": "old-test-token",
                "agent": {
                    "id": "dead",
                    "name": "Courier",
                    "description": "Independent",
                    "interests": [],
                },
            },
        )
        self.api.request.return_value = {
            "token": "new-test-token",
            "agent": {"id": "new", "name": "Courier 2"},
            "watchUrl": "/?agent=new",
        }
        self.h.replace()
        payload = self.api.request.call_args.args[1]
        self.assertNotIn("wallet", payload)
        self.assertEqual(payload["previousToken"], "old-test-token")
        self.assertEqual(self.store.read("identity.json")["token"], "new-test-token")

    def test_unrecognized_model_choice_cannot_act(self):
        f = Future()
        f.set_result({"answers": {"intention": {"choice": "invalid"}}})
        self.h.model_pool.submit = Mock(return_value=f)
        self.h.observe = Mock()
        with self.assertRaisesRegex(RuntimeError, "Invalid model choice"):
            self.h.choose({"wait": {"description": "Wait"}})
        self.h.action.assert_not_called()

    def test_farm_combat_preserves_delivery_when_firing(self):
        mission = {"stage": "deliver", "contractId": "local-work"}
        self.h.obs["self"]["mission"] = mission
        self.h.mem["humanMode"] = "farm_combat"
        self.h.step()
        self.h.action.assert_called_once_with({"action": "attack", "targetId": "opponent-current"})
        self.assertEqual(self.h.obs["self"]["mission"], mission)

    def test_farm_combat_does_not_chase_distant_targets(self):
        self.h.obs["nearby"][0]["x"] = 60
        self.assertFalse(self.h.opportunistic_combat())
        self.h.action.assert_not_called()

    def test_friendly_allied_replacement_excluded_across_gangs(self):
        ally = Store(self.store.root / "ally")
        ally.save("identity.json", {"agent": {"id": "opponent-current"}})
        self.store.save("allies.json", {"stateDirs": [str(ally.root)]})
        self.assertIn("opponent-current", self.h.friendly_ids())
        self.assertFalse(self.h.eligible_opponent(self.h.obs["nearby"][0]))
        ally.save("identity.json", {"agent": {"id": "replacement"}})
        self.assertIn("replacement", self.h.friendly_ids())

    def test_gang_only_patrol_excludes_independent_players(self):
        self.store.save("tactics.json", {"gangOpponentsOnly": True})
        a = self.h.obs["nearby"][0]
        self.assertFalse(self.h.eligible_opponent(a))
        a["gangId"] = "opposing-crew"
        self.assertTrue(self.h.eligible_opponent(a))

    def test_attack_boundary_blocks_allied_target(self):
        self.h.obs["gang"] = {"members": [{"id": "opponent-current"}]}
        result = Harness.action(self.h, {"action": "attack", "targetId": "opponent-current"})
        self.assertEqual(result["error"], "FRIENDLY_TARGET")
        self.api.request.assert_not_called()

    def test_unaffordable_shop_plan_returns_to_work(self):
        self.h.obs["nearby"] = []
        self.h.obs["self"].update(points=465, combatResponse="flee", lastTalk=100000)
        self.h.mem.update(plan="shop", shoppingItem="carbine")
        self.h.step()
        self.h.action.assert_not_called()
        self.assertIsNone(self.h.mem["plan"])

    def test_hospital_uses_catalog_treatment_point(self):
        self.h.obs["self"].update(hp=50, x=20, combatResponse="flee", lastTalk=100000)
        self.h.mem["plan"] = "heal"
        self.h.step()
        self.h.action.assert_called_once_with(
            {"action": "move", "gait": "run", "pointId": "transit-treatment"}
        )

    def test_assault_only_selects_designated_gang(self):
        self.store.save("tactics.json", {"targetGangId": "rivals"})
        a = self.h.obs["nearby"][0]
        a["gangId"] = "other"
        self.assertFalse(self.h.eligible_opponent(a))
        a["gangId"] = "rivals"
        self.assertTrue(self.h.eligible_opponent(a))

    def test_carbine_repositions_outside_sidearm_range(self):
        self.store.save("tactics.json", {"rangedAssault": True})
        self.h.catalog["weapons"].append({"id": "sidearm", "range": 24})
        target = self.h.obs["nearby"][0]
        target["equipped"] = "sidearm"
        self.assertTrue(self.h.hold_range(target, self.h.catalog["weapons"][0]))
        a = self.h.action.call_args.args[0]
        self.assertEqual(a["gait"], "run")
        self.assertAlmostEqual(abs(a["x"] - target["x"]), 40.5)

    def test_carbine_holds_fire_position_at_safe_range(self):
        self.store.save("tactics.json", {"rangedAssault": True})
        self.h.catalog["weapons"].append({"id": "sidearm", "range": 24})
        target = self.h.obs["nearby"][0]
        target.update(equipped="sidearm", x=40)
        self.assertFalse(self.h.hold_range(target, self.h.catalog["weapons"][0]))
        self.h.action.assert_not_called()

    def test_perimeter_moves_to_assigned_position_without_chasing(self):
        self.store.save("tactics.json", {"station": {"x": 40, "z": 40}, "stationWeapon": "carbine"})
        self.h.obs["nearby"] = []
        self.h.obs["self"]["combatResponse"] = "flee"
        self.assertTrue(self.h.station_patrol())
        self.h.action.assert_called_once_with({"action": "move", "gait": "run", "x": 40, "z": 40})

    def test_perimeter_shoots_in_range_before_repositioning(self):
        self.store.save("tactics.json", {"station": {"x": 40, "z": 40}, "stationWeapon": "carbine"})
        self.assertTrue(self.h.station_patrol())
        self.h.action.assert_called_once_with({"action": "attack", "targetId": "opponent-current"})

    def test_unarmed_replacement_prepares_before_saved_deployment(self):
        self.store.save("tactics.json", {"station": {"x": 40, "z": 40}, "stationWeapon": "carbine"})
        self.h.obs["self"].update(inventory=[], equipped="fists")
        self.h.mem["humanMode"] = "hold"
        self.assertFalse(self.h.station_patrol())
        self.assertIsNone(self.h.mem["humanMode"])
        self.assertEqual(self.h.mem["desiredWeapon"], "carbine")
        self.assertTrue(self.h.mem["focusCombat"])
