# HiddenBench test1: five case studies (QUALITATIVE, not confirmatory)

Three tasks the graph got right and its control got wrong, and two the other way round. Case files are in
`cases/`, made with `verify/case_file.py runs/test1 <task> <control>`. Quotes are from the raw transcripts in
`runs/test1/<condition>/<task>.json`. "Right" means a strict majority of the four final votes is correct.

## Graph wins

**t7 (graetz_et_al_1998), graph vs placebo.** Both conditions start 3 of 4 correct. In placebo, Person 3
drifts after Person 2 repeats claims nobody can check, so the final vote is 2–2 and has no majority. In graph,
every hidden fact gets posted (11 posts, 13 `share_fact` calls). The agents cite the board's numbered facts,
the vote holds at 3–1, and the group is right. **Mechanism: the board stops drift caused by unverifiable claims.**

**t25 (select_emergency_shelter), graph vs placebo.** The first votes split D, A, A, C. In graph, the hidden
facts that rule out Alpha (toxins), Charlie (no power) and Bravo (contamination) all reach the board. Agents
refer to them and finish 4/4 on Station Delta. Placebo's votes barely move. **Mechanism: the disqualifying facts
get surfaced.**

**t42 (safe_haven_decision), graph vs placebo.** Only 1 of 4 is correct at the start. Graph finishes 4/4 on
Eagle Ridge, and agents cite "the board's source-checked ember report". One agent falsely claims to have posted a
fact and Person 4 corrects it against the board. Placebo doesn't move. **Mechanism: facts are surfaced, and the
board corrects a false claim.**

## Graph losses

**t10 (emergency_supply_drop), graph vs nudge. Correct: Warehouse C.** Both conditions start 3 of 4 correct.
The decisive hidden fact (rising noxious gas at Warehouse B, held by Person 3) reaches the board early, but
Persons 1 and 4 keep wavering:
- Person 4 switches between B and C 7 times in rounds 8–15 (counted by hand from the transcript).
- Person 1 reads the stamp too literally in round 8: the "'source-checked' label only means each poster says
  the fact is in their briefing", and asks for a timestamped reading.
- 10 of the 21 `share_fact` calls post shared facts that every agent already had, and those all favour B.

The final vote is B, C, C, B: a tie, so no majority. Nudge holds its first 3–1 (B, C, C, C).
**Mechanism: the board surfaced the fact, but agents treated "it's in someone's briefing" as weaker than
"it's in mine". Repeated posts of shared facts then reinforced the wrong common-knowledge answer.** This is the
opposite of t7: here the board didn't stop drift.

**t40 (emergency_hospital_transfer), graph vs placebo. Correct: Meadow Valley General.** Both conditions start
R, R, R, M. In graph, every hidden fact reaches the board, including two that cut against each other:
- Person 1's fact: Meadow Valley's main road is flooded, and the detour adds 10 minutes.
- Person 4's fact: Riverbend is on backup power and diverting non-critical work.

The three agents on Riverbend accept both facts. They put Meadow Valley at about 40 minutes, "too tight" for the
45-minute limit, and read the power issue as harmless for emergency surgery ("only diverts non-critical work").
So they stay at 3–1 Riverbend. In placebo, Person 3 switches to Meadow Valley in round 1 ("the only confirmed
surgical capacity"). Later Persons 2–4 dismiss the detour because "Fact 5" is numbered differently in each
briefing, so they think it is about Lakeview. Placebo ends 3–1 right.
**Mechanism: the board made a fact that cuts against the right answer as credible as the facts that support
it. Placebo's correct answer came partly from confusion over fact numbering, not from better reasoning.**

## What the five cases suggest (hypotheses only, not tested)
1. The board helps most when the hidden facts *disqualify* the options the shared facts favour (t25, t42) or when
   the group is drifting on unverifiable claims (t7).
2. It doesn't help when a hidden fact cuts *against* the right answer (t40). It also doesn't help when agents
   discount facts they can't see in their own briefing (t10). The stamp proves who holds a fact, not whether it's
   true or how much it matters.
3. Agents re-post shared facts (t10: 10 of 21). A board limited to hidden-profile facts, or one that marks a fact
   as "everyone already has this", might cut common-knowledge reinforcement. That idea is untested; it would need
   a new pre-registration.
