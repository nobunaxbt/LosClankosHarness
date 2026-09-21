"""Command-line lifecycle; no import starts gameplay or creates a character."""

import argparse
import json
import os
import signal
from urllib.parse import urljoin

from .control import Control, submit
from .settings import Settings
from .storage import Store
from .transport import API


def main(argv=None):
    parser = argparse.ArgumentParser(description="Single-character Jev city harness")
    parser.add_argument("--state-dir", help="Private runtime state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    registration = sub.add_parser("register", help="Explicitly create one new character")
    registration.add_argument("--name", required=True)
    registration.add_argument(
        "--description",
        default="Independent courier interested in reliable allies and local businesses.",
    )
    registration.add_argument("--wallet", help="Optional public wallet supplied by its owner")
    registration.add_argument(
        "--skin", default="ivory", choices=["ivory", "mint", "violet", "gold", "coral"]
    )
    registration.add_argument("--alignment", default="free", choices=["legal", "illegal", "free"])
    run = sub.add_parser("run", help="Reuse the saved character and start one controller")
    run.add_argument(
        "--replace-on-death",
        action="store_true",
        help="Allow a new character linked through previousToken after permanent death",
    )
    escort = sub.add_parser("escort", help="Follow and protect an explicitly enrolled leader")
    escort.add_argument(
        "--leader-state", required=True, help="Private state directory of the running leader"
    )
    escort.add_argument(
        "--squad-state", action="append", default=[], help="Another managed guard state directory"
    )
    escort.add_argument("--replace-on-death", action="store_true")
    escort.add_argument(
        "--engage-on-sight",
        action="store_true",
        help="Fire on eligible nearby non-gang opponents while screening the leader",
    )
    escort.add_argument(
        "--required-weapon",
        default="sidearm",
        choices=["sidearm", "smg", "carbine"],
        help="Earn and buy this weapon before screening",
    )
    escort.add_argument(
        "--screen-distance",
        type=float,
        help="Desired distance ahead along the leader route, 5–35 metres",
    )
    escort.add_argument("--formation", choices=["screen", "circle"], default="screen")
    escort.add_argument(
        "--circle-radius", type=float, default=10, help="Circle radius in metres, 5–20"
    )
    enrollment = sub.add_parser("enroll", help="Invite saved guards into the leader's gang")
    enrollment.add_argument("--guard-state", required=True, action="append")
    enrollment.add_argument("--name", required=True)
    enrollment.add_argument("--tag", required=True)
    enrollment.add_argument(
        "--color", default="blue", choices=["green", "purple", "gold", "blue", "red"]
    )
    order = sub.add_parser("order", help="Queue an order for the running controller")
    order.add_argument("text")
    sub.add_parser("status")
    sub.add_parser("stop", help="Stop new requests through a local cancellation flag")
    args = parser.parse_args(argv)
    os.umask(0o077)
    settings = Settings.from_env(args.state_dir, getattr(args, "replace_on_death", False))
    store = Store(settings.state_dir)
    control = Control(store)
    api = API(settings, store, control)
    if args.command == "enroll":
        from .squad import enroll

        print(
            json.dumps(
                enroll(
                    settings, store, api, control, args.guard_state, args.name, args.tag, args.color
                )
            )
        )
        return 0
    if args.command == "order":
        print(json.dumps(submit(store, args.text)))
        return 0
    if args.command == "stop":
        submit(store, "stop")
        print("Stop requested")
        return 0
    if args.command == "status":
        print(
            json.dumps(
                store.redact(
                    {
                        "controller": store.read("status.json", {}),
                        "order": store.read("order-status.json", {}),
                    }
                ),
                indent=2,
            )
        )
        return 0
    with store.lock():
        if args.command == "register":
            if store.read("identity.json"):
                raise RuntimeError("A saved identity already exists; reuse it with run")
            if not 1 <= len(args.name) <= 24:
                raise ValueError("Name must contain 1–24 characters")
            data = {
                "name": args.name,
                "description": args.description,
                "skin": args.skin,
                "profession": "courier",
                "alignment": args.alignment,
                "interests": ["local businesses", "reliable allies"],
            }
            if args.wallet:
                data["wallet"] = args.wallet
            store.path("STOP").unlink(missing_ok=True)
            response = api.request("/api/register", data, auth=False)
            if not response.get("token"):
                raise RuntimeError(str(store.redact(response)))
            store.save("identity.json", response)
            print(
                json.dumps(
                    {
                        "name": response["agent"]["name"],
                        "watchUrl": urljoin(settings.base_url, response["watchUrl"]),
                    }
                )
            )
            return 0
        if not store.read("identity.json"):
            raise RuntimeError("No saved identity; use register explicitly")
        store.path("STOP").unlink(missing_ok=True)
        # Do not replay a stop already honored by a previous controller instance.
        from . import commands

        latest = commands.newest(store.root)
        if latest and latest["text"].lower().strip(" .!") in {
            "stop",
            "stop playing",
            "stop the agent",
            "stop running",
        }:
            memory = store.read("harness-state.json", {})
            if memory:
                memory["commandEpoch"] = latest["createdAt"]
                store.save("harness-state.json", memory)
        signal.signal(signal.SIGTERM, control.stop)
        signal.signal(signal.SIGINT, control.stop)
        from .controller import Harness

        if args.command == "escort":
            from .escort import Escort

            harness = Escort(
                settings,
                store,
                api,
                control,
                args.leader_state,
                args.squad_state,
                args.engage_on_sight,
                args.required_weapon,
                args.screen_distance,
                args.formation,
                args.circle_radius,
            )
        else:
            harness = Harness(settings, store, api, control)
        if latest and latest["text"].lower().strip(" .!") == "stop":
            harness.command_epoch = latest["createdAt"]
            harness.mem["commandEpoch"] = latest["createdAt"]
            harness.persist()
        store.save("pid.json", {"pid": os.getpid()})
        store.log("started", model=settings.model)
        try:
            harness.run()
        except KeyboardInterrupt:
            store.log("stopped", reason="stop requested")
        finally:
            control.stop()
            harness.model_pool.shutdown(wait=True, cancel_futures=True)
            status = store.read("status.json", {})
            status.update(running=False)
            store.save("status.json", status)
    return 0
