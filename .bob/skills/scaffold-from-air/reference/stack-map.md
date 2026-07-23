# Stack map — from the AIR's `kind` to technology and template

Prototype default. This is not a production standard: it is what comes up fast,
runs offline, and lets you falsify a hypothesis. Adjust it to your client's
standard before using it on a real engagement.

| AIR `kind` | Prototype default | Common alternatives | Container |
|---|---|---|---|
| `service` | FastAPI (Python 3.11) | Quarkus, Spring Boot, Express | `python:3.11-slim` |
| `gateway` | FastAPI with routing | Kong, APIC, Nginx | `python:3.11-slim` |
| `database` | PostgreSQL 16 | Db2, MongoDB, MySQL | `postgres:16-alpine` |
| `cache` | Redis 7 | Memcached, Hazelcast | `redis:7-alpine` |
| `queue`/`topic` | Kafka (KRaft, no ZK) | RabbitMQ, IBM MQ | `confluentinc/cp-kafka` |
| `storage` | MinIO | COS, S3 | `minio/minio` |
| `job` | script + compose cron | Airflow, Quartz | `python:3.11-slim` |
| `ui` | HTTP client (`httpx`) | — | — |
| `external` | **WireMock/FastAPI stub** | — | never a real call |
| `actor` | pytest exercising the flow | k6, locust | — |

## Selection rules

1. **The stack comes from the AIR, not from here.** If `experiment_plan.stack` is
   filled in, it wins. This table is the default for when the AIR says nothing.
2. **Category ≠ product.** A "queue" in the drawing is not Kafka: it is
   `kind: queue`. The product belongs in `assumptions[]` and the human confirms it.
3. **Counter-indication for Kafka in a prototype:** if the hypothesis is not about
   ordering, retention, or reprocessing, RabbitMQ comes up in a third of the time
   and validates the same thing. Kafka in a laptop compose burns memory and time
   that should have gone to the hypothesis.
4. **`external` is never real.** Calling a third-party API from the prototype
   introduces flakiness, cost, and a network dependency into a measurement that
   should be clean.

## IBM context (when the client requires it)

| Category | IBM product | Note |
|---|---|---|
| queue | IBM MQ | image `icr.io/ibm-messaging/mq`; accepts the license via env |
| database | Db2 | heavy container; in a prototype prefer Postgres unless the hypothesis is about Db2 itself |
| gateway | API Connect | hard to containerize; use FastAPI and record the gap |
| integration | App Connect Enterprise | there is a dedicated Bob mode; out of scope for arch2code |
| Java runtime | Quarkus / Open Liberty | Quarkus comes up much faster in the loop |

When you swap the default for the IBM product, record it in `assumptions[]` with
an `impact` — trading Postgres for Db2 changes startup time, DDL, and what the
hypothesis can actually measure inside the experiment window.
