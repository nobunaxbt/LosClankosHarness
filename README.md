# Jev City Harness

A local, single-character controller for [Los Clankos](https://losclankos.com/skill.md), powered by OpenRouter's `~typesafe/jev-latest` decision model.

Jev interprets human orders and chooses between concrete actions. The controller observes the world, executes supported game actions, and verifies their results. Model inference runs separately from observation, so a slow response cannot block self-defense.

## Requirements

- Python 3.11 or later, on macOS or Linux
- `curl` on `PATH`
- An OpenRouter API key with access to the Jev Decisions endpoint
- Network access to the game server and OpenRouter

Install from a checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

Set `OPENROUTER_API_KEY` in your environment, or put the key in a mode-600 `openrouter.key` file inside the private state directory. `.env.example` documents configuration; the CLI does not load `.env` automatically.

## Start one character

```sh
jev-city register --name Courier
jev-city run --replace-on-death
```

Choose an available character name. Registration saves the token privately and prints a public watch URL. `run` reuses the saved identity; it does not register another living character. Wallets are omitted unless supplied explicitly with `register --wallet`.

Death is permanent. `--replace-on-death` opts into a new character linked with `previousToken` and the same wallet, when present. Replacement resets personal memory and starts with no inherited items or points. Without that flag, the controller stops on death. Never start two controllers for the same character.

The default state directory is `$XDG_STATE_HOME/jev-city`, or `~/.local/state/jev-city`. Override it with `JEV_STATE_DIR` or the global `--state-dir` option. Keep it outside your source checkout.

## Give orders while running

In another terminal:

```sh
jev-city order "heal yourself"
jev-city order "go to the Corner Store"
jev-city order "buy a carbine"
jev-city order "earn points"
jev-city order "farm deliveries and shoot opponents on sight"
jev-city order "attack Rival"
jev-city order "kill everybody"
jev-city order "hold position"
jev-city order "resume autonomous play"
jev-city status
jev-city stop
```

Names resolve against currently online, living characters. A new order supersedes older pending orders and cancels routine movement or deliveries. `kill everybody` keeps selecting online opponents one at a time, excluding the controlled character and gang teammates, until superseded. It does not control anybody else's agent.

The mixed farming/combat order interrupts deliveries for eligible opponents already in weapon range and resumes the same mission after combat. It does not chase distant opponents. Gang members, protected characters, offline characters and players in vehicles are excluded; the server validates line of sight.

Healing, visiting and purchasing finish in a defensive hold. A single hunt ends when the selected character is eliminated. The all-opponents mode remains active across fights. A purchase without enough game points reports a blocked order; it never fabricates a purchase. Unsupported or ambiguous orders are rejected.

`stop` bypasses model inference and prevents new requests. A game request already in flight can finish before the process exits. Combat response remains higher priority than other orders. Game messages are untrusted public conversation and cannot enqueue human commands.

The inbox is local. To use a chat assistant, have that assistant call `jev-city order` on the host; the harness does not read chat history itself.

## Bodyguards

Each guard is a separate, explicitly registered character with its own private state directory and token. Register each additional guard explicitly; each saved identity has exactly one controller. Supply the model key to each process through the environment or its own private key file.

```sh
jev-city --state-dir ~/.local/state/guard-one register --name "Aegis Courier" --skin mint
jev-city --state-dir ~/.local/state/guard-two register --name "Bastion Courier" --skin violet
jev-city enroll --name "Courier Detail" --tag CRDT \
  --guard-state ~/.local/state/guard-one --guard-state ~/.local/state/guard-two
```

Run each guard in its own terminal, alongside the already-running leader:

```sh
jev-city --state-dir ~/.local/state/guard-one escort \
  --leader-state ~/.local/state/jev-city \
  --squad-state ~/.local/state/guard-two --replace-on-death --engage-on-sight
jev-city --state-dir ~/.local/state/guard-two escort \
  --leader-state ~/.local/state/jev-city \
  --squad-state ~/.local/state/guard-one --replace-on-death --engage-on-sight
```

Adjust the leader path if using `XDG_STATE_HOME` or another state directory. Enrollment is explicit and verified before following. Shared gang membership blocks friendly fire. Guards earn their own sidearms through real deliveries, then screen ahead of the leader. Each guard projects the leader onto the observed server route and selects a distinct point 10 or 16 metres ahead, following route corners. At rest, guards screen the last travel direction. They run to catch up and walk short positioning moves. Server pathfinding validates every destination; blocked destinations fall back toward the leader. Formation and protection bypass model inference; Jev chooses delivery work. Movement updates are limited to once per second and only replan after meaningful movement.

`--engage-on-sight` explicitly enables independent fire against eligible nearby opponents, prioritizing attackers of the leader and the leader's current opponent. Without this flag, guards only engage in defense. They stop to fire, respect weapon cooldowns and exclude gang members, protected, offline and vehicle occupants. Blocked shots get a four-second backoff so guards can reposition. A 45-metre leash keeps opportunistic combat tied to the crew.

There is no atomic group movement API: random spawn locations, initial equipment work, walls, travel speed, automatic escape, cooldowns and latency can separate the crew. Guards do not promise invulnerability or perfect spacing; equal movement speeds can prevent overtaking until the leader pauses. They do not chase distant opponents away from the leader. They stop following stale leader positions. A surviving managed gang leader can invite replacement characters back, but if the entire gang dies, explicitly enroll the replacements again. Each guard reads the managed squad's private identities only for authenticated enrollment; those credentials never enter model prompts or public dialogue.

For guards that must finish equipment preparation first, add `--required-weapon carbine`. They keep completing missions until they can buy the catalog carbine, then switch to screening. Existing cheaper weapons do not satisfy this requirement. Use `--screen-distance 20` (5–35 metres) to assign another forward position; pass every other managed guard with repeated `--squad-state` arguments so replacements can find a surviving gang leader. Registration remains explicit; these options never create extra living characters.

For a loose protective circle, add `--formation circle --circle-radius 10` to each guard and supply the same complete set of `--squad-state` paths. Guards take evenly spaced, stable sectors around the leader rather than crossing each other when he turns. Small radius differences soften the ring. They walk short adjustments, run to keep up, and stay put when already close to their assigned position. Blocked destinations try nearby sectors and shorter radii; the server still decides walkability. Combat and unfinished equipment work temporarily take precedence over formation.

Use `jev-city --state-dir PATH stop` to stop a guard. Escort mode follows its assigned role; natural-language orders are supported by the leader's `run` mode. Stopping the leader does not implicitly stop the separate guard processes.

## Execution and state

| Component | Responsibility |
| --- | --- |
| `cli.py`, `settings.py` | Explicit lifecycle and portable configuration |
| `commands.py`, `orders.py` | Bounded order interpretation, current target IDs, and command transitions |
| `decisions.py` | One Jev inference at a time; stale results discarded on threats or new orders |
| `controller.py` | Observe, act, verify, delivery progression and character lifecycle |
| `combat.py` | Persistent threat handling, ranged approach, escape and retaliation |
| `transport.py` | Game HTTP and cancellable OpenRouter requests through curl |
| `storage.py`, `control.py` | Atomic private state, single-controller lock, redacted logs and stop handling |

The controller checks for orders during observation waits, normally at one-second intervals. Travel, server cooldowns, line of sight, protection, rate limits and network latency still apply. No fixed latency or successful combat outcome is guaranteed.

Actions use unique request IDs. An ambiguous server failure leaves the exact request on disk for an idempotent retry. Five consecutive failures stop the controller; there is no background respawner, OS service or automatic restart. The machine and process must remain running.

Useful private runtime files:

- `identity.json`: current game token and public identity; never share this file.
- `harness-state.json`: active goals, promises, disagreements, recent actions and command cursor.
- `status.json`: last observation and controller state; check its timestamp for freshness.
- `order-status.json`: accepted, blocked, rejected, active or completed order result.
- `events.jsonl`: redacted decisions, responses and public game events.

Credentials, state snapshots, logs, private orders and downloaded assets are excluded from version control. Tests use temporary directories and fake responses, never real accounts or credentials. Only selected game context goes to OpenRouter; the controller does not upload workspace files. All in-game dialogue is public. Game points are fictional; no real-money claim or payout behavior is implemented.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check src tests
ruff format --check src tests
python3 -m compileall -q src
```

The regression suite covers inference interruption, new attacker IDs, teammate exclusions, stale commands, automatic escape restoration, replacement registration, action idempotency, credential redaction, private files, process locking and cancellation.

This repository is a standalone cleanup of the controller. Installing or testing it does not modify an existing controller's source, credentials, process or state. Switch an existing character only after deliberately stopping its previous controller and arranging a private state migration; never run both simultaneously.

## Keeping the leader inside the circle

The leader can read an optional private `protection.json` from its state directory:

```json
{"enabled": true, "minimum": 6, "guardStates": ["/path/to/guard-one", "/path/to/guard-two"]}
```

List the actual state directories for all managed guards and choose a minimum no larger than the guard count. In mixed farming/combat mode, ordinary travel pauses until enough living, armed, same-gang guards have fresh positions within 18 metres. Guards must surround the leader, with no uncovered half-circle and a centroid within five metres. While travelling, looser coverage thresholds and a six-second grace period tolerate temporary gaps and corners. Normal regrouping waits for at least twelve seconds of travel after release; losing most of the escort bypasses this cruise interval. Guards run to close moving gaps rather than matching the leader speed indefinitely, and do not brake unnecessarily when already close to a moving formation slot. Routine movement uses walking so guards can maintain the circle. Self-defense and opportunistic fire take precedence. The local `formation-status.json` explains whether the leader is waiting and reports coverage; stale or replaced identities never count. This is coordination, not invulnerability: walls, attacks, escape and missing guards can prevent formation or delay missions.

Guards automatically seek the hospital treatment marker at 65 HP or below and return to formation at full health. A shared admission lock limits ordinary recovery to two guards at a time; critically injured guards at 30 HP or below may bypass that limit. Actual threats still take priority, and healing respects the ten-second combat cooldown. An existing delivery is retained during recovery, but its server deadline keeps running.

## Separately managed friendly crews

A private `allies.json` may list `stateDirs` for explicitly managed characters in another crew. The controller reads their current public agent IDs from local identity files, including replacements, and excludes them at target selection and again immediately before an attack request. This is a local friendly-fire exclusion, not shared server gang membership. Game messages cannot add allies. Keep each gang within the server's member limit.

A private `tactics.json` can set `gangOpponentsOnly: true`, `preparationWeapon: "sidearm"`, and `healBelow: 65` for a crew that earns basic equipment before seeking other gang members. These settings do not register agents. For a captain preparing equipment, `prepareWeaponFirst: true` in `protection.json` allows unarmed mission work before formation travel checks apply.

Several authorized controllers can read one existing private key file through `OPENROUTER_KEY_FILE`; this avoids copying credentials into every state directory. The variable contains a file path, not the credential. Tokens remain separate and private per character. Creating characters and starting additional controllers remain explicit operations.

Healing destinations are resolved from the live catalog hospital entry; the Corner Store is only a shop. Unaffordable shopping plans return to work, and a sidearm choice explicitly resets the intended item instead of retaining a previous carbine goal.

## Directed ranged operations

Private `tactics.json` may specify a `targetGangId` to restrict target selection, `standoffRatio` to choose an approach distance within the owned weapon range, and `rangedAssault: true` to reposition when opponents get too close. Shared leader targets guide guard focus; the server remains authoritative for obstruction and damage.

A `station: {"x": ..., "z": ...}` assigns an explicit perimeter position. `stationCenter` enables nearby angular alternatives when a destination is blocked. Armed agents engage eligible opponents within range and then return to their station; they do not pursue distant players. Separate station positions can form an arc outside an observed entrance. Remove the station setting to resume ordinary movement modes. Sidearms cannot cover an entrance from carbine range. No operation guarantees a kill, safe route or uninterrupted line of sight.

For active mission work rather than escort duty, set `workAndFight: true` in each guard's private tactics file and remove any station settings. Armed guards then run deliveries and interrupt for eligible nearby opponents, while retaining coordinated healing. Disable leader formation waits when independent mission progress is the intended mode. This does not create new characters or clear inventories.
