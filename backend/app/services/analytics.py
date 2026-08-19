"""The designated fixed-lane computation module — deliberately empty.

The fixed tool lane was deleted on 2026-08-17: every analysis executes through
the template pool and the governed harness (`analysis_harness`), and
deterministic domain reads live in `intelligence`. The Operator's typed
conversational reads that survived that cut — totals, breakdowns, comparisons,
cash position, recurring patterns — were removed on 2026-08-18 for the same
reason: they answered analytical questions outside the governed executor, which
meant a second read path, a second set of renderings, and two ways for the same
question to be answered. `grounding_tools` now holds only the record listing the
semantic layer cannot express. If a future capability genuinely needs a fixed,
hand-written computation path outside the governed executor, it belongs in this
file — and its addition should be treated as an architecture decision, not a
convenience.
"""
