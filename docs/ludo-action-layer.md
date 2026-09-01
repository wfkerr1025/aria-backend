# Ludo.ai action layer

Drives the Ludo.ai API the way the Blender layer drives Blender: a vocabulary
ARIA owns, mapped to primitives that exist, with a model never deciding what
runs.

## Everything here was read, not remembered

Fetched from `https://api.ludo.ai/api-documentation/swagger.json` (Ludo.ai API
**0.9.9**) before a line was written. Four earlier guesses about the Unity CLI
were wrong — `--project` doesn't exist, `--mode` is test-only, `list` returns
`data.tools`, a pager would hang — and each cost a round of "it says it worked
and nothing happened".

The spec is **Swagger 2.0**: bodies are `parameters[in=body]`, types live under
`definitions`. Auth was verified live against the one free endpoint:
`GET /auth/validate-api-key` → **204, empty body** for a valid key. (Our code
comments said spec 0.9.2; that was stale.)

## Generation costs real money

This is the one way this layer differs from every other tool ARIA drives. Every
POST carries an `x-credit-action` and spends credits the user cannot get back.
Blender is free to run and can be run again. So:

- **Nothing retries a generation automatically.**
- **`request_id` is sent on every call** — the API uses it to recognise a repeat
  and not charge twice.
- **Every enum is checked before the first call**, not before the call it
  belongs to. Found by a test: an invalid `texture_size` used to raise on the
  *second* request, which meant the first had already generated an image and
  charged for it. A parameter this layer could refuse for free must never cost a
  credit.
- **The tool gate is stricter in effect than Blender's**, because a sentence
  read too eagerly here is not an inconvenience, it is a charge.

## The modules

| Module | Job |
| --- | --- |
| `ludo_client.py` | Auth, one call, job polling, downloads. Endpoints and enums from the spec. |
| `ludo_actions.py` | The high-level verbs. Each returns an answer, never raises. |
| `ludo_nl_mapping.py` | A sentence → one action, or `None`. |
| `ludo_asset_pipeline.py` | Download → validate → Unity. |

The Unity half is **not** in this layer: it is `backend/unity/unity_delivery.py`,
shared with Blender. The Pipeline argument names in it were read out of the
package's own C# and are worth exactly one copy.

## Four things the spec asked for that the API does not do

Written down because the alternative is a function that looks like it works.

**1. There is no text-to-3D endpoint.** `/assets/3d-model` requires an `image`.
So `generate_model()` is **two calls** — an image, then the conversion — and
both are named in `steps`, because two calls is two lots of credits.
`generate_character()` and `generate_vehicle()` are the same, differently framed.
Sprite sheets and video are two calls for the same reason.

**2. An image has no resolution setting.** `/assets/image` takes `aspect_ratio`
and nothing else about size. `generate_texture(resolution=...)` therefore
**refuses** rather than accepting a number and returning whatever came back.
(`texture_size` 1024/2048 is real, but belongs to the 3D conversion.)

**3. A voice needs words.** `/audio/voice` requires `voice_description` **and**
`text`. `generate_voice(prompt)` alone has nothing to say, so it asks.

**4. `generate_environment` is an image, not a mesh.** Converting a scene to one
mesh gives a diorama nobody can walk through. Pass `three_d=True` when a prop is
what was meant.

## Using it

```python
from backend.ludo import ludo_actions, ludo_asset_pipeline

made = ludo_actions.generate_vehicle("a red sports car", style="Low Poly")
if made["success"]:
    ludo_asset_pipeline.download_and_deliver(made, name="SportsCar", prefab=True)
```

Every action returns the same shape:

```python
{"success": bool, "ran": bool, "error": str|None,
 "kind": str, "url": str|None, "result": dict|None, "steps": [...]}
```

`ran` means **a generation was attempted and credits may have been spent**. A
refusal before any call has `ran=False` and never claims otherwise.

## Natural language

```python
from backend.ludo import ludo_nl_mapping as mapping

mapping.map_text("make a low poly red sports car in Ludo")
# {"action": "generate_vehicle",
#  "params": {"prompt": "red sports car", "style": "Low Poly"},
#  "needs": [],
#  "summary": "ask Ludo.ai for a vehicle model of red sports car, in the Low
#              Poly style (two generations, because Ludo has no text-to-3D…)"}
```

**Ludo has to be named** — "in Ludo", "with Ludo", "using Ludo", "Ludo, …".
Naming **Blender** returns `None`, exactly as naming Ludo returns `None` in the
Blender mapper; two tools that both make 3D assets must not both answer one
sentence, and naming both is a reason to ask rather than to pick.

Questions never spend anything. `"in Ludo, what does a credit cost?"` and
`"can you tell me how to use Ludo"` both return `None`; `"can you make a car in
Ludo?"` builds, on the same rule as the Blender layer.

**Format words are separated from subject words.** `"sprite sheet"` names the
output and comes *out* of the prompt; `"car"` names the thing and stays in.
Found by a test: before that split, `"make a sprite sheet in Ludo"` produced
`prompt="sprite sheet"` — asking Ludo for a picture of the words.

When a sentence names a format but no subject, the result carries
`needs: ["prompt"]` rather than a guess:

```
"Make a sprite sheet in Ludo"  →  generate_sprite_sheet, needs=["prompt"]
```

Of *what*? Answered by asking. Guessing costs money, and handing it to a model
gets an offer to help.

`summary` always states the cost — "one generation" or "two generations" — so a
person sees it before it happens rather than on their invoice.

## Where the files go

`Documents/ARIA/Ludo` by default, settable on the Ludo config page under Plugins,
or `ARIA_LUDO_OUTPUT`. This is the shared `plugin_settings.output_dir("ludo")`.

> **Deviation from the brief, deliberate.** The brief said `aria_output/ludo/`.
> That is the folder you objected to last session ("I don't think having the
> output in Aria's Root Folder is a good idea"), and the shared resolver you
> asked for was built to replace it. The override mechanism the brief names —
> env var and plugin `output_dir` — is exactly what is used; only the default
> differs. Say the word and it moves back.

Downloads are streamed to a `.part` and moved into place at the end, capped at
512MB, and **validated by their bytes** before Unity is asked to look at them. An
expired link or an error page served with a 200 produces a file that exists, has
a plausible name, and is not an asset — that passes "is it there?" and fails an
import much later, somewhere less obvious. A file that fails validation is
**kept**, not deleted: it was paid for.

## Wired into chat

`turn_orchestrator._ludo_reply` sits after `_blender_reply`, ahead of intent
detection and model routing. Measured before it did:

```
Create a stylized cartoon girl with bright red hair, a large pink bow, a pink
dress, big expressive eyes, and a confident heroic pose in Ludo

  -> phi-3-mini: "As an AI, I can't directly create images, but I can guide you
                  through the process..." followed by a paragraph of prose.
```

Nothing was generated. Now that sentence produces a 533KB image in the
configured folder.

**Two gates stand in front of the spending, and both are the user's own act:**
the sentence must name Ludo (naming Blender hands the turn away), and the plugin
must be installed, enabled and hold a key. Beyond that a named request is treated
as meant — "create X in Ludo" is not ambiguous — and the reply always states what
was spent.

To require a confirmation turn instead, reply with `plan["summary"]` in
`_ludo_reply` and run on the next message. `map_text` costs nothing, so nothing
is spent until the second turn.

## A generation must not freeze the app

The WebSocket handler reads packets with

```python
async for raw in self.websocket:
    await self._dispatch(packet)
```

so **nothing else is read while a turn runs** — heartbeats included. Measured: a
25-second image generation stalled every `heartbeat_ack` until it finished, and
a 3D request (two calls) took **two minutes** with the UI showing nothing at
all. Under the original 900-second poll ceiling, a queued job would have frozen
the app for fifteen minutes.

Three changes, all in this layer:

- **The chat deadline is 120 seconds**, not 900. `JOB_MAX_SECONDS` (900) remains
  for a script with its own patience.
- **Progress is reported** — `generating`, then `downloading` — through the
  `on_status` callback the orchestrator already threads to the transport. A
  silent thirty seconds is indistinguishable from a crash.
- **A job that outruns the turn is not lost.** `LudoStillRunning` carries the job
  id, the reply hands it over, and `"collect <id> in Ludo"` fetches it later.
  Collecting **costs nothing** — the work was charged for when it was queued,
  which is what makes a timeout an inconvenience rather than a loss.

The deeper cause is the receive loop serialising every packet behind the current
turn. That is not this layer's to fix and predates it; the fix above keeps a
Ludo turn short enough that it does not matter much. Making the handler dispatch
chat turns as their own task would fix it properly, for every slow turn.

## Tests

`backend/tests/test_ludo_actions.py` — 176 tests covering the client, the
actions, the mapping and the pipeline. **No test touches the real API**: an
autouse fixture replaces `requests.post`/`get` and fails loudly if anything
reaches for the network. A suite that quietly billed you would be a suite you
could not run twice.

Three bugs these tests caught before you saw them: the enum-after-spending
problem above; `_guard` erasing the wrapped signature, so a bad keyword would
have been a `TypeError` at runtime instead of a test failure; and
`trigger_unity_import` returning Unity's `globalId` where the asset path
belonged — which **Blender had too**, since they share that code.
