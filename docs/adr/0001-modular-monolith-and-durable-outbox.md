# ADR 0001: Modular monolith with PostgreSQL transactional outbox

- Status: Accepted
- Date: 2026-07-20

## Context

The platform needs asynchronous synchronization, events, retries, and future extensibility, but starts as a self-hosted deployment with modest operational capacity.

## Decision

Deploy one codebase as separate API and worker processes. Use PostgreSQL for normalized facts and a transactional outbox for durable events. APScheduler runs in the worker; Redis provides locks, rate-limit counters, and cache only.

## Consequences

This prevents lost "data written but event not sent" transitions and gives simple local/Coolify operations. It requires an outbox dispatcher, idempotent consumers, and monitoring for event lag. A broker can later be added behind the event-publisher interface without changing producers.
