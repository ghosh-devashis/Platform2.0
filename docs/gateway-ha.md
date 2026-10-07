# Gateway high availability (GW-10)

The LLM gateway is on the path of every model call, so it must not be a single point of failure, and a gateway failure
must never turn into agents quietly calling providers directly.

## Design

```
agents ──▶ model router ──▶ gateway instance A  ──▶ providers
                       └──▶ gateway instance B
```

- **At least two gateway instances** (Portkey, stateless) behind the router (or behind a load balancer, with the router
  pointing at it). Locally and in a small deployment, give the router several instances: `PORTKEY_URLS=http://gw-a:8787/v1,http://gw-b:8787/v1`.
  The router rotates between them, and an instance that fails a request is skipped for 10 seconds.
- **Health checks**: the router's `/health` reports mode, whether auth is on, and how many gateway instances it knows about.
  Put the router itself behind two or more replicas and a health-checked load balancer in production.
- **Failure behaviour**: if every instance is down, the router answers `502 gateway_unreachable`; the SDK maps that to
  `503 GatewayUnavailable` for the caller, and `/ping` reports `Unhealthy` while the gateway is unreachable. There is no
  fallback to a provider (agents don't hold provider keys, and rules ENT-001..003 forbid direct calls).
- **In-flight requests**: a request that was being served by an instance that dies fails once; the caller (or its orchestrator)
  retries and lands on a healthy instance. The router retries across instances for *connection* failures, not for requests
  that may already have reached a provider (to avoid duplicate side effects).
- **Provider outages** are the gateway's job: Portkey configs carry fallbacks and retries per logical model (`registry.json`).

## Acceptance (from the spec)

"Gateway instance loss causes no failed invocations beyond in-flight retries." Verified locally with the router's failover
tests (`harness/model-router/tests/test_gateway_controls.py`): first instance refusing connections, second serving, the
dead one skipped, and a clear error when all are down.

## Production notes

- Run instances in at least two availability zones; size for N-1 capacity.
- With Portkey enterprise (hybrid data plane) the gateway caches configs and keys locally and keeps serving if the control plane is unreachable.
- With the open-source gateway, restarts lose nothing: it holds no state. Keep the gateway version pinned and roll one instance at a time.
- Alert on: router `5xx` rate, `gateway_unreachable` count, fallback usage (`ent.router.*` span attributes), and p95 latency.
- Not covered here: the production deployment model (self-hosted, hosted or hybrid) is still an open decision in the spec.
