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
6. **Online chatbot LLM is stubbed.** The offline intent engine is real;
   the online LLM call is a documented stub (ADR-002) until a provider is
   chosen and keyed.
7. **Security is baseline.** Audit logging exists; encryption-at-rest and
   role-based access are designed but not implemented in this build.
8. **Single-writer assumption per terminal DB.** Concurrent checkouts on
   *one* terminal serialise through the billing worker; true multi-cashier
   contention on shared hardware is not modelled.
