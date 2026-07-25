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

## v2 — Agent Fabric kinds

14 ה-`kind`ים החדשים. המפרט המלא (סמנטיקה, שדות תוצאה, מדיניות):
[`docs/v2_spec.md`](v2_spec.md). כולם עוברים דרך אותו policy engine כמו v1.

### `shell_stream` — הרצה עם פלט חי
```json
{
  "id": "…",
  "kind": "shell_stream",
  "payload": {
    "command": "npm run build",
    "cwd": "D:\\CLAUDE\\shiri",
    "timeout_sec": 600,
    "flush_interval_ms": 500,
    "max_chunk_bytes": 16000
  }
}
```
התוצאה מגיעה כ-`results/<id>-chunk-<n>.json` — כל אחד עם `seq`, `is_final`,
`stream` (`stdout`/`stderr`) ו-`text`. הצ'אנק האחרון נושא `is_final: true`,
`ok`, `exit_code` ו-`stats`.

### `shell_session_open` — פתיחת session חי
```json
{
  "id": "…",
  "kind": "shell_session_open",
  "payload": {
    "command": "python -i -u",
    "cwd": "D:\\CLAUDE",
    "idle_timeout_sec": 900
  }
}
```
מחזיר `metadata.session_id` (למשל `s-4f3c1a9b2e77`) ואת ה-`pid`.
אם `command` חסר — נפתח ה-shell הדיפולטי של המערכת.

### `shell_session_write` — שליחת input ל-session
```json
{
  "id": "…",
  "kind": "shell_session_write",
  "payload": {
    "session_id": "s-4f3c1a9b2e77",
    "input": "print(2 + 2)",
    "read_timeout_sec": 2.0
  }
}
```
ה-state נשמר בין קריאות — משתנה שהוגדר בקריאה אחת קיים בבאה.
`\n` נוסף אוטומטית. `input` ריק = רק לרוקן פלט שהצטבר.

### `shell_session_close` — סגירת session
```json
{
  "id": "…",
  "kind": "shell_session_close",
  "payload": { "session_id": "s-4f3c1a9b2e77" }
}
```

### `grep` — חיפוש regex רקורסיבי
```json
{
  "id": "…",
  "kind": "grep",
  "payload": {
    "path": "D:\\CLAUDE\\shiri",
    "pattern": "def\\s+handle_\\w+",
    "include": "*.py",
    "ignore_case": false,
    "context": 2,
    "max_results": 500
  }
}
```
מחזיר `metadata.matches` — `[{path, line_no, line, before, after}]`.
קבצים בינאריים מדולגים; `node_modules`, `.git`, `__pycache__` נגזמים.

### `glob` — התאמת נתיבים
```json
{
  "id": "…",
  "kind": "glob",
  "payload": {
    "path": "D:\\CLAUDE",
    "pattern": "**/*.test.ts",
    "recursive": true,
    "files_only": true,
    "max_results": 1000
  }
}
```
`metadata.paths` ממוין לפי mtime — החדש ביותר ראשון.

### `patch_file` — עריכה כירורגית (דורש confirm)
```json
{
  "id": "…",
  "kind": "patch_file",
  "payload": {
    "path": "D:\\CLAUDE\\shiri\\config.py",
    "edits": [
      { "old_string": "TIMEOUT = 30", "new_string": "TIMEOUT = 120" },
      { "old_string": "  log(", "new_string": "  logger.info(", "replace_all": true }
    ],
    "backup": true,
    "dry_run": false
  }
}
```
עריכה בודדת אפשר גם שטוחה: `old_string` / `new_string` / `replace_all` ישירות
ב-`payload`.

כללים: `old_string` שלא נמצא → כישלון. נמצא יותר מפעם אחת בלי
`replace_all: true` → כישלון עם `metadata.occurrences`. **אם עריכה אחת
נכשלת — אף עריכה לא נכתבת** (אטומי). `backup: true` (ברירת מחדל) שומר
`<path>.bak-<timestamp>`. `dry_run: true` מחזיר diff בלי לגעת בדיסק.

### `watch_start` — התחלת מעקב אחרי תיקייה
```json
{
  "id": "…",
  "kind": "watch_start",
  "payload": {
    "path": "D:\\CLAUDE\\shiri",
    "recursive": true,
    "patterns": ["*.py", "*.ts"],
    "max_buffer": 1000
  }
}
```
מחזיר `metadata.watch_id` ו-`backend` (`watchdog` או `polling`).

### `watch_poll` — שליפת אירועים שהצטברו
```json
{
  "id": "…",
  "kind": "watch_poll",
  "payload": { "watch_id": "w-91ab3d0c5f12", "max_events": 200 }
}
```
מחזיר `metadata.events` — `[{ts, type, path}]` כאשר `type` הוא
`created` / `modified` / `deleted` / `moved`, ואת `dropped` (אירועים שנפלו
בגלל buffer מלא).

### `watch_stop` — עצירת מעקב
```json
{
  "id": "…",
  "kind": "watch_stop",
  "payload": { "watch_id": "w-91ab3d0c5f12" }
}
```

### `process_list` — רשימת תהליכים
```json
{
  "id": "…",
  "kind": "process_list",
  "payload": { "filter": "node", "sort_by": "memory", "limit": 50 }
}
```
`sort_by`: `memory` (ברירת מחדל) / `cpu` / `pid`.
מחזיר `pid`, `name`, `cmdline`, `cpu_percent`, `memory_mb`, `status`,
`username`, `created`.

### `process_kill` — הריגת תהליך (דורש confirm)
```json
{
  "id": "…",
  "kind": "process_kill",
  "payload": { "pid": 8123, "force": false, "timeout_sec": 5 }
}
```
SIGTERM ואז המתנה; אם התהליך שרד (או `force: true`) — SIGKILL.
PID 0/1 והתהליך של ה-harness עצמו מסורבים תמיד.

### `http_local` — בקשת HTTP מהמחשב שלך
```json
{
  "id": "…",
  "kind": "http_local",
  "payload": {
    "url": "http://127.0.0.1:8080/api/health",
    "method": "GET",
    "headers": { "Accept": "application/json" },
    "body": null,
    "timeout_sec": 30,
    "max_bytes": 1000000
  }
}
```
מותר רק loopback / private / link-local. כל כתובת ציבורית נדחית **לפני**
שנפתח socket, עם `error: "host not local: <ip>"`.
loopback → `auto`; LAN → `confirm`.

### `read_file_chunked` — קריאה מדפדפת
```json
{
  "id": "…",
  "kind": "read_file_chunked",
  "payload": {
    "path": "D:\\CLAUDE\\logs\\train.log",
    "offset": 0,
    "limit_bytes": 65536,
    "encoding": "utf-8"
  }
}
```
מחזיר `content`, `bytes_read`, `total_size`, `next_offset` ו-`eof`.
ה-`offset` הוא **בבייטים**, כך שהדפדוף מדויק גם בטקסט רב-בייטי (עברית),
והפענוח משתמש ב-`errors="replace"` כדי שגבול צ'אנק באמצע תו לא יזרוק.

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

---

## v2 — מבנה תשובה בצ'אנקים (`results/{id}-chunk-{n}.json`)

`shell_stream` (וכל kind שנשלח דרך `ReporterV2`) כותב קובץ לכל צ'אנק:

```json
{
  "task_id": "20260725-004-build",
  "seq": 7,
  "is_final": false,
  "stream": "stdout",
  "text": "webpack 5.90.0 compiled\n"
}
```

הצ'אנק האחרון:

```json
{
  "task_id": "20260725-004-build",
  "seq": 42,
  "is_final": true,
  "ok": true,
  "decision": "auto",
  "exit_code": 0,
  "stats": { "chunks": 42, "stdout_bytes": 91233, "stderr_bytes": 0 }
}
```

חוזה לצרכן:

1. למיין לפי `seq`, לא לפי זמן הקובץ — צ'אנקים מגיעים בכמה push-ים.
2. `is_final: true` מסמן סוף. בדיוק צ'אנק אחד לכל task נושא אותו, והוא זה
   שנושא `ok` / `decision` / `exit_code`.
3. kind לא-סטרימי מייצר צ'אנק אחד בלבד — `seq: 0, is_final: true`.
4. task שלא הגיע ל-`is_final` בתוך ה-timeout שלו נחשב כישלון.

כשה-task הנכנס היה עטוף ב-AMP envelope, כל צ'אנק הוא envelope מלא של
`harness_result` עם ה-body למעלה תחת `payload.body` — בדיוק כמו ב-v1.

---

## v3 — מטא-דאטה של תזמון

ארבעה שדות אופציונליים ב-`payload` (או מקובצים תחת `payload.schedule`) קובעים
*מתי* ו*באיזה סדר* משימה תרוץ. packet שלא נושא אף אחד מהם מתנהג בדיוק כמו ב-v2:
FIFO בתוך הבנד `normal`.

```json
{
  "id": "20260726-010-nightly-build",
  "kind": "shell_stream",
  "payload": {
    "command": "npm run build",
    "priority": "high",
    "not_before": "2026-07-27T02:00:00Z",
    "deadline": "2026-07-27T06:00:00Z",
    "depends_on": ["20260726-009-npm-install"],
    "retry": { "max_attempts": 3, "backoff_seconds": [1, 5, 15] }
  }
}
```

| שדה | ברירת מחדל | משמעות |
|---|---|---|
| `priority` | `normal` | `high` \| `normal` \| `low`. גבוה קודם, FIFO בתוך בנד. `urgent`/`critical`/`p0` → `high`, `background`/`batch`/`p3` → `low`. |
| `not_before` | — | ISO-8601. המשימה לא נראית לסדרן עד המועד, אבל לא חוסמת אחרות. |
| `deadline` | — | ISO-8601. עבר — המשימה נזרקת ומדווחת `status: "deadline_exceeded"`. `high` רץ בכל מקרה: מאוחר עדיף על never. |
| `depends_on` | `[]` | רשימת task ids שחייבים להסתיים ב-`ok`. נבדק גם מול `results/` בדיסק, כך שתלות שהתקיימה במחזור polling קודם נחשבת. |
| `retry` | `max_attempts: 1` | ניסיון חוזר על כשל. **verdict של policy לא נחשב flaky** — `block`/`confirm_required`/`cancelled` לא מנוסים שוב. |

חותמת זמן נאיבית (בלי אזור זמן) נקראת כ-UTC, לא כשעה מקומית: המפיק הוא brick
בענן וה-harness עשוי לשבת בכל אזור זמן. חותמת לא-פרסבילית נרשמת ל-log ומתעלמים
ממנה — `deadline` שגוי לא תוקע את התור.

`backoff_seconds` קצר מ-`max_attempts` חוזר על האיבר האחרון: `[1, 5]` עם 4
ניסיונות ממתין 1s, 5s, 5s.

### `results/{id}-attempt-{n}.json`

כל ניסיון של משימה עם retry נכתב לקובץ נפרד. **הקבצים האלה אינם צ'אנקים** —
הם יושבים מחוץ לזרם `seq`/`is_final`, כך שצרכן שעוקב אחרי חוזה הצ'אנקים לעולם
לא רואה שני `is_final` למשימה אחת. הם דיאגנוסטיקה; התוצאה הקובעת היא הצ'אנק
הסופי של הניסיון האחרון.

### `cancel` — ביטול משימה

```json
{
  "id": "20260726-011-stop-the-build",
  "kind": "cancel",
  "payload": { "task_id": "20260726-010-nightly-build" }
}
```

`cancel` עוקף את התור — להעמיד ביטול בתור מאחורי העבודה שהוא עוצר היה מחטיא
את הנקודה. שלוש התנהגויות, לפי מצב היעד:

* **בתור** — נשלף מהתור ומקבל צ'אנק סופי סינתטי עם `cancelled: true`,
  `status: "cancelled"` ו-`cancelled_by: <id של ה-cancel>`. המפיק לא נשאר
  ממתין לתשובה שלא תבוא.
* **רץ** — התהליך מקבל SIGTERM ואחריו SIGKILL. ה-executor מדווח בעצמו את
  הצ'אנק הסופי שלו עם `cancelled: true`, ולכן ה-runner לא מסנתז שני.
* **לא מוכר** — כישלון גלוי: `ok: false` עם ה-id בשדה `error`. ביטול שנעלם
  בשקט הוא גרוע מביטול שנכשל.

ה-`cancel` עצמו מחזיר `metadata.target_state` — `queued` \| `running` \|
`unknown` — כך שהמפיק יודע מה בדיוק קרה.
