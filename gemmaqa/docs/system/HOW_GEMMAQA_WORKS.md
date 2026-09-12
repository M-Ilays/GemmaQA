# How GemmaQA Works

*A plain-language explanation for readers who want to understand the system without reading its source code. Technically accurate, but light on implementation detail — see `TECHNICAL_SYSTEM_DOCUMENTATION.md` for that.*

---

## 1. What GemmaQA does

GemmaQA looks at a web application the way a careful new QA engineer would on their first day: it opens the app in a real browser, clicks around, reads what's on the screen, and slowly builds an understanding of what the application actually *is* — what kinds of things it manages (customers, orders, tickets, whatever they turn out to be), who's allowed to do what, what processes exist, and how the numbers on the dashboard relate to the actions people take.

Once it has that understanding, it can (optionally, and only if you ask it to) go further: pick something specific worth checking — "does creating a job actually increase the open-jobs count?" — and carry out a safe, evidence-based investigation to confirm or refute it, reporting exactly what it found.

Two things GemmaQA deliberately does **not** do: it never invents facts about your application, and it never takes an unsafe or destructive action, no matter what a language model might suggest.

---

## 2. What happens after the user provides an application

You give GemmaQA one thing: an authorized URL (plus, optionally, login credentials and some safety settings). From there:

1. GemmaQA opens a real browser and loads the page.
2. It reads the page's structure — headings, forms, tables, buttons, navigation.
3. It figures out what to click next, guided by a deterministic priority system (and, optionally, a language model's advice on close calls).
4. Before anything is clicked or typed, a safety check runs. If the action is risky, prohibited, or out of scope, it's blocked — no exceptions.
5. If approved, the action actually happens in the browser.
6. GemmaQA looks at what changed, captures a screenshot, and updates its understanding.
7. It repeats this loop until it runs out of things worth exploring, hits a safety limit (like a maximum number of actions), or you cancel it.
8. At the end, it produces a report: what pages exist, what was tested, what bugs were found, and how much of the application was actually observed.

If you've turned on the optional "autonomous investigation" feature, one more thing can happen during that loop: instead of just exploring, GemmaQA can decide to specifically investigate something it has a hypothesis about — see Section 9.

---

## 3. Observation

Every time the browser lands on a page (the first page, or after clicking something), GemmaQA takes a structured snapshot of it: what's visible, what's clickable, what forms exist, what error messages are showing, what the browser's console and network traffic looked like. This turns a live, messy web page into clean, structured data GemmaQA can reason about — the same way a person would glance at a page and mentally note "okay, there's a login form here."

A key detail: GemmaQA computes a fingerprint of each page state that deliberately ignores things like timestamps and item counters. That way, revisiting a page whose only difference is "3 minutes ago" instead of "2 minutes ago" is correctly recognized as "the same page," not a brand-new one worth exploring again.

---

## 4. Application understanding

From that structured snapshot, GemmaQA infers business meaning — without ever being told in advance what kind of application it's looking at:

- **Entities** — the "things" the application manages. GemmaQA never assumes it's looking at "customers" or "invoices"; it notices repeated nouns in navigation, headings, and table columns, and only calls something a confirmed entity once it's seen from more than one independent angle.
- **Actors** — the roles or identities involved (an admin, a guest, a specific user type). GemmaQA distinguishes "I've genuinely logged in as this role and seen its dashboard" from "I merely saw this role's name mentioned somewhere" — the first earns a much higher confidence than the second.
- **Workflows** — the actual business processes: someone does something, an entity changes state, something else happens as a result. GemmaQA reconstructs these from watching a real action happen and comparing the page before and after.
- **Business dependencies** — the connections between what people do and the numbers/summaries the application displays. If there's a "5 open jobs" counter, GemmaQA tries to work out which workflow makes that number go up or down — and it only calls that connection *verified* once it has actually watched the number change in response to the right action.

None of this reasoning requires a language model. It's all deterministic pattern-matching over structured evidence, with a transparent confidence score attached to every conclusion.

---

## 5. Knowledge Graph

All of the above — entities, actors, workflows, dependencies — gets unified into one graph: nodes for each thing GemmaQA has identified, edges for how they relate. Think of it as GemmaQA's evolving mental map of the application.

Why a graph, specifically? Because the same fact ("this actor performs this workflow, which affects this entity, which drives this dashboard number") naturally involves several different discoveries connecting to each other. A graph lets GemmaQA ask questions like "what do we know about this workflow?" and get back everything connected to it — the actors, the entities, the dependent outputs — in one bounded, efficient lookup, rather than searching four separate lists by hand.

The graph also tracks its own version over time, and flags things like contradictions (two pieces of evidence that don't agree) and gaps (something GemmaQA suspects exists but hasn't confirmed) — honestly, rather than silently picking one answer.

---

## 6. Investigation goals

Once there's a graph, GemmaQA can ask: "given everything I currently know, what's the most valuable thing to check next?" That's a goal. A goal isn't a vague to-do item — it's a specific, evidence-backed hypothesis, like "verify that this workflow's expected outcome actually occurs" or "resolve this contradiction I noticed."

Goals are ranked by a transparent priority score (how much would this teach us, how business-critical does it seem, how risky would checking it be), and the ranking is fully explainable — you can always see *why* one goal outranks another.

---

## 7. Scenario planning

A goal says *what* to investigate; a scenario says *how* — as a safe, step-by-step plan, described in business terms, not browser terms. A scenario step reads like "observe the current count," "perform the workflow," "observe the count again," "compare" — never "click the button with CSS selector `#submit-btn-42`."

This is deliberate: by keeping scenarios purely semantic, they stay meaningful even though the exact page layout might change, and — just as importantly — a scenario can be fully planned, reviewed, and reasoned about *before* anything ever touches a real browser. Every scenario also gets a feasibility check (can this realistically be done right now?) and a risk assessment (is this read-only, or does it mutate real data — and if so, can it be cleaned up afterward?).

---

## 8. QA strategy

A given moment in a run might have dozens of feasible scenarios sitting around. The QA Strategy layer decides which ones matter most right now, groups them sensibly (so GemmaQA doesn't switch actors or bounce between unrelated parts of the app more than necessary), and sorts them into named buckets — some ready to run immediately, some that need to wait on something else first, some that are outright unsafe and should never run, and so on.

This is a planning/ranking step only — nothing gets executed here. It just produces an ordered, well-organized to-do list for the next layer.

---

## 9. Autonomous investigation

This is the one part of GemmaQA that can actually *act on its own initiative* rather than simply react to whatever the ordinary exploration loop happens to click next — and it is **off by default**. You have to explicitly turn it on for a given run.

When it's on, GemmaQA picks the next scenario from the strategy's to-do list, double-checks that its requirements still hold (an actor it needs might not have logged in yet, for instance — if so, it waits rather than forcing it), and confirms one more time that it isn't fundamentally unsafe to attempt.

Then, crucially: **it never issues a raw click or keystroke itself.** For every step of the scenario that requires touching the browser, it hands the *intent* — "locate the subject," "perform the operation" — to the exact same Runtime Planner that ordinary exploration already uses, just with a hint about what this particular step is trying to accomplish. The Planner then does what it always does: picks a concrete, safe action from the real page in front of it.

Steps that don't need a browser action at all — "verify this," "compare this" — are evaluated directly from what's already been observed, honestly, without inventing a result.

---

## 10. Runtime planning and execution

Whether the trigger was ordinary exploration or an autonomous investigation, every concrete action goes through the exact same four-stage pipeline:

1. **Semantic intent** — "click this," "fill this field," expressed as a structured action.
2. **Frontier** — the complete, deterministic list of everything currently clickable/fillable/navigable on the page, scored by a transparent priority model (and, optionally, refined among near-ties by a language model's advisory opinion — never allowed to introduce an option that wasn't already on the list).
3. **Runtime Planner** — picks the winning action.
4. **Safety Validator** — the one gate every single action passes through, with zero exceptions. Budget exhausted? Blocked. Out of the authorized domain? Blocked. Looks like a delete/payment/invite/bulk-action/etc.? Blocked. Only after this check passes does anything actually happen in the browser, via the **BrowserAdapter** and **ActionExecutor**.

---

## 11. Evidence and verification

When an investigation checks something specific — "did this assertion hold?" — GemmaQA reports one of three honest outcomes:

- **Supported** — the evidence backs it up.
- **Contradicted** — the evidence disagrees with it.
- **Inconclusive** — there simply wasn't a reliable enough signal to say either way.

That third option matters. GemmaQA is built to never claim "supported" just because nothing obviously went wrong — if it can't genuinely tell, it says so.

---

## 12. Learning loop

Nothing observed during an investigation is thrown away once the investigation ends. The same discovery engines that run during ordinary exploration also run for every action an investigation takes, so:

- **Registries** (entities/actors/workflows/dependencies) update from whatever was just observed.
- The **Knowledge Graph** re-synchronizes from those registries.
- **Confidence** and **coverage** are re-measured — how many open questions were resolved, how much more of the application's behavior is now understood.
- **Goals** are regenerated against the freshly updated graph — which is how a completed investigation can produce brand-new follow-up goals nobody explicitly asked for. (In one real test run, a single investigation on a demo application produced 44 new follow-up goals just from what it happened to learn along the way.)
- **Scenarios** and the **strategy** naturally reflect the new goal set the next time they're generated.

This is the "loop" in the architecture diagrams you'll see elsewhere in this documentation: observation feeds understanding, understanding feeds reasoning, reasoning feeds (optionally) action, and action feeds back into observation.

---

## 13. Example end-to-end investigation (hypothetical, application-neutral)

This example is illustrative only — no specific counter delta is claimed as a general guarantee; it's meant to show *how the pieces connect*, not to promise a specific numeric outcome for any real application.

Imagine an application has some kind of item that can be in **State A** (say, "open") or **State B** ("closed"), and a dashboard shows a count of items currently in State A.

1. **Discovery** notices the item type, notices an actor who can change its state, notices the workflow that performs the transition, and notices the dashboard count as a candidate output.
2. **Dependency discovery** forms a hypothesis: "this count probably reflects how many items are in State A" — but doesn't call it verified yet, since it hasn't watched the number actually move.
3. **Knowledge Graph** ties the entity, the actor, the workflow, and the output together as connected facts.
4. **Goal generation** notices the dependency is still unverified and proposes a goal: "verify this output's relationship to the item's state transition."
5. **Scenario planning** turns that into a concrete-but-semantic plan: observe the count, perform the workflow (move an item from State A to State B), observe the count again, compare.
6. **QA strategy** ranks this scenario alongside everything else currently worth doing, and — if it's safe, cheap, and informative — queues it for immediate attention.
7. **Autonomous investigation** (if enabled) picks it up: checks the actor is available, confirms the scenario isn't unsafe, and steps through it — the "perform the workflow" step goes through the Runtime Planner → Safety Validator → real browser action; the "observe" and "compare" steps read directly from what's already visible.
8. **Verification** reports **supported** if the count genuinely moved the expected direction, **contradicted** if it moved the wrong way or didn't move at all, or **inconclusive** if there wasn't a reliable enough signal to tell (for instance, if the "before" and "after" text on the page couldn't be compared cleanly).
9. **The loop closes**: the Knowledge Graph now has stronger evidence either way, confidence/coverage numbers reflect that, and a fresh goal-generation pass may surface something new worth checking next — for example, whether a *different* actor sees the same count update, or whether the reverse transition (State B back to State A) behaves symmetrically.

---

## 14. Safety and stopping

GemmaQA stops, or refuses to act, for very deliberate reasons:

- **Budgets** — a maximum number of actions, pages, screenshots, and total runtime are always enforced. Hit the limit, and the run ends cleanly rather than continuing indefinitely.
- **Scope** — every navigation and click target is checked against the one authorized domain you provided. Anything outside it (a third-party link, an unrelated site) is blocked, not followed.
- **Prohibited intent** — a fixed list of dangerous-sounding actions (deleting things, making payments, inviting users, bulk operations, and similar) is blocked outright, regardless of what a language model proposed or how confident it sounded.
- **Repeated failure** — if the same kind of action keeps failing, GemmaQA backs off rather than hammering the same broken path forever; the optional autonomous-investigation feature has its own, separate "stop after repeated failures" rule.
- **No more work left** — once every candidate action or investigation has been tried (or is legitimately blocked/deferred), GemmaQA reports that and stops, rather than looping pointlessly.
- **You cancel it** — a run can be cancelled at any time, and that request is always honored.

---

## 15. What GemmaQA currently cannot guarantee

Being direct about the honest limits of the current system:

- It **cannot** guarantee it will pick the exact intended UI element for a semantic step on every page — it nudges the same general-purpose action-selection system the rest of exploration uses, rather than surgically targeting one specific button.
- It **cannot** switch between two different logged-in actors within the same run to test a genuinely cross-role scenario — that would require a fresh run with different credentials today.
- It **cannot** verify a numeric or textual assertion with guaranteed precision — verification today is based on comparing visible before/after text and the outcome of the last action, not a dedicated structured data-extraction system, and it will honestly say "inconclusive" rather than guess.
- It **cannot** learn across separate runs — everything it figures out lives only in that run's memory; a brand-new run starts from zero, even against the same application.
- It **cannot** promise complete coverage of an application — its understanding is strictly built from what it has actually observed; unexplored areas remain unknown, not silently assumed to be fine.
- It **does not** ask a human to approve individual risky actions mid-run today — safety is enforced entirely by the deterministic rules described above, not by a pause-and-ask-a-person step.

None of these are hidden trade-offs — every one of them is also documented, in more technical terms, alongside the parts of the system responsible for them.
