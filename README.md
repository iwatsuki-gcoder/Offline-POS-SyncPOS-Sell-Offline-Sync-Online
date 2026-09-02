# Offline-POS-SyncPOS-Sell-Offline-Sync-Online

**OfflinePOS: A Priority-Scheduled, Conflict-Aware Retail Sync System**

OfflinePOS is a point-of-sale application that keeps working even when the internet doesn't. It runs entirely offline using local SQLite storage, then automatically syncs to a central MySQL/MS SQL Server database the moment connectivity returns. It also includes a hybrid AI chatbot assistant that follows the same offline-first philosophy as the rest of the system.

**Core OS & DBMS concepts explored:**
- CPU scheduling & thread management — priority scheduler for sync tasks and background jobs
- Mutex/locking — concurrency control across local and remote writes
- ACID transactions — reliable local writes even during power loss or crashes
- Replication & conflict resolution — delta sync with automatic conflict detection
- Idempotency & retry logic — safe retries with exponential backoff during flaky connectivity

**AI Chatbot Assistant (hybrid):**
- Offline mode — lightweight intent-matching over local SQLite (sales lookups, stock checks, sync status), no model dependency, works with zero connectivity
- Online mode — calls an LLM API for open-ended questions, natural language summaries, and richer troubleshooting
- Plugs into the same online/offline state machine used by the sync engine — no separate infrastructure needed
- General-purpose: handles both data queries ("What sold best today?") and sync troubleshooting ("Why didn't yesterday's transactions sync?")

┌─────────────────────────────────────────┐
│              POS Client (UI)             │
│         [Sales | Inventory | Chat]       │
└──────────────┬───────────────┬───────────┘
               │               │
     ┌─────────▼───────┐  ┌────▼──────────┐
     │  Local SQLite    │  │  Chat Handler │
     │  (source of      │◄─┤  (checks conn │
     │   truth offline) │  │   state first)│
     └─────────┬────────┘  └────┬──────────┘
               │                │
     ┌─────────▼────────┐   ┌───▼─────────────┐
     │ Priority Sync     │   │ Offline: local  │
     │ Scheduler         │   │ query engine    │
     │ (threads, locks,  │   │ Online: LLM API │
     │  retry/backoff)   │   │ call            │
     └─────────┬────────┘   └─────────────────┘
               │
     ┌─────────▼────────────┐
     │ Central DB (sync +   │
     │ conflict resolution) │
     └───────────────────────┘

**Other features:** audit logging, encryption at rest, chaos testing for sync failures, and a health dashboard for monitoring sync status.
___________________________________________________________________
TEAM ID_T178
ANAS ANSARI(TEAM LEAD)
UJJWAL UNIYAL
ADRSH YADAV
____________________________________________________________________
