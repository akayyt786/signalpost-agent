To: soham@builderr.ai
Subject: Signalpost — official-run budget, time limit, and previous-run state (Arman Katia)

Hi Soham,

I'm building a solo Signalpost entry (Arman Katia, akaykatia9@gmail.com; repo:
https://github.com/akayyt786/signalpost-agent) and want to size my request/time budget and refresh
logic against the real official run rather than guess. Three questions:

1. What are the exact request-count, wall-clock, and (if any) third-party-cost ceilings for an
   official 1,000-company (or 1,100) run? This isn't hypothetical sizing on my end: a real
   1,000-company rehearsal on my final commit used 5,900-6,050 requests depending on run-to-run
   variance — right at my current --max-requests 6000 default, occasionally over it. My agent
   degrades gracefully under a tighter cap (drops the weakest website candidate tier first, then
   site-subpage crawl depth, then job-detail re-fetches, then the cheapest-to-drop external
   connectors, then the whole website layer as a last resort — never drops an input row, confirmed
   by the same rehearsal: 1,000/1,000 envelopes emitted even when the budget ran out mid-batch), so
   getting this number right doesn't change whether the agent completes, only how much it finds. I'd
   rather calibrate against the real limit than guess and lose avoidable coverage.

2. How is previous-run state supplied between scheduled daily batches — is my agent expected to
   persist its own state directory across runs inside a long-lived container/volume, or does each
   official run start cold and rely on something the harness supplies (e.g. the prior run's
   envelopes.jsonl)? My refresh/diff logic (SQLite-backed) behaves correctly either way — a cold
   start reports changes: [] with refresh.baseline: "cold_start" rather than treating an empty
   state as every claim disappearing — but I want to confirm which mode the official harness
   actually runs so I'm not solving a problem it doesn't have.

3. Is the model key for optional LLM synthesis (mentioned in the agent playbook) something I should
   request now, or only once I want to enable it? My agent ships with a $0 declared cost by default
   (deterministic synthesis only) and only spends against SIGNALPOST_LLM_API_KEY if I explicitly
   provide one.

Thanks,
Arman
