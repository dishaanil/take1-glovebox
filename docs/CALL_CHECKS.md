# Things to check on the next real call

Before calling: `.env.local` has `AGENT_PROFILE=glovebox` and
`MODEL_PRESET=glovebox`, and `lk agent dev --deployment production` is running.
Watch that terminal while you talk: each tool call is logged.

| # | From | Say or do | Pass if |
|---|---|---|---|
| 1 | Decision 1 (Deepgram numerals) | "Fuel was five twelve forty" | The log shows `log_expense ... 512.40` and the readback says five hundred twelve dollars and forty cents |
| 2 | Decision 1 | "Fifty two eight net" or "a hundred forty gallons" | Gallons come through as digits, not words |
| 3 | Decision 2 (Gemini Flash) | Two expenses in one breath | Exactly two `log_expense` lines, no empty or $0 one |
| 4 | Decision 2 | Note the pause before each reply | Write down roughly how long; this is the number decision 5 has to cover |
| 5 | Decision 3 (endpointing) | "Fuel was five twelve... (pause a second)... forty" | It waits for "forty" instead of jumping in after "twelve" |
| 6 | Decision 3 | Short, quick answers ("yep", "nope") | Replies still feel quick, not sluggish |
| 7 | Decision 3 | "Uh, hang on..." then wait about 3 s, then finish the sentence | It doesn't answer the "uh" |
| 8 | Decision 4 (silence) | Log an expense, hear the readback, then say nothing | At about 10 s: "Still there, Mike? No rush if you're driving." |
| 9 | Decision 4 | Stay quiet | About 10 s later: "I've still got that waiting on your yes. Want me to save it?" |
| 10 | Decision 4 | Stay quiet | About 10 s later: the "haven't saved anything you didn't confirm" goodbye, then the call hangs up |
| 11 | Decision 4 | Start over; at the first check-in, say "yeah I'm here" | The count resets and the call carries on normally |
| 12 | Decision 5 (filler) | Ask "What's coming up?" and listen to the start of the reply | If there's a wait, you hear "Let me check your schedule." **before** the answer, never after it |
| 13 | Decision 5 | Log an expense | At most one filler line per turn, and quick turns get none. In the agent terminal, look for `filler after 1.0s` |
| 14 | Decision 5 | Interrupt while the filler is playing | It stops and listens to you |
| 15 | Decision 6 (keyterms) | "Got to Harlan Point at seven fifty" and "Corliss pushed my pickup" | The agent log shows the facility names spelled right |
| 16 | Decision 9 (latency) | Any call; hang up normally | The agent terminal shows `turn latency ...` per reply and `call latency over N turns: median ..., worst ...` at the end. Write the median down for the pitch |
| 17 | Simulation fixes | "That's it, thanks" at the end | One goodbye, then the call hangs up (not silent, not twice) |
| 18 | Simulation fixes | "Reefer alarm, won't make Southmere till five AM" | It's recorded as a delay with the alarm as the reason, and warns about the 6 AM Corliss pickup |
