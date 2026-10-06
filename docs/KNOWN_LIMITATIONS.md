# Known Limitations — OfflinePOS

Stated plainly, so the project reads as mature rather than over-claimed.

1. **No true multi-region replication.** There is one logical central
   database. Cross-region active-active sync (and its latency/cost
   trade-offs) is out of scope.
2. **No payment-gateway integration.** Checkout records the sale and prints
   a receipt; card processing / UPI / PCI-DSS handling is not implemented.
3. **Clock skew is detected, not prevented.** LWW ordering trusts terminal
   clocks. A skewed clock wins the timestamp comparison (demonstrated in
   S7); the conflict is logged for manual review, but there is no NTP
   enforcement or vector-clock upgrade yet.
4. **Master-data conflicts need a human.** LWW auto-resolves, but the loser
   is only *logged*. There is no merge UI for a manager to pick the winner.
5. **Pull is last-sync-wins for state.** Terminals converge to central
   state after pushing. A terminal that stays offline indefinitely will
   diverge until it syncs; there is no peer-to-peer gossip between
   terminals.
6. **Online chatbot LLM needs a key.** The online path is a real
   OpenAI-compatible call (ADR-002, `offlinepos/llm.py`), but without
   `OFFLINEPOS_LLM_API_KEY` set the bot stays on offline intents. No key
   is bundled with the repo.
7. **Security is baseline.** Audit logging and login with cashier/manager
   roles exist; encryption-at-rest is not implemented in this build.
8. **Single-writer assumption per terminal DB.** Concurrent checkouts on
   *one* terminal serialise through the billing worker; true multi-cashier
   contention on shared hardware is not modelled.
9. **Tax rates are catalog-seeded, not synced.** `tax_rate` is master data
   from `catalog/products.csv`; it is not part of the LWW
   `product_updates` sync. Changing a rate means re-seeding. Tax is
   exclusive and rounded per line with binary floats (`round(x, 2)`) —
   fine for this scale, but a production GST system would use decimal
   arithmetic and per-jurisdiction CGST/SGST splitting.
