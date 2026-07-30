# Sentinel

Lightweight system monitoring with persistent, stateful Telegram alerts.

## Monitored metrics

- CPU utilization: critical at the configured percentage.
- Available RAM: warning and critical thresholds in MiB.
- Available disk capacity for each configured path: warning and critical
  thresholds in GiB.
- Disk growth rate in MiB/s.
- Top processes by CPU, memory, and cumulative I/O for status diagnostics.

The default LY910 policy warns below 10 GiB free disk and becomes critical
below 1 GiB. RAM warns below 1 GiB available and becomes critical below
50 MiB.

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
