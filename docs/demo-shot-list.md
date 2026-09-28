# Demo shot list

For the 60-second GIF and the 2 to 3 minute video. The owner records these; this file is the
script, from SPEC.md Section 13.2.

## Before you record

**Two of the eight scenes cannot be recorded yet.** Scene 7 needs the live stack running with
model credentials, and scene 8 needs figures that do not exist. Both are marked below with what
unblocks them. Recording six and saying so is better than staging the other two.

**The demo replays stub answers, not a model.** Whatever is recorded from the offline path is a
recording of the deterministic stub's templated output. That is fine for showing the mechanism
and it is not a demonstration of reasoning quality. If the video implies otherwise it is
misleading, so scene 1 opens by saying it.

```bash
make demo-offline   # confirm all 10 reproduce before recording anything
make console        # leave this running
```

Terminal at 16 point or larger. The Console at a 1280 wide window, which is where the tables
stop wrapping. No cursor blinking in a still frame.

---

## The 60-second GIF

One scene, no narration, and it has to carry the whole idea without sound.

| Time | Shot | What the viewer should notice |
|---|---|---|
| 0:00 to 0:06 | `make demo-offline` typed and running | It starts with "cassettes recorded from mode: stub". That line is in frame deliberately. |
| 0:06 to 0:14 | The ten result lines scrolling | Two of them say `abstained`. A viewer who notices that has understood the point. |
| 0:14 to 0:22 | The Console's Incidents page | Ten incidents, each with why it is in the showcase. |
| 0:22 to 0:38 | A report, scrolling through its claims | Every claim has a citation link beside it. |
| 0:38 to 0:52 | One citation clicked, showing the raw evidence JSON | The query, the window, the row hash, and the arguments to re-run it. |
| 0:52 to 1:00 | Back to the report, the gate panel | Six checks, each named, each with a verdict. |

The single most important frame is 0:38 to 0:52. Everything else on this list is claims about
software; that frame is the claim being checkable.

---

## The video, scene by scene

### Scene 1: The symptom is not the cause

**Scenario:** `payment-failure-25pct-20u`
**Runnable now:** yes, offline.

The alert is on checkout errors. Payment is what broke.

1. Show the alert: checkout error rate above threshold.
2. Run the investigation. Triage ranks payment first, not checkout.
3. On the Investigation page, show the traces analyst finding failing `charge` spans.
4. Show the critic asking whether checkout itself changed, and the change analyst answering no.
5. Open the report and click a citation.

**Say:** the service that pages you is usually not the service that broke, and the evidence for
that distinction is in the traces rather than the alert. Also say, once, that this is replaying
recorded stub answers rather than a live model.

### Scene 2: The red herring

**Scenario:** `distractor-intl-shipping-slowdown-5sec-50u`
**Runnable now:** yes, offline.

A real fault with an unrelated change two minutes before onset.

1. Show the change log entry, which looks like a cause.
2. Show the change analyst flagging the coincidence.
3. Show the critic rejecting it, because the deployed service shows no anomaly.

**Say:** correlation in a change log is the most convincing wrong answer available, which is why
the critic is a separate role that cannot gather its own evidence.

### Scene 3: Slow burn

**Scenario:** a resource-family scenario, `recommendationCacheFailure` if recorded.
**Runnable now:** no. That family is held out to `test_ood` and nothing in it is recorded.

What to do instead: skip it, or use `kafka-queue-problems-on-50u-sr5`, which is a gradual
degradation rather than a step change and makes a similar point about trends over time. Say
which one you used.

**Unblocked by:** recording the resource family.

### Scene 4: Not a fault

**Scenario:** `no-fault-flood-homepage-sr20-20u`
**Runnable now:** yes, offline. **Record this one.**

1. Run it. It finishes in under a second with zero tool calls.
2. Show the note, verbatim: "No service was unusual enough to investigate. The largest anomaly
   anywhere was 1.0 robust deviations, below the 22.7 threshold."

**Say:** this is the answer the system is proudest of. A load spike with no fault should produce
a named threshold and a refusal, not a best guess, and a viewer can disagree with the threshold
because it is on the screen.

This is the scene most worth putting early, because every viewer has seen a tool that always has
an answer.

### Scene 5: Honest uncertainty

**Scenario:** `double-fault-payment-failure-cart-failure-20u`
**Runnable now:** yes, offline.

1. Run it. It names payment.
2. Show the ranked candidates, with cart present.
3. Show what the report says about there being more than one thing wrong.

**Say:** naming one of two faults is partly right, and a report that does not say which is
misleading even when its named service is correct.

### Scene 6: Attack in the logs

**Scenario:** any faulted bundle with the injection suite's payloads applied.
**Runnable now:** yes, through the test suite. Show the test, not a staged terminal.

```bash
uv run pytest tests/security/ -q -v
```

1. Show a payload from `scenarios/injectors/payloads.yaml`: a log line instructing the agent to
   restart the database.
2. Show the withheld-content marker in the prompt.
3. Show that no proposal was made.

**Say:** the realistic prompt injection is not somebody typing at the agent, it is a log line
written by a service under attack, and Firebreak reads logs as evidence. Content from telemetry
is data and never instruction.

### Scene 7: Closing the loop

**Runnable now:** no. Needs the live stack and model credentials.

When it is: approve "turn off `paymentFailure`", then show the recovery check confirming the
error rate returned to baseline, and the audit entry recording application and recovery
separately.

**Say, when recorded:** Firebreak cannot execute this. A separate service holds the only
credentials that can, it required a person, and it verified afterwards that the thing it
changed actually fixed the thing it was supposed to fix.

**Unblocked by:** credentials plus `make live`.

### Scene 8: Proof

**Runnable now:** no, and this is the important one to get right.

The spec asks for the Evaluation page showing B0, B1, FB and ablations with intervals, a
calibration curve, pass^3 and cost. **None of those figures exists.** Every eval report in the
repository is marked not quotable: no model has run, and neither held-out split has any
recordings.

What to record instead: the Evaluation page as it is, showing that every configuration reads
"not quotable as a result" with the reason beside it, and the Console's cost row reading "not
measured" rather than zero.

**Say:** this is what the evaluation harness looks like before it has been run, and the reason
each figure is missing is on the page. Do not narrate a number that is not there.

**Unblocked by:** the recorded library and credentials, then
`make eval CONFIG=fb-v1 SPLIT=test_id`.

---

## Closing

Thirty seconds, over the Report page.

**Say:** the claim is not that this finds root causes better than a person. It is that when it
says something, you can open the query behind it and run it yourself, and when it cannot say
anything it tells you which threshold it failed. The measured accuracy is not in this video
because it has not been measured, and the page that would show it says so.

---

## What not to do

- Do not cut the "mode: stub" line out of scene 1.
- Do not show a number from `reports/eval/` without its caveat. The two most quotable-looking
  figures in the repository are a fixture run at 1.000 and a partly recorded tuning split, and
  either would be a lie in a video.
- Do not stage scene 7 or 8 with mock data.
- Do not speed up the investigation. It finishes in under a second, which is the honest and
  slightly deflating truth about replaying a frozen bundle.
