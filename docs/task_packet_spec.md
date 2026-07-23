# Task Packet — מבנה

זה המבנה שאני (Perplexity) כותב לריפו `iddo111/iddo-harness-bridge/tasks/`.

## דוגמה בסיסית

```json
{
  "id": "20260723-001-scan-dclaude",
  "kind": "shell",
  "priority": "normal",
  "payload": {
    "command": "dir D:\\CLAUDE /s /b > D:\\index.txt",
    "paths": ["D:\\CLAUDE"],
    "timeout_sec": 120
  }
}
```

## סוגי משימות (`kind`)

### `shell` — הרצת פקודה
```json
{
  "id": "…",
  "kind": "shell",
  "payload": {
    "command": "git status",
    "paths": ["D:\\shiri"],
    "cwd": "D:\\shiri",
    "timeout_sec": 60
  }
}
```

### `read_file` — קריאת קובץ
```json
{
  "id": "…",
  "kind": "read_file",
  "payload": {
    "path": "D:\\CLAUDE\\RAMCHAT\\RAM_SHARED_MEMORY.md"
  }
}
```

### `write_file` — כתיבת קובץ (דורש confirm)
```json
{
  "id": "…",
  "kind": "write_file",
  "payload": {
    "path": "D:\\shiri\\AGENTS.md",
    "content": "…"
  }
}
```

### `list_dir` — רשימת קבצים
```json
{
  "id": "…",
  "kind": "list_dir",
  "payload": {
    "path": "D:\\CLAUDE"
  }
}
```

---

## מבנה תשובה (`results/{id}.json`)

```json
{
  "task_id": "20260723-001-scan-dclaude",
  "ok": true,
  "decision": "auto",
  "stdout": "…",
  "stderr": "",
  "exit_code": 0,
  "metadata": {}
}
```

או במקרה של דחייה:

```json
{
  "task_id": "…",
  "ok": false,
  "decision": "block",
  "error": "blocked by pattern: rm -rf /"
}
```
