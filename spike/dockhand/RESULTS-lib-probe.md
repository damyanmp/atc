| atc feature | env | result | note |
|---|---|---|---|
| atc docker ls (environments) | - | PASS | hawser-loop/hawser-standard, local/socket |
| atc docker compose up (new stack, env vars, 2 networks) | hawser-loop | PASS | 2/2 running |
| atc docker ls (containers, labels, status) | hawser-loop | PASS | 5 containers; status 'Up Less than a second' |
| atc docker stacks | hawser-loop | PASS | 1 managed (internal), 0 inferred-only |
| atc docker compose up (existing stack: file + env) | hawser-loop | PASS | compose-only update kept env [{'name': 'GREETING', 'value': 'v1'}]; env update -> [{'name': 'GREETING', 'value': 'v2'}]; container sees GREETING=v2 |
| update_stack env-wipe guard | hawser-loop | PASS | env_vars=[] refused without force_clear_env |
| atc docker logs (-n, --since) | hawser-loop | PASS | tail=3 -> 3 lines; --since 5s -> 2 lines; e.g. '2026-11-21T15:30:07.199319744Z tick 1795275007' |
| atc docker start/stop/restart <container> | hawser-loop | PASS | stop/start/restart |
| atc docker start/stop <stack> | hawser-loop | PASS | stop -> exited, start -> running |
| atc docker images | hawser-loop | PASS | 20 images, 0 dangling |
| atc docker volumes | hawser-loop | PASS | 6 volumes |
| atc docker prune --no-volumes (dangling images) | hawser-loop | PASS | deleted 0, reclaimed 0 B |
| atc quick publish (tar into container) | hawser-loop | PASS | nested dir tar landed |
| atc blackbox (container outages) | hawser-loop | PASS | 224 events ['die', 'kill', 'start', 'stop']; 21 outages for worker |
| failed deploy surfaces as an error (HTTP 200 + success:false) | hawser-loop | PASS | raised: Dockhand stack create (atc-lib-probe-bad) failed: failed to parse /dat |
| atc docker compose down (delete stack) | hawser-loop | PASS | containers gone |
| atc docker compose up (new stack, env vars, 2 networks) | local | PASS | 2/2 running |
| atc docker ls (containers, labels, status) | local | PASS | 5 containers; status 'Up Less than a second' |
| atc docker stacks | local | PASS | 1 managed (internal), 0 inferred-only |
| atc docker compose up (existing stack: file + env) | local | PASS | compose-only update kept env [{'name': 'GREETING', 'value': 'v1'}]; env update -> [{'name': 'GREETING', 'value': 'v2'}]; container sees GREETING=v2 |
| update_stack env-wipe guard | local | PASS | env_vars=[] refused without force_clear_env |
| atc docker logs (-n, --since) | local | PASS | tail=3 -> 3 lines; --since 5s -> 1 lines; e.g. '2026-11-21T15:31:37.915971521Z tick 1795275097' |
| atc docker start/stop/restart <container> | local | PASS | stop/start/restart |
| atc docker start/stop <stack> | local | PASS | stop -> exited, start -> running |
| atc docker images | local | PASS | 20 images, 0 dangling |
| atc docker volumes | local | PASS | 6 volumes |
| atc docker prune --no-volumes (dangling images) | local | PASS | deleted 0, reclaimed 0 B |
| atc quick publish (tar into container) | local | PASS | nested dir tar landed |
| atc blackbox (container outages) | local | PASS | 224 events ['die', 'kill', 'start', 'stop']; 27 outages for worker |
| failed deploy surfaces as an error (HTTP 200 + success:false) | local | PASS | raised: Dockhand stack create (atc-lib-probe-bad) failed: failed to parse /app |
| atc docker compose down (delete stack) | local | PASS | containers gone |
