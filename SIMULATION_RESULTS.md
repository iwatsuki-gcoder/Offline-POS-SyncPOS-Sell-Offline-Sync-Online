# OfflinePOS Simulation Results

Ran 7 scenarios, **7 passed**, 0 failed.

| Scenario | Result | Time (s) |
|---|---|---|
| S1: offline sale, then sync | PASS | 0.01 |
| S2: price conflict -> last-write-wins | PASS | 0.02 |
| S3: concurrent offline sales -> delta merge | PASS | 0.01 |
| S4: flaky link -> backoff + exactly-once | PASS | 0.45 |
| S5: billing preempts saturated background lane | PASS | 2.0 |
| S6: chatbot answers offline | PASS | 0.01 |
| S7: clock skew -> LWW still decides, conflict logged | PASS | 0.01 |

## Details

### S1: offline sale, then sync - PASS

sale total=$20.00, local stock 20->18 while offline, sync pushed 1 txn, central stock=18, central txns=1

### S2: price conflict -> last-write-wins - PASS

A set $11 (older), B set $12 (newer); central price=$12.00; conflicts logged on B: 1 (resolution: last-write-wins (incoming newer))

### S3: concurrent offline sales -> delta merge - PASS

A sold 2, B sold 3 while offline; central stock 100->95 (commutative delta merge, no conflict); both terminals converged to 95

### S4: flaky link -> backoff + exactly-once - PASS

3 mid-push drops -> 3 backoff retries then success; central has exactly 1 txn (no dupes); duplicate-push test deduped 1 txn(s), central total=2

### S5: billing preempts saturated background lane - PASS

background lane blocked ~2s; checkout completed in 0.001s via dedicated billing worker; wait stats: {'count': 1, 'p50': 0.0001, 'max': 0.0001}

### S6: chatbot answers offline - PASS

offline answers OK: sales='Today's sales on T-E: $20.00 (offline data).', stock='Widget (W6): 18 in stock @ $10.00.', sync='Sync status [OFFLINE]: 2 pending (txns=1, deltas=1, updates=0), last sync: never.'; mode followed net OFFLINE->ONLINE

### S7: clock skew -> LWW still decides, conflict logged - PASS

T-B clock +5000s: its $30 write carried the newer timestamp and won LWW (central=$30.00); conflict logged for manual review -> 'last-write-wins (incoming newer)' (known limitation)

