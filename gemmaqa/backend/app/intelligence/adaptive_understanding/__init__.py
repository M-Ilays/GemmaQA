"""Adaptive Application Understanding Engine
(app.intelligence.adaptive_understanding).

Answers, continuously and from evidence alone:

  - What am I looking at?
  - Is this screen ready to reason about, or still assembling itself?
  - Is it still changing?
  - How much do I trust this observation?
  - Should I look again before planning?
  - Did the application contradict what I understood a moment ago?

**Framework independence is the point, not a side effect.** Nothing in this
package names or detects React, Vue, Angular, Svelte, htmx, or any other
rendering technology, and nothing infers meaning from a URL. Every signal is
either something a human tester could see on screen ("no control is
clickable yet", "the screen changed between two looks") or a published web
standard (`aria-busy`, `role="progressbar"`, HTTP status semantics). An
application that has served its shell but rendered nothing is observably the
same condition whether a slow SPA bootstrap, a blocked script, or an empty
server response caused it -- and GemmaQA does not need to know which in
order to notice it should look again.

**No fixed waits.** This engine never sleeps and never sets a timeout. It
judges evidence and returns a `decision`; the caller owns any re-observation.
The only unavoidable concessions to physics are stated openly: two samples
cannot be taken at the same instant, and the number of samples is bounded
(`MAX_OBSERVATION_PASSES`) so a permanently-animating screen cannot stall a
run -- at which point the engine returns `accept_degraded` and records the
low confidence honestly rather than pretending the screen was ready.

**Prior knowledge is a hypothesis.** Software under active development
changes underneath the tester. Rather than overwriting an earlier belief,
this engine raises a typed contradiction recording both what it understood
before and what it observes now, so a human can adjudicate.

Scope boundaries: this engine never navigates, clicks, waits, mutates
application state, or calls a language model. It reads observations the
perception layer already produced and returns a judgement.
"""

from app.intelligence.adaptive_understanding.adaptive_understanding_engine import (
    AdaptiveApplicationUnderstandingEngine,
)
from app.intelligence.adaptive_understanding.schemas import UnderstandingAssessment

__all__ = ["AdaptiveApplicationUnderstandingEngine", "UnderstandingAssessment"]
