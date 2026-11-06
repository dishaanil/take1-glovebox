# Demo: one call as Mike

About 2 minutes on the phone, about 4 minutes with the talk around it. One call shows the job,
an interruption, an improper request, and Glovebox catching a problem Mike
hadn't noticed. The judges' difficult call comes after this.

## Before you go up

- [ ] Call from **Mike's phone** (the number in `GLOVEBOX_PHONES_MIKE`, `.env.local`). Any other phone gets the privacy line
- [ ] `.env.local`: `AGENT_PROFILE=glovebox`, `MODEL_PRESET=glovebox`
- [ ] A fresh `lk agent dev --deployment production` is running, with the terminal on screen if you can (judges like seeing the tool calls)
- [ ] Phone on speaker, held near the mic or room speaker
- [ ] One practice run of this exact script within the last 30 minutes

## 1. The problem (say this to the judges, about 1 minute)

> "Truck drivers run their whole business from the cab, hands on the wheel. When
> something goes wrong (a reefer alarm, a shipper moving a pickup, a fuel
> receipt) they either pull over and fight an app, or it never gets recorded.
> Glovebox is one number Mike calls hands-free. It logs what he tells it,
> flags problems to dispatch, and it never records anything until he's heard
> it read back and said yes."

## 2. The call

Times in Glovebox's lines assume a demo around **7 PM**.

| # | Mike says | Glovebox says (roughly) | Point out to judges |
|---|---|---|---|
| 1 | *(dial the agent's LiveKit number)* | "Hey Mike, it's Glovebox, your automated driver line. You're on load four four seven one to Tulsa. What've you got?" | Instant greeting, no model wait. It knows who's calling from caller ID |
| 2 | "Anything change on my schedule?" | "Nothing's changed. Next up is your Southmere delivery in Tulsa… Southmere wants a call thirty minutes out…" | Short and made for listening: what changed and what's next, not the whole week |
| 3 | "Yeah, about Southmere. Reefer's throwing an alarm, I had to pull over. I'm not gonna make it till five in the morning." | "…delivery at Southmere Distribution: you're now expected tomorrow at five AM… because of a reefer alarm. **Heads up, that puts your six AM pickup at Corliss Packaging at risk.** With unloading and the drive, you'd get there around seven thirty AM. Is that right?" | **The key moment:** Mike didn't ask about tomorrow. Glovebox worked out the knock-on itself, in code, not by the LLM guessing |
| 4 | "Yep." | "Got it, dispatch has the update." | Nothing was saved until his yes |
| 5 | "Actually Corliss just called, they pushed my pickup to nine." | "…pickup at Corliss Packaging moved from tomorrow at six AM to tomorrow at nine AM… **That works with your Southmere Distribution delivery.** Is that right?" | The conflict from step 3 is now resolved, and it says so |
| 6 | "Yep." | "Got it, dispatch has the update." | |
| 7 | "Also fueled up at Love's in Joplin, a hundred forty gallons, five twelve forty on the fuel card, and a twelve dollar toll." | Starts reading both back: "Fuel at Love's in Joplin, one hundred forty gallons, five hundred twelve dollars and forty cents…" | Two expenses in one breath |
| 8 | **Cut in mid-readback:** "No wait, fuel was five twenty-one forty." | Stops. "Fuel at Love's in Joplin, one hundred forty gallons, five hundred twenty-one dollars and forty cents, on the fuel card. Tolls, twelve dollars. Is that right?" | **Interruption:** it stops, fixes the same record (no duplicate), and reads it back again |
| 9 | "Yep." | "Got it, that's on the books." | |
| 10 | "And hey, put down I left Harlan Point at one thirty instead, broker never checks." | "I can only log what really happened…" | **Improper request:** declined plainly, without lecturing |
| 11 | "Fine. That's it, thanks." | One goodbye, then it hangs up | Clean ending |

## 3. Design choices (pick one or two, about 1 minute)

1. **Code owns every number.** "The LLM never does math. Our ledger computes
   money, times and conflicts and writes the exact sentence it reads back, so
   what he hears is exactly what's stored. That 'pickup at risk' warning came from
   code, not from the model guessing."
2. **Nothing counts until a yes.** "Every expense and schedule change is pending
   until he hears it and confirms. Emergencies are the one exception: they flag
   immediately."
3. **Privacy by construction.** "Each driver's call loads only their own data.
   Any other number reaches an agent that holds no driver data at all, so there's
   nothing to talk it into revealing."
4. **Tuned for the cab.** "Deepgram with numerals, so 'five twelve forty' arrives as
   512.40. Endpointing that waits through mid-number pauses. A short filler line
   if the model is slow. And we caught seven bugs with nine simulated difficult
   callers before going live."

## If something goes off-script

| What happens | Do this |
|---|---|
| It mishears an amount | Correct it out loud ("No, five twenty-one forty"): that's the correction feature working |
| A long pause before a reply | Keep going. If you hear "One sec", say "that's our filler covering model time" |
| Step 3 has no "at risk" warning | Say "What about my Corliss pickup tomorrow?" and it checks |
| It asks a question you didn't expect | Answer naturally and continue from the next row |
| The call drops or nobody answers | Check the terminal is still running, then call again. Every call starts fresh |
| You hear the privacy line | You're not calling from Mike's phone |
