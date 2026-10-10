# SwiftBill Simulation Results

Ran 13 scenarios, **13 passed**, 0 failed.

| Scenario | Result | Time (s) |
|---|---|---|
| S1: offline sale, then sync | PASS | 0.02 |
| S2: price conflict -> last-write-wins | PASS | 0.03 |
| S3: concurrent offline sales -> delta merge | PASS | 0.02 |
| S4: flaky link -> backoff + exactly-once | PASS | 0.45 |
| S5: billing preempts saturated background lane | PASS | 2.01 |
| S6: chatbot answers offline | PASS | 0.01 |
| S7: clock skew -> LWW still decides, conflict logged | PASS | 0.02 |
| S8: login, roles, sessions | PASS | 0.63 |
| S9: receipt printing (file + printer fallback) | PASS | 0.01 |
| S10: product catalog + barcode lookup | PASS | 0.01 |
| S11: tax calculation on checkout | PASS | 0.02 |
| S13: real connectivity auto-detect | PASS | 0.4 |
| S14: conflict review -> mark as reviewed | PASS | 0.04 |

## Details

### S1: offline sale, then sync - PASS

sale total=₹20.00, local stock 20->18 while offline, sync pushed 1 txn, central stock=18, central txns=1

### S2: price conflict -> last-write-wins - PASS

A set ₹11 (older), B set ₹12 (newer); central price=₹12.00; conflicts logged on B: 1 (resolution: last-write-wins (incoming newer))

### S3: concurrent offline sales -> delta merge - PASS

A sold 2, B sold 3 while offline; central stock 100->95 (commutative delta merge, no conflict); both terminals converged to 95

### S4: flaky link -> backoff + exactly-once - PASS

3 mid-push drops -> 3 backoff retries then success; central has exactly 1 txn (no dupes); duplicate-push test deduped 1 txn(s), central total=2

### S5: billing preempts saturated background lane - PASS

background lane blocked ~2s; checkout completed in 0.001s via dedicated billing worker; wait stats: {'count': 1, 'p50': 0.0001, 'max': 0.0001}

### S6: chatbot answers offline - PASS

offline answers OK: sales='Today's sales on T-E: ₹20.00 (local data).', stock='Widget (W6): 18 in stock @ ₹10.00.', sync='Sync status [OFFLINE]: 2 pending (txns=1, deltas=1, updates=0), last sync: never.'; bot is offline-intent only, no network

### S7: clock skew -> LWW still decides, conflict logged - PASS

T-B clock +5000s: its ₹30 write carried the newer timestamp and won LWW (central=₹30.00); conflict logged for manual review -> 'last-write-wins (incoming newer)' (known limitation)

### S8: login, roles, sessions - PASS

manager/cashier logins OK, bad password + unknown user rejected, session create/validate/logout OK, duplicate username rejected

### S9: receipt printing (file + printer fallback) - PASS

42-col receipt OK (total ₹45.50); file backend wrote /tmp/offlinepos_print_k9eut8c8/rcpts/receipt_abc123de_COUNTER-1.txt; unreachable network printer fell back to file, no exception

### S10: product catalog + barcode lookup - PASS

6 products loaded from catalog/products.csv, all EAN-13 valid and unique; scan of 8901011000015 -> Widget; unknown -> None; central carries the barcode too

### S11: tax calculation on checkout - PASS

2x Widget@18% + 1x Bolt@5% + 1x tax-free: subtotal ₹25.00, tax ₹3.70, total ₹28.70; breakdown persisted, printed, and synced; pre-tax DBs migrate cleanly with 0% default

### S13: real connectivity auto-detect - PASS

probe True vs local server / False vs dead port; auto-detect thread drove OFFLINE->ONLINE->OFFLINE on the shared state machine

### S14: conflict review -> mark as reviewed - PASS

1 conflict(s) logged; marked 5622e12a as reviewed; unreviewed=0

