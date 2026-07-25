# v3 Track A — Realtime & Performance

v2 נתן ל-harness יכולות (streaming, sessions, patch, grep...).
Track A מטפל בדבר היחיד ש-Desktop Commander עוד הוביל בו: **latency**, ובמה
שחסר סביבו — תזמון, ביטול, retry, מקביליות ומדידה.

הכל **backward compatible**: harness בלי `config.yaml`, בלי מטא-דאטה של תזמון
ובלי `ws.enabled` מתנהג בדיוק כמו v2.

---

## 1. `config.yaml` — קונפיג ריצה אחד

עד עכשיו הכיוונונים היו מפוזרים. עכשיו יש קובץ אחד בשורש הריפו
(או `~/.iddo-harness/config.yaml`), נטען דרך `agent/config.py`:

```yaml
poll_interval_seconds: 5
max_concurrent_tasks: 3
chunk_flush_ms: 500
chunk_max_bytes: 16384
ws:
  enabled: false
  host: 127.0.0.1
  port: 8477
retry:
  default_max_attempts: 1
metrics:
  enabled: true
```

* `load_runtime_config(path=None)` → `RuntimeConfig`. לכל שדה יש default, ולכן
  **קובץ חסר הוא לא שגיאה** — התקנת v2 שלא עברה מיגרציה מקבלת התנהגות v2.
  נתיב שהמשתמש ביקש במפורש ולא קיים — כן שגיאה.
* ולידציה נוקשה: `max_concurrent_tasks: 0` או `ws.port: banana` נופלים ב-startup
  עם `ConfigError` שמזכיר את שם המפתח, ולא מתגלים שעה אחר כך.
* **ENV override** מעל הקובץ: `IDDO_MAX_CONCURRENT_TASKS`, `IDDO_WS_ENABLED`,
  `IDDO_WS_PORT`, `IDDO_WS_TOKEN`, `IDDO_RETRY_MAX_ATTEMPTS`,
  `IDDO_METRICS_ENABLED`, `IDDO_POLL_INTERVAL_SECONDS`, `IDDO_CHUNK_FLUSH_MS`,
  `IDDO_CHUNK_MAX_BYTES`. סדר קדימות: **ENV > קובץ > default**.

`policy.yaml` ו-`load_config` לא נגעו — מדיניות נבדקת ונכתבת ביד, קונפיג ריצה
מכוונים בחופשיות. שני קבצים, שני loaders, בכוונה.

---

## 2. תור עדיפויות ותזמון — `agent/taskqueue.py`

> **הערה על שם הקובץ:** המפרט ביקש `agent/queue.py`. `agent/` יושב על
> `sys.path` גם בהרצה כסקריפט וגם כחבילה, כך ש-`queue.py` שם היה מסתיר את
> `queue` של הספרייה התקנית — ש-`executor_v2.py` משתמש בו (`queue.Queue`)
> לצינורות ה-streaming. לכן `taskqueue.py`.

ארבעה שדות תזמון, כולם נקראים מ-`payload` של ה-task (כלומר `payload.body`
של מעטפת AMP), אופציונלית תחת מפתח `schedule`. **`amp.py` לא נגע.**

| שדה | סמנטיקה |
|---|---|
| `priority` | `high` \| `normal` \| `low`. גבוה קודם, FIFO בתוך אותה רמה. `urgent`/`critical`/`p0` → `high`, `background`/`batch`/`p3` → `low` |
| `not_before` | ISO-8601. ה-task בלתי נראה עד אז |
| `deadline` | ISO-8601. עבר — ה-task נזרק עם warning ותוצאת `deadline_exceeded`. **`high` לא פג** — מאוחר עדיף על אף פעם למשהו דחוף |
| `depends_on` | רשימת task ids שחייבים להסתיים בהצלחה קודם |

* טיימסטמפ בלי timezone נקרא כ-UTC (לא local): ה-producer הוא brick בענן
  וה-harness יכול לשבת בכל אזור זמן.
* טיימסטמפ שלא נפרס → warning וההגבלה מתעלמת. `deadline` שבור לא יתקע את התור.
* תלויות נבדקות גם מול `results/` (`ResultIndex`) וגם מול השלמות באותו תהליך,
  כך שתלות שנסגרה במחזור polling קודם — או ע"י harness אחר שחולק את אותו
  bridge — נחשבת.
* `next_task()` מחזיר את ה-task הזכאי בעל העדיפות הגבוהה ביותר, או `None`.

---

## 3. ביטול — `kind: cancel`

```json
{"id": "cancel-1", "kind": "cancel", "payload": {"task_id": "build-42"}}
```

`cancel` **עוקף את התור** — להמתין בתור מאחורי העבודה שאתה מנסה לעצור זה
להחמיץ את הפואנטה.

| מצב היעד | מה קורה | `target_state` |
|---|---|---|
| עוד בתור | מוצא מהתור, נכתב לו chunk סופי `cancelled: true` | `queued` |
| רץ | `ExecutorV2.stop()` — SIGTERM, ואחרי `grace_sec` SIGKILL. ה-executor מזרים בעצמו chunk סופי עם `cancelled: true` | `running` |
| לא קיים | ה-`cancel` עצמו נכשל עם `no such task` | `unknown` |

ל-`cancel` יש תוצאה משל עצמו (האם הביטול תפס?), ובנפרד היעד מקבל תוצאה — כדי
שה-producer לא ימתין לצ'אנק שלא יגיע. ה-task שהוצא מהתור נשמר עם המעטפת שלו,
כך שהתוצאה הסינתטית מנותבת חזרה לכתובת ה-`reply` הנכונה.

ב-`executor_v2.py` נוגענו **רק** ב-hooks של ביטול: `_running` dict
(`task_id` → Popen / `ShellSession` / `FileWatch`), `stop(task_id)`,
ובדיקת "בוטל לפני שהתחיל" בראש `run()`. סשן נרשם תחת ה-task שפתח אותו, כך
שביטול ה-task הפותח הוא הדרך להרוג REPL בלי לדעת את ה-session id.

---

## 4. Retry

```json
{"kind": "shell", "payload": {"command": "flaky-build",
 "retry": {"max_attempts": 3, "backoff_seconds": [1, 5, 15]}}}
```

* ניסיון 1 לא ממתין. רשימת backoff קצרה מ-`max_attempts` חוזרת על האיבר האחרון
  (`[1, 5]` עם 4 ניסיונות → 1s, 5s, 5s).
* כל ניסיון נכתב כ-`results/<id>-attempt-<n>.json` — כולל המנצח, כך שהתמונה
  המלאה 1..n על הדיסק.
* קבצי attempt הם **לא chunks**: הם יושבים מחוץ לזרם `seq`/`is_final`, כך
  שצרכן שעוקב אחרי פרוטוקול הצ'אנקים לא רואה שני `is_final` ל-task אחד.
* **לא כל כישלון שווה retry.** `block`, `confirm_required` ו-`cancelled` הם
  פסיקת מדיניות או פעולת אופרטור, לא תקלה חולפת — הם לא חוזרים.
* ביטול באמצע ה-backoff נוטש את שאר הניסיונות, ו-`attempts` נשאר על מספר
  הניסיונות שבאמת הגיעו ל-executor.

---

## 5. מקביליות

`max_concurrent_tasks: 3` (ברירת מחדל) — `agent/runner.py` מריץ עד 3 tasks
במקביל על thread pool חסום ב-semaphore, במקום הלופ הסדרתי של v1/v2 שבו build
של עשר דקות חסם כל `read_file` מאחוריו.

**git הוא writer יחיד.** שלושה כותבים חולקים את אותו working copy —
`reporter.Reporter`, `reporter_v2.ReporterV2` ו-`poller.GithubPoller`
(ה-`add -A` שלו סוחף כל מה שהריפורטרים כתבו). כולם נועלים
`GIT_PUSH_LOCK` מ-`agent/locks.py`, מודול נפרד כדי שהשלושה לא יצטרכו לייבא
אחד את השני. בלי זה commits מקבילים היו מתערבבים.

`max_concurrent_tasks: 1` בלי מטא-דאטת תזמון = בדיוק הלופ הסדרתי של v2.

---

## 6. WebSocket — התחבורה המהירה

**כבוי כברירת מחדל** (`ws.enabled: false`), כי הוא פותח פורט.
לא מחליף את git bridge — **שתי התחבורות רצות יחד** וה-executor לא מבדיל ביניהן.

```
ws://127.0.0.1:8477/tasks
```

הצרכן פותח socket, שולח task packet כ-JSON, ומקבל את זרם הצ'אנקים חזרה על
אותו socket כשהוא נוצר — אותם packets, אותו policy engine, אותו חוזה צ'אנקים
כמו `docs/v2_spec.md` §3. רק התחבורה שונה.

**Auth — חובה.** token נוצר ונשמר ב-`~/.iddo-harness/ws-token` (מצב `0600`);
`ws.token` מ-config או מ-ENV גובר ואינו נכתב לדיסק. הצרכן שולח
`Authorization: Bearer <token>` ב-handshake. **בלי token, או עם token שגוי —
`401` מיידי**, לפני שנקרא packet אחד, ובלי לפתוח socket בכלל. הדחייה קורית
ב-`process_request` — אין סיבה לתת socket רק כדי לקחת אותו מיד, ו-401 זה מה
שגם probe שאינו WebSocket (curl, סקאנר, צרכן עם קונפיג שגוי) מבין.
דפדפן לא יכול לקבוע headers ב-handshake, ולכן גם
`Sec-WebSocket-Protocol: bearer.<token>` נתמך. ההשוואה ב-`secrets.compare_digest`.

`websockets>=12` הוא extra אופציונלי (`iddo-harness[agentfabric]`). חסר —
ה-bridge מסרב לעלות ואומר את זה; הוא לא מתדרדר בשקט ל-listener לא מאומת.
נפילה שלו לא מפילה את ה-agent: git bridge הוא התחבורה שתמיד עובדת.

תוצאה שהגיעה ב-socket עדיין נכתבת גם ל-bridge repo, כך ש-audit trail ב-git
נשמר בין אם ה-task הגיע ב-socket ובין אם בריפו.

---

## 7. Metrics

`agent/metrics.py` — אוסף in-memory, נטול תלויות ומקומי לתהליך. שום דבר לא
נשמר: restart מאפס את המונים, וזה בסדר, כי הרשומה העמידה היא ה-audit log.

```
GET http://127.0.0.1:8478/metrics             → JSON
GET http://127.0.0.1:8478/metrics?format=prom → Prometheus text
GET http://127.0.0.1:8478/health              → status, uptime, version
```

`tasks_total`, `tasks_by_kind`, `tasks_by_status`, `shell_bytes_streamed`,
`chunks_pushed`, `retries_total`, `queue_depth`, `active_sessions`,
`active_watches`, `active_tasks`, ושתי משפחות latency עם p50/p95/p99:
`poll_to_start` (כמה ה-task חיכה) ו-`start_to_complete` (כמה הוא לקח).

* כל mutator הוא no-op כש-`enabled: false`, כך שאף קורא לא צריך לעטוף
  אינסטרומנטציה ב-if.
* gauges נמשכים בזמן snapshot (ולא נדחפים), ולכן לא יכולים להיסחף מהדבר שהם
  מתארים. הם נמשכים **מחוץ** ל-lock — הם מושיטים יד לתור ול-executor,
  והחזקת ה-lock בזמן הזה הייתה הזמנה ל-deadlock. gauge שזורק חריגה מחזיר `-1`
  ולא שובר את `/metrics`.
* חלון של 1024 דגימות למשפחה: p99 יציב בלי גדילה בלי גבול בתהליך שרץ שבועות.
* השרת קשור ל-loopback, וכל בקשה שאינה מ-`127.0.0.1`/`::1` מקבלת `403` גם אם
  האופרטור הרחיב את ה-bind — הנקודות האלה מתארות מה המכונה עושה.

> **הערה:** המפרט ביקש `/metrics` על אותו שרת WS. הוא יושב על שרת stdlib נפרד
> (פורט 8478) בכוונה: המדדים צריכים להיות קריאים גם כשהתחבורה המהירה כבויה —
> וכבויה היא ברירת המחדל.

---

## קבצים

| קובץ | תפקיד |
|---|---|
| `config.yaml` | קונפיג הריצה |
| `agent/config.py` | `RuntimeConfig`, ולידציה, ENV override, `ensure_ws_token` |
| `agent/taskqueue.py` | עדיפויות, `not_before`/`deadline`/`depends_on`, `RetryPolicy` |
| `agent/runner.py` | thread pool, retries, ביטול, `deadline_exceeded` |
| `agent/metrics.py` | אוסף מדדים + `/metrics` ו-`/health` |
| `agent/ws_bridge.py` | תחבורת WebSocket מאומתת |
| `agent/locks.py` | `GIT_PUSH_LOCK` המשותף |
| `agent/executor_v2.py` | *(hooks של ביטול בלבד)* `_running`, `stop()` |
| `agent/reporter.py` / `reporter_v2.py` | `send_attempt`, נעילת git |
| `agent/poller.py` | פרסור packets ברמת המודול (משותף עם ה-WS), נעילת git |
| `agent/main.py` | טעינת קונפיג, metrics, runner מקבילי, הרמת ה-WS |
