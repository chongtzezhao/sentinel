# Sentinel

Lightweight system monitoring with persistent, stateful Telegram alerts.

## Monitored metrics

- CPU utilization: critical at the configured percentage. CPU is treated as an
  early warning for heat/overheating, so it stays deliberately sensitive.
- Available RAM: warning and critical thresholds in MiB.
- Memory pressure: Linux PSI `some avg60`, the percentage of time tasks are
  stalled waiting on memory. This is the memory-pressure trigger; swap
  occupancy alone is diagnostic only.
- Available disk capacity for each configured path: warning and critical
  thresholds in GiB.
- Disk growth rate in MiB/s.
- Swap usage, load averages (1/5/15m), and top processes by CPU, memory, and
  cumulative I/O for status diagnostics.

The default LY910 policy warns below 10 GiB free disk and becomes critical
below 1 GiB. RAM warns below 1 GiB available and becomes critical below
50 MiB. Memory pressure warns at 10% stalled (PSI some avg60) and becomes
critical at 25%.

## Trigger damping

A condition must breach its threshold for 3 consecutive polls (30 seconds at
the default 10s interval) before it alerts. It clears only after 2 consecutive
polls past a 5% hysteresis bound (for example, CPU alerts at 90% and clears
below 85.5%). This removes single-sample spikes without hiding sustained
problems. Three back-to-back spikes can still alert by design; the CPU signal
is an early warning, not a verdict.

A recovery message reports the value that actually cleared the condition, not
the last breaching value.

## Notification lifecycle

New conditions notify immediately. Critical conditions repeat every 30
minutes for the first three successful deliveries and every six hours
afterward. Warnings repeat every six hours. Failed deliveries retry with
backoff and do not count as successful notifications. A recovery message is
sent when a condition clears.

Alert state is persisted in `/var/lib/sentinel/alert-state.json`, so restarts
do not reset notification history.

## Commands

```bash
uv run python -m sentinel status
uv run python -m sentinel test-notification
uv run python -m sentinel run
```

The systemd-context test helper is:

```bash
scripts/test_alert.sh
```

## Journald policy

`scripts/journald-sentinel.conf` caps persistent journal use at 1 GiB, keeps
5 GiB free, retains at most one month, and rate-limits log storms. The
installer places it in `/etc/systemd/journald.conf.d/`.
