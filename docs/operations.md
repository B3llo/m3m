# Operations guide

## Compatibility

Supported environments are macOS and Linux with Python 3.9 through 3.13. Windows is not supported because process management and inter-process locking currently depend on POSIX behavior.

## Backup and recovery

Back up `~/.agent-m3m/` for global memory and `<repo>/.agent-m3m/` for project memory. The durable files to preserve are Markdown memories, `CORE.md`, configuration, and handoffs. SQLite indexes under `.index/`, queue locks, and temporary session files are rebuildable.

Before restoring, stop active m3m queue workers for the affected store and keep a copy of the current directory. Restore the Markdown/config files, then run:

```bash
m3m clean
m3m stats
m3m doctor
```

`m3m clean` validates Markdown and rebuilds the disposable index. Archival and supersession are status changes, so `m3m restore <id>` can reactivate an archived item.

## Upgrade

From a trusted checkout, rerun `./install.sh`, `uv tool install --force .`, or `pipx install --force .`. Upgrades do not initialize stores or rewrite hooks. Refresh hooks explicitly when their documented format changes:

```bash
m3m hooks install --agents codex,claude
```

## Troubleshooting

Start with `m3m doctor`, then inspect `m3m diagnostics --limit 20` or `m3m audit --limit 20`. Use `m3m queue process --retry-failed` for explicit queue recovery. Provider verification is opt-in because it makes a live provider call:

```bash
m3m doctor --verify-provider
```

Never force-remove queue locks while a worker may still be active. For hook problems, validate the checked-in template and use selective uninstall instead of restoring an entire backup over unrelated settings.
