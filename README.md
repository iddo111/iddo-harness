# Iddo Harness

**הארנס גישה מלאה למחשב של עידו — יד ורגל של Perplexity על המחשב שלך.**

---

## v3 Highlights — Agent-Native (Track C)

v3 מוסיף 7 יכולות (14 `kind`ים) שהופכות את ה-harness מזוג ידיים לסוכן: הוא מסדר
תלויות, זוכר, ופועל גם כשאף אחד לא מבקש. המפרט המלא:
[`docs/v3_track_c.md`](docs/v3_track_c.md).

| יכולת | `kind` | מה זה נותן |
|---|---|---|
| Sub-tasks | `spawn_task` / `await_tasks` | fan-out מקבילי + join, במקום עשר נסיעות רשת |
| Workflow DAG | `workflow` | גרף תלויות שה-harness פותר לבד, עם `${nodes.x.stdout}` בין צמתים |
| Memory | `memory_set` / `_get` / `_list` / `_delete` | SQLite מקומי — משהו ששורד את ה-task, עם TTL, tags ו-namespaces |
| Scheduler | `schedule_task` / `_list` / `_cancel` | cron / interval / one-shot; "כל בוקר ב-6" בלי producer שלא ישן |
| LLM tool loop | `llm_task` | "תבין את זה על המכונה" כ-kind, דרך אותו policy ואותו audit trail |
| Handshake | `handshake` | נסיעה אחת שאומרת מה ה-harness יודע לעשות, עם negotiation |
| Templates | `run_template` / `template_list` | 5 עבודות מוכנות שנפרשות ל-workflow רגיל |

**עקרונות:** אין מסלול הרצה שני — כל sub-task, צומת workflow, firing מתוזמן,
tool call של מודל ותבנית שנפרשה חוזרים דרך `Executor.run()` ופוגשים את
`PolicyEngine` בזכות עצמם. Policy נאכף **על הילד, לא על המעטפה**: לבקש אישור על
`workflow` זה לבקש מהבעלים לאשר מכולה שהוא לא רואה מה בתוכה. ו-`llm_task` **עוצר**
על `confirm_required` — מודל שיכול לעבור שער אישור מבטל את השער.

**Backward compat:** כל packet של v1 ו-v2 ממשיך לרוץ בדיוק כמו קודם.
ב-`agent/amp.py` וב-`agent/executor_v2.py` לא נגענו, וה-hook של v3 הוא שלוש
שורות ניתוב מעל מסלול v1. packet של v1 בכלל לא בונה את `ExecutorV3`.

**תלות חדשה (אופציונלית):** `croniter>=2.0.0`. בלעדיה יש parser פנימי ל-cron
בחמישה שדות, ו-`validate_cron` עונה אותו דבר בשני המקרים.

---

## v2 Highlights — Agent Fabric

v2 מוסיף 11 יכולות חדשות (14 `kind`ים) מעל v1, בלי לשבור שום דבר קיים.
המפרט המלא: [`docs/v2_spec.md`](docs/v2_spec.md).

| יכולת | `kind` | מה זה נותן |
|---|---|---|
| Streaming shell | `shell_stream` | פלט חי בצ'אנקים במקום "שקט 10 דקות ואז הכל בבת אחת" |
| Interactive sessions | `shell_session_open` / `_write` / `_close` | REPL חי: `python -i`, `psql`, `ssh` — state נשמר בין קריאות |
| Recursive regex search | `grep` | חיפוש regex עם context, דילוג על בינאריים, גיזום `node_modules` |
| Glob | `glob` | `**/*.test.ts`, ממוין לפי mtime |
| Surgical editing | `patch_file` | search/replace אטומי, `replace_all`, `dry_run`, backup אוטומטי, unified diff |
| File watching | `watch_start` / `watch_poll` / `watch_stop` | אירועי מערכת קבצים (watchdog, עם polling fallback) |
| Process management | `process_list` / `process_kill` | רשימת תהליכים + הריגה SIGTERM→SIGKILL עם הגנה עצמית |
| Local HTTP | `http_local` | גישה ל-`localhost:8080` **שלך** — עם allowlist קשיח ל-loopback/LAN |
| Paginated reads | `read_file_chunked` | offset בבייטים מדויק, `total_size` + `next_offset` |
| Chunked results | — | `results/<id>-chunk-<n>.json` עם `seq` + `is_final`, push בבאצ'ים |
| Path-aware policy | — | חוקי `paths.read` / `paths.write` שהיו ב-`policy.yaml` נאכפים סוף-סוף |

### מול Desktop Commander MCP

| יכולת | Desktop Commander | Iddo Harness v2 |
|---|---|---|
| Transport | MCP מקומי (stdio) — הלקוח חייב להיות על אותה מכונה | **GitHub bridge** — עובד מכל מקום, בלי tunnel, בלי פורט פתוח, גם מאחורי NAT |
| Streaming shell | ✅ | ✅ |
| Interactive sessions | ✅ | ✅ + idle reaping ו-`max_sessions` |
| עריכה כירורגית | ✅ | ✅ **אטומית** (all-or-nothing), backup, `dry_run`, diff מוחזר |
| grep / glob | ✅ | ✅ pure-Python, זהה ב-Windows וב-Linux |
| process list/kill | ✅ | ✅ + הגנה מהריגה עצמית / PID 0-1 |
| File watching | ❌ | ✅ |
| HTTP מקומי | ❌ | ✅ |
| קריאה מדפדפת עם byte offsets | חלקי | ✅ |
| Confirm / approval flow | ❌ (סומך על הלקוח) | ✅ auto / confirm / block + audit log |
| Audit trail | ❌ | ✅ כל תוצאה נכנסת ל-git — היסטוריה בלתי ניתנת לשינוי |
| פרוטוקול | MCP | **AMP v1.0** — ניתוב רב-brick עם `reply.to_address` |
| מספר לקוחות במקביל | server אחד ללקוח | הרבה producers לתוך `tasks/` אחד |

איפה Desktop Commander עדיין מוביל: latency. stdio מקומי מגיב במילישניות, git poll לא.
זה המחיר של לעבוד מכל מקום.

**Backward compat:** כל task packet של v1 (`shell`, `read_file`, `write_file`,
`list_dir`) ממשיך לרוץ בדיוק כמו קודם. `agent/executor.py` נשמר, ב-`agent/amp.py`
לא נגענו, ו-`policy.yaml` רק קיבל דפוסים חדשים.

**תלויות חדשות (אופציונליות):** `watchdog>=4.0.0`, `psutil>=5.9.0`.
בלעדיהן ה-harness עדיין עובד — יש fallback ל-polling ול-`ps`/`tasklist`.

---

## מה זה?

`iddo-harness` הוא agent קטן שרץ על המחשב שלך (Windows/Linux/Mac) ומאפשר ל-Perplexity Computer להיות ה-eyes, hands & admin שלך:

- **קריאה:** קורא קבצים, מריץ פקודות, מקבל תוצאות
- **כתיבה:** יוצר קבצים, עורך, מוחק (עם אישור)
- **הרצה:** מריץ סקריפטים, מפעיל שירותים, מתקין תלויות
- **דיווח:** שולח את הפלטים חזרה אליי לניתוח ולפעולה הבאה

---

## איך זה עובד?

```
┌────────────────┐     ┌────────────────┐     ┌────────────────┐
│   Perplexity   │────▶│  Cloud Bridge  │────▶│  iddo-harness  │
│     Cloud      │     │   (GitHub)     │     │  (D:\ / Yigal) │
└────────────────┘     └────────────────┘     └────────────────┘
        ▲                                              │
        └──────────────────────────────────────────────┘
                    reports flow back
```

Perplexity כותב **task-packet** ל-GitHub repo פרטי → הסוכן שלך מושך אותו כל 5 שניות → מבצע → כותב תוצאה חזרה → אני קורא בסבב הבא.

**זו arch פשוטה שעובדת בלי הרשאות חריגות, בלי VPN, ובלי לפתוח פורטים.**

---

## מצבי אישור

הסוכן פועל לפי **policy** שאתה שולט בה:

| רמה | מה קורה | דוגמאות |
|-----|--------|---------|
| **auto** | רץ מייד, מדווח | `ls`, `git status`, `cat`, `find`, `df` |
| **confirm** | שולח לך התראה, ממתין לאישור | `pip install`, `git push`, יצירת קבצים |
| **block** | אף פעם | `rm -rf /`, `del C:\Windows`, `format`, שינוי registry |

הרשימות ב-`policy.yaml`. אתה יכול לערוך.

---

## מה יש בפרויקט

```
iddo-harness/
├── agent/                    # ה-agent עצמו
│   ├── main.py              # הכניסה הראשית
│   ├── poller.py            # מושך משימות מ-GitHub
│   ├── executor.py          # מבצע פקודות (v1) + router ל-v2 ול-v3
│   ├── executor_v2.py       # 14 ה-kinds של Agent Fabric (v2)
│   ├── executor_v3.py       # 14 ה-kinds של Agent-Native (v3)
│   ├── subtasks.py          # spawn_task / await_tasks
│   ├── workflow.py          # גרף התלויות (kind: workflow)
│   ├── memory.py            # זיכרון מתמיד ב-SQLite
│   ├── scheduler.py         # cron / interval / one-shot
│   ├── llm_task.py          # לופ הכלים של המודל + mock providers
│   ├── handshake.py         # גילוי יכולות ו-negotiation
│   ├── templates.py         # 5 התבניות המוכנות
│   ├── reporter.py          # מדווח תוצאות
│   ├── reporter_v2.py       # דיווח בצ'אנקים (streaming)
│   ├── policy.py            # אכיפת policy
│   └── config.py            # הגדרות
├── installer/               # התקנה מהירה
│   ├── install_windows.ps1  # Windows one-liner
│   ├── install_linux.sh     # Linux/DGX
│   └── install_mac.sh       # macOS
├── docs/
│   ├── quickstart.md
│   ├── security.md
│   └── troubleshooting.md
└── policy.yaml              # policy קונפיג
```

---

## Quickstart

### על המחשב Windows שלך (D:\ machine):

```powershell
# 60 שניות
iwr -useb https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_windows.ps1 | iex
```

### על Yigal (DGX Spark):

```bash
curl -sSL https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_linux.sh | bash
```

### על Eran (DGX Station):

```bash
curl -sSL https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_linux.sh | bash
```

בסיום — הסוכן חי, מקבל משימות ממני, מבצע.

---

## Security

- הרפוזיטורי הוא **פרטי** על GitHub
- כל תעבורה עוברת HTTPS דרך GitHub
- אין פורטים פתוחים על המחשב שלך
- אין הרשאות root נדרשות (רק אם המשימה דורשת)
- כל פעולה נרשמת ב-`~/.iddo-harness/audit.log`
- כפתור kill switch: `iddo-harness stop`
- אתה תמיד יכול להעיף את הסוכן: `iddo-harness uninstall`

---

## Status

🚧 **בבנייה** — 23/07/2026

- [x] ארכיטקטורה
- [x] README
- [ ] agent core
- [ ] Windows installer
- [ ] Linux installer
- [ ] policy engine
- [ ] task packet spec
- [ ] first end-to-end test
