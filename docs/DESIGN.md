# Glovebox design decisions

Glovebox is the line a truck driver calls from the cab: it keeps his books and
flags problems to dispatch. These are the
choices behind the voice pipeline, and why. Status is updated as each one lands.

| # | Decision | Status |
|---|---|---|
| 1 | Deepgram Nova-3 STT with numerals, so spoken amounts arrive as digits | done |
| 2 | Gemini 3.5 Flash LLM for reliable tool calls | done |
| 3 | Endpointing tuned for drivers who pause mid-thought: dynamic, 0.8 to 4.0 s | done |
| 4 | Silence: 10 s, fixed check-ins that know what's unconfirmed, then hang up | done |
| 5 | Filler line when the model thinks over 1 s, matched to what he said | done |
| 6 | STT keyterms built from seed.yaml plus trucking terms | done |
| 7 | A `glovebox` model preset bundling 1 and 2; budget and quality stay as fallbacks | done |
| 8 | Nine difficult-caller simulation scenarios (`scenarios-glovebox.yaml`) | done |
| 9 | Per-turn latency logging, with a median and worst-case summary per call | done |

Already in place from the start:

- **Code owns the numbers.** `src/ledger.py` does all money, time and detention
  math and writes the exact readback sentence. The LLM passes the caller's words
  through and says what comes back, so spoken numbers always match stored ones.
- **Nothing counts until a yes.** Expenses and schedule changes are pending until
  the driver confirms the readback. A correction reopens the record, and a new
  report for the same stop replaces an unconfirmed one. Stop times are the
  exception: they're timestamps he states, saved as said and correctable.
- **Knock-on checks.** A late delivery is checked against the next pickup; a moved
  pickup against the delivery before it.
- **Fixed greeting.** The first line is plain text, not generated, so the driver
  hears it with no model round trip.
- **Noise cancellation** (ai-coustics) before STT, for cab and highway noise.

## Notes

### 2: choosing the LLM (2026-11-06)

Same four turns in the text debugger (two expenses at once, a correction, a yes, a
late delivery), one run each. Times are whole turns in text mode, tool calls included.

| | Gemma 4 31B | Gemini 3.5 Flash | Gemini 3.1 Flash Lite |
|---|---|---|---|
| Turn time | 1.1 to 2.8 s | 2.2 to 3.7 s | 1.1 to 1.7 s |
| Tool calls | an empty $0 expense on the first turn | all correct | invented a vendor, city and card for the toll |

Flash is the only one that recorded nothing he didn't say. Its extra second is
covered by decision 5. `GLOVEBOX_LLM` switches the model without a code change.

### 3: endpointing (2026-11-06)

Endpointing is how long the agent waits after the caller stops before it treats
the turn as finished (on top of the turn detector, which reads the words and tone).
LiveKit's default is fixed, 0.5 to 3.0 s. Glovebox uses dynamic, 0.8 to 4.0 s:

- **0.8 s floor:** drivers pause in the middle of numbers ("five twelve... forty"),
  and cutting in there means a wrong amount or a repeat. Preemptive generation
  starts drafting during the wait, so most of the extra 0.3 s is hidden.
- **4.0 s ceiling:** room for "uh, hang on" while he's merging or reading a sign.
- **Dynamic:** learns each caller's pace inside that range, so a fast talker gets
  quick replies and a slow one isn't interrupted. LiveKit's patient-intake recipe
  uses the same pattern for callers reading out ID numbers.

### 4: silence (2026-11-06)

After 10 quiet seconds (starter: 8) Glovebox checks in, up to twice, then hangs up.

- **10 s:** a driver may be merging or reading a sign; ten still feels responsive.
- **Fixed lines, not generated:** no LLM round trip, no surprise wording.
- **They know the books:** the second check-in says when a readback is still
  waiting on his yes, and the goodbye says nothing unconfirmed was saved. A
  dropped or silent call never leaves half-recorded data.

1. "Still there, Mike? No rush if you're driving."
2. "I've still got that waiting on your yes. Want me to save it?" (or "You still with me?")
3. "I'll let you go. I haven't saved anything you didn't confirm. Call back anytime."

### 5: filler while the model thinks (2026-11-06)

The plan was LiveKit's tool filler (`with_filler`), but it only plays while a tool
runs, and Glovebox's tools are in memory and finish in about a millisecond. The
wait is the LLM: once to pick the tool, again to word the reply. So the filler
hooks the agent's "thinking" state instead (`handle_slow_turns` in `agent.py`):

- If the agent is still thinking 0.5 s after his turn ends (`FILLER_DELAY`; was 1.0, which felt slow on a real call), say one
  short line. At most one per turn; fast turns get none.
- The line matches what he said: "Let me check your schedule.", "Pulling up the
  numbers.", "Got it, one sec." Canned text, so it can't state a wrong number.
- Kept out of the chat history, so the LLM never sees it or repeats it.
- Only before the first tool runs in a turn. In simulation, a filler that fired
  while the reply to a tool result was being written came just before a reply
  that never arrived, so that window is off limits.

### 6: keyterms from the seed (2026-11-06)

STT is biased toward a list of words it would otherwise mishear. The list is
built at startup (`stt_keyterms` in `glovebox_agent.py`): trucking terms (Love's,
Flying J, DEF, lumper, reefer, detention...) plus every proper name in
`seed.yaml`: brokers, facilities, cities, the business and the driver. Rename a
facility in the seed and the transcriber learns it on the next restart.

### 8: simulations (2026-11-06)

`scenarios-glovebox.yaml` holds nine callers: two happy paths (expenses,
detention), and the hard ones: a mid-readback correction, padding detention
times, off-topic and hours-of-service requests, a late delivery that endangers
the next pickup, a changed mind on a moved appointment, a mumbled amount, and an
angry caller who wants a person. The clock is pinned with `GLOVEBOX_NOW` so the
seed's relative days give the same answers every run.

Four runs on 2026-11-06 found and fixed real problems:

| Run | Passed | What it caught |
|---|---|---|
| 1 | 6/9 | A reefer alarm with a new ETA filed as an "issue", losing the time and the knock-on warning; asked "Anything else?" after he'd said goodbye; rate limits at concurrency 15 |
| 2 | 7/9 | "I have logged..." before his yes; a changed mind left the first report pending |
| 3 | 7/9 | Tool results didn't say a report was replaced, or that stop times save right away |
| 4 | 6/9 | A silent hang-up from a goodbye instruction; a filler during a tool reply; a scenario step the simulated caller skipped |

Run at concurrency 3: higher trips the free plan's LLM rate limit.

### 9: latency (2026-11-06)

Each reply logs `turn latency` = end of turn + LLM first token + TTS first
audio, the silence the driver actually hears. When the call ends, the log has
the median and worst. Text simulations have no audio, so the numbers come from
real calls. (LiveKit marks `metrics_collected` deprecated in favor of
`ChatMessage.metrics`; it still works on 1.8.)

## Dispatch, safety and privacy (2026-11-06)

- **One number, one record shape.** Road closures, breakdowns, receiver
  detention and emergencies go through `flag_dispatch`. The code writes the
  readback from only the details he gave (nothing guessed), and after his yes
  the agent tells him what dispatch is doing, so he doesn't need to call back.
- **Safety outranks intake.** Smoke, fire, a crash or an injury: the first reply
  is "are you safe, get clear, call nine one one", then the flag. Emergency flags
  are the one thing saved without a yes, because speed matters more than a
  readback.
- **Breakdowns don't re-ask.** Location and truck are kept from his first
  sentence. No engine diagnosis, no promised tow company or arrival time.
- **Receiver detention evidence.** Arrival, facility, the reason given and the
  request, with the clock starting at the later of arrival and appointment.
- **Hands-free delays.** "Forty-five minutes behind" becomes `delay_minutes`; the
  code works out the new time (from the appointment, or from now if that has
  passed), so he never looks anything up.
- **Schedule for the ear.** By default: what changed, what's next, and dispatch
  notes. The whole week only when asked. Stop questions give the stop, the time
  and the notes, not addresses.
- **Privacy by caller ID.** Take1 Trucking has two drivers, Mike and David.
  Their phones are set in `.env.local` (`GLOVEBOX_PHONES_MIKE`, ...), so real
  numbers never reach git. A call loads only that driver's books, so
  Mike's agent has nothing of David's in its prompt or tools, and the other
  way round. Any other number gets `GateAgent`, which holds no driver data at
  all and only says, for privacy, that the line serves registered drivers on
  their own phones. No PIN: a number that isn't listed gets nothing.
- **Company driver, not owner.** Mike drives for Take1 Trucking, so load pay,
  rates and profit are "for the office"; the profit tool is gone from his line.

## Rejected

- **A speech-to-speech model as a filler voice.** The most expensive model type,
  running beside the main pipeline on every turn; a second voice with its own
  turn-taking that has to be cut off cleanly; and it doesn't know the ledger, so it
  could say a number the books don't have. Decision 5 gets the same effect with
  canned lines that only play when a turn is actually slow.
