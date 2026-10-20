# Portfolio: Distributed Chat and Reservation Systems

[English](PORTFOLIO.md) | [한국어](PORTFOLIO.ko.md) | [Project README](../README.md)

## 1. Overview

**Original development:** September to December 2024. **Reliability maintenance:** October 2026.

This project implements two networked applications in Python. Talk handles concurrent users over a newline-delimited TCP protocol. Hotel Wedding Planner uses an HTTP client, a local Hotel/Band API, and SQLite to find and reserve a matching pair of slots. The October 2026 work preserves those functions while adding bounded resource handling and recoverable failure paths.

Its portfolio contribution is practical evidence of protocol handling, concurrency, transactional state, and failure recovery. These are useful foundations for operating services that include AI components, while the project itself remains a networking and consistency project.

In everyday terms, Talk takes a typed message and delivers it to connected users; the planner takes a request for a hotel/band match and returns the booking outcome. A **bounded queue** stores only a limited number of waiting messages for each recipient. **Idempotency** lets a repeated reservation request reuse its original result, while **compensation** cancels only a booking created by an incomplete workflow. These mechanisms handle slow readers and uncertain requests while retaining the original user operations.

## 2. Results

**89 tests passed on Windows Python 3.10.21 and 3.12.7** after the close-path and receipt-confirmation repair. All 178 executions passed with no failures, errors or skips, both runs record the same current source hashes, and the initial five tests remain. The [CI repair verification](reports/ci-repair-verification.json) links the complete local records. For automated cross-platform runs, see [GitHub Actions](https://github.com/Dongmyung378/Distributed-System/actions/workflows/ci.yml).

The retained earlier source snapshot passed **85 tests in each of four environments**, totaling 340 executions without failures, errors or skips. Those verification reports and the following benchmarks remain historical records rather than measurements of the repaired source.

**Loopback** measurements run the server and clients on the same computer. One broadcast creates an expected delivery for each intended receiver. **p95** summarises the slower end of measured message delays: about 95% are at or below that value. The median of trial-level p95 values summarises repeated trials, and must be read alongside completed deliveries to avoid treating an unfinished run as fast.

With one stalled connection and three healthy receivers, each version completed three identical trials: **3 of 900 planned delivery observations initially, versus 900 of 900 after strengthening in every trial**. The initial writer attempted only 48 of 300 broadcasts and submitted 47, so incomplete observations are not a message-loss rate. Under 60 healthy connections the median trial-level p95 rose from **1.6338ms to 3.2020ms**. Resource measurements include the server and client harness together.

The earlier four-environment record used **Windows Python 3.10.22 / 3.12.7 and WSL Ubuntu Python 3.10.22 / 3.12.3**, with separate virtual environments installed from the dependency lock. Linux used a separate source copy; normalized source hashes match within those four runs. WSL shares the same physical host. The local repair records cover two Windows environments, while performance benchmarks retain their original source snapshot.

| Evidence | What it establishes |
| --- | --- |
| Real-socket chat tests | Original commands, split/coalesced UTF-8 input, registration races, resource limits, stalled peers, reconnects, and shutdown |
| HTTP and database tests | Reservation isolation, conflict handling, replay-safe mutations, persisted receipts, and guarded cancellation |
| Recovery and CLI tests | Lost outcomes remain recoverable, replacement reservations are protected, and restart recovery uses a file journal |
| Common benchmark harness | Actual healthy-client receipts, missing and duplicate counts, and delay distributions under declared loopback conditions |

These records do not establish production capacity, internet performance, durable chat delivery, or atomic booking across services.

## 3. Problem

Working commands were not sufficient evidence that the project handled failures well. An unbounded TCP line or idle connection could consume resources, and a peer that stopped reading could hold up synchronous broadcasts. Registration also needed a clear ordering between its confirmation and messages from other users.

The booking problem is different: a client can lose a response after a reservation was already committed. Blind retries or unconditional compensation can then misclassify success or delete a newer booking of the same slot. Recovering a historical successful request also requires checking whether that booking is still current.

## 4. Architecture and decisions

Talk retains one receiving thread per connection. It adds a bounded FIFO queue and a sending thread per connection, with explicit connection, input, registration, and send limits. The trade-off is a higher but bounded thread count in exchange for slow-peer isolation. Registration confirmation is queued before the username becomes visible.

Normal rejection and quit now send the final response before half-closing the socket and discarding unread input within the existing close budget. Continuous input cannot hold the connection open indefinitely, and failed-peer cleanup precedes updating `send_failures`.

Hotel commits each reservation mutation and its operation receipt together in SQLite. A stable operation key makes a retry refer to the same action. A reservation generation guard limits compensation to the booking created by that workflow. The CLI records intent before a request and stores unresolved outcomes for later recovery.

Receipt confirmation uses read-only GET attempts with a limit of `max(2, client.retries)` inside one lookup's existing `max_elapsed` budget. It adds no mutation POST, preserves mutation retry limits, and still requires proof of the current Hotel/Band pair before reporting a match.

The interactive planner uses a persistent journal and a same-journal process lock. Direct function calls use an in-memory journal unless the caller explicitly passes a file-backed one. Automatic matching preserves unrelated manual reservations; cleanup remains the explicit menu 7 action.

See [the architecture document](ARCHITECTURE.md) for state transitions, lock boundaries, and API compatibility.

## 5. Experiments and reproduction

```bash
python -m pip install -r requirements-dev-lock.txt
python -B scripts/verify.py --output target/verification.json
python -B scripts/benchmark.py --project . --scenario all --trials 3 --label current-source --output target/benchmark.json
```

Tests allocate temporary ports and SQLite databases. They use controlled failures and real subprocesses rather than requiring external services or a running demo instance.

Healthy chat measurements use 2, 10, 30, and 60 receiving clients, five warmups, and 20 measured broadcasts per trial. The stalled-peer experiment adds one non-reading connection and constrains its socket buffers. Both source versions use the current harness, identical message contents, and the same buffer fixture. Only the improved version has the new configurable queue and send deadline.

The initial source is preserved at commit `1504ab3`. Exporting it to another directory and supplying that directory through `--project` avoids overwriting the current source. Compare actual deliveries and missing messages before interpreting p50 or p95 delay; percentiles alone can conceal non-delivery. Default repeated trials are sequential, not a claim of independent production hosts.

## 6. Failure analysis and limits

- An outgoing queue admission or completed socket write is not a recipient acknowledgement. Chat has no durable history or replay contract.
- Slow-peer isolation adds one sending thread per connection. Connection and queue limits make this cost explicit.
- Server-side operation receipts are retained without automatic expiration. Long-term retention and disk management remain operational decisions.
- File-backed journals support restart recovery. A default direct function call's in-memory journal does not survive exit.
- Recovery can remain unresolved when a service or its receipts cannot be verified. The planner retains that state rather than inventing a completed outcome.
- Hotel and Band current-generation checks are separate reads. Another actor can change a booking after either check or after a workflow has been marked matched.
- `max_elapsed` is a retry budget, not an absolute wall-clock guarantee for a request or the complete workflow.
- Demo credentials and the local HTTP server are intended for reproducible local execution. A public deployment needs a separate security and operations design.

## 7. Demo

For Talk, open one server and two clients. Register Alice and Bob, then show broadcast, direct messaging, listing, and a confirmed quit. The automated tests separately exercise disconnects and stalled peers so the demo does not rely on editing or replaying an old output.

For Hotel, start `Hotel/local_api.py` and `Hotel/booking.py`. On a fresh demo database, menu 6 selects the earliest available matching slot. Menus 1 through 5 retain the initial views and manual operations, and menu 7 performs requested cleanup. Restarting the CLI with the same file journal is the recovery entry point for an interrupted automatic attempt. Do not reset its API DB to simulate recovery: that destroys evidence needed to resolve the outstanding operation.

## 8. Source and evidence

- [Talk server](../Talk/myserver.py) and [client](../Talk/myclient.py)
- [Reservation API server](../Hotel/local_api.py) and [HTTP client](../Hotel/reservationapi.py)
- [Planner](../Hotel/booking.py) and [recovery journal](../Hotel/recovery.py)
- [Tests](../tests), [verification runner](../scripts/verify.py), and [benchmark harness](../scripts/benchmark.py)
- [CI repair verification](reports/ci-repair-verification.json), with complete Windows Python 3.10.21 and 3.12.7 run records
- [Earlier verification summary](reports/verification-summary.json), [initial measurement](reports/initial-benchmark.json), [improved measurement](reports/final-benchmark.json), and [comparison](reports/comparison.json)

The source history preserves the initial implementation. Public evidence contains technical measurements and source hashes, without local runtime databases, bearer credentials, private workflow records, chat contents, or daily maintenance notes.
