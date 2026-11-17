"""Shape C — orchestrator plus read-only investigators. Student work.

1. Build a handoff with `lab8.handoff.make_handoff`. Allowed tools are read-only.
   Never put parent_secret, raw headers, or the full claim row into facts.
2. `lab8.runtime.run_readonly_investigators` runs those tools.
3. The orchestrator quotes from the investigator's order view and calls
   `durability.issue_refund`. Investigators have no write registry.
   Pass the full quote; the host subtracts earlier refunds on the same order.
   `lookup_refund_history` is a read-only tool you may hand to an investigator.
4. Trace the sanitized handoff, not the parent context.

An LLM summary of the parent context is not a sanitizer. Use the allowlist.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

from lab8 import durability
from lab8.db import connect
from lab8.handoff import make_handoff
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, tool_cost
from lab8.trace import Trace

SHAPE = "orchestrator"


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        budget = Budget()
        trace = Trace(make_run_id(SHAPE, claim_id, seed))

        def end(status: str, proposal: int = 0, reason: str | None = None) -> RunResult:
            trace.add("decision", {"status": status, "proposal_amount": proposal, "reason": reason})
            return finish(conn, shape=SHAPE, claim_id=claim_id, seed=seed,
                          status=status, proposal_amount=proposal, budget=budget,
                          trace=trace, error_reason=reason)

        budget.add_tool(tool_cost())
        claim = get_claim_view(conn, claim_id)
        if not claim["found"]:
            return end("failed", reason="unknown_claim")
        if not claim.get("order_id"):
            return end("needs_info", reason="missing_order_id")

        facts = {key: claim[key] for key in ("order_id",) if key in claim}
        handoff = make_handoff(
            request_id=trace.run_id,
            claim_id=claim_id,
            task="Inspect the claim's order and current compensation policy.",
            facts=facts,
            allowed_tools=["lookup_order", "lookup_policy", "lookup_refund_history"],
        )
        trace.add("handoff", handoff)
        investigation = run_readonly_investigators(conn, budget, handoff, claim["principal_id"])
        trace.add("investigation", investigation)
        order_view = investigation["tools"].get("lookup_order", {})
        if not order_view.get("visible"):
            return end("escalated", reason=order_view.get("reason", "order_unavailable"))

        budget.add_tool(tool_cost())
        quote = compute_compensation(conn, order_view["order"])
        proposal = int(quote["amount_vnd"])
        trace.add("quote", quote)
        if proposal <= 0:
            return end("denied", reason=quote["reason"])

        outcome = durability.issue_refund(conn, claim_id, proposal, crash_at=crash_at)
        trace.add("refund", {"ok": outcome["ok"], "reason": outcome["reason"], "conflict": outcome["conflict"]})
        if not outcome["ok"]:
            return end("failed" if outcome["conflict"] else "denied", proposal, outcome["reason"])
        return end("resolved", proposal)
    finally:
        conn.close()
