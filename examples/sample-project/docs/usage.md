# Usage

## Options

| Option | Default | Description |
|---|---|---|
| `--jobs`, `-j` | 4 | Parallel jobs |
| `--timeout` | 30 | Seconds before a task is stopped |
| `--format` | csv | Export format |

## Hooks

Hooks run before each task.

```json
{
  "hooks": ["lint", "format"],
}
```
