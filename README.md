# Iddo Harness

**הארנס גישה מלאה למחשב של עידו — יד ורגל של Perplexity על המחשב שלך.**

---

## v3 Track A — Realtime & Performance

v2 נתן יכולות; Track A מטפל ב-latency ובמה שסביבו. המפרט המלא:
[`docs/v3_track_a.md`](docs/v3_track_a.md).

| יכולת | מה זה נותן |
|---|---|
| **WebSocket bridge** | `ws://127.0.0.1:8477/tasks` — צ'אנקים חוזרים בזמן אמת במקום להמתין ל-poll ול-push. מאומת ב-Bearer token, **כבוי כברירת מחדל**, ורץ *לצד* git bridge ולא במקומו |
| **מקביליות** | `max_concurrent_tasks: 3` — build של עשר דקות לא חוסם יותר כל `read_file` מאחוריו. git נשאר writer יחיד דרך lock משותף |
| **תור עדיפויות** | `priority`, `not_before`, `deadline`, `depends_on` — ה-producer קובע *מתי* ו*באיזה סדר* |
| **ביטול** | `kind: cancel` — SIGTERM→SIGKILL, עם chunk סופי `cancelled: true` כדי שאף אחד לא ימתין לשווא |
| **Retry** | `retry: {max_attempts, backoff_seconds}`, כל ניסיון ב-`results/<id>-attempt-<n>.json`. `block`/`confirm_required` לא חוזרים |
| **Metrics** | `GET /metrics` (JSON או Prometheus) + `/health` — latency p50/p95/p99, מונים לפי kind ו-status, queue depth |
| **`config.yaml`** | קובץ קונפיג ריצה אחד עם ולידציה ו-ENV override, במקום env vars מפוזרים |

**Backward compat:** harness בלי `config.yaml`, בלי מטא-דאטת תזמון ובלי
`ws.enabled` מתנהג בדיוק כמו v2. ב-`amp.py` לא נגענו.

---

## v3 Track B — Security & Trust

v2 נתן יכולות; Track B עונה על "למה שמישהו יסמוך על זה?". המפרט המלא:
[`docs/security_v3.md`](docs/security_v3.md).

| יכולת | מה זה נותן |
|---|---|
| **חתימת תוצאות** | כל תוצאה ו-chunk נחתמים Ed25519 מעל JSON קנוני. push access לריפו כבר לא מספיק כדי לזייף `results/<id>.json` — `python -m agent.verify` תופס את זה |
| **יומן בשרשרת** | `audit.jsonl` שבו כל שורה חותמת על קודמתה ב-HMAC. מחיקה או עריכה של שורה שוברת את כל השרשרת מאותה נקודה, ו-`iddo-harness audit verify` מצביע על השורה הראשונה שנשברה |
| **כספת סודות** | `{{secret:openai_key}}` בתוך ה-packet; הערך מוחלף רגע לפני ההרצה ונמחק מהפלט. מפתח API כבר לא צריך לעבור דרך git ולשבת ב-`results/` לנצח |
| **Sandbox** | `none` / `light` / `strict` לפי `kind`. ה-policy מחליט *אם* פקודה תרוץ; זה מגביל *במה היא נוגעת* אחרי שכן |
| **זרימת אישורים** | `local` / `notification` / `remote` — מצטברים, לא חלופיים. timeout של 5 דקות במקום 30, כי prompt שמתמהמה זה prompt שמאשרים בלי לקרוא |
| **Health endpoint** | `/health`, `/metrics`, `/audit/tail`, `/policy` — localhost בלבד, פעמיים (bind + בדיקת peer). **כבוי כברירת מחדל** |
| **Policy linter** | `iddo-harness policy lint` — תופס `block:` ריק, דפוס שלא מתאים לכלום, וכפילויות בין `auto_allow` ל-`block`. exit codes ל-CI |

**Backward compat:** `policy.yaml` בלי בלוק `security:` מתנהג בדיוק כמו v2. בלי
מפתחות — תוצאות יוצאות בלי חתימה, עם warning אחד. ב-`amp.py` לא נגענו.

**תלות חדשה (אופציונלית):** `cryptography>=42.0.0` (`pip install 'iddo-harness[security]'`).

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

איפה Desktop Commander הוביל: latency. stdio מקומי מגיב במילישניות, git poll לא —
זה היה המחיר של לעבוד מכל מקום. **v3 Track A סוגר את הפער** עם תחבורת
WebSocket אופציונלית לצד git bridge: כשהצרכן על אותה מכונה (או בקצה השני של
tunnel קיים) הוא מקבל את הצ'אנקים בזמן אמת, ובלי לאבד את היכולת לעבוד מכל מקום
כשהוא לא.

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
│   ├── executor.py          # מבצע פקודות (v1) + router ל-v2
│   ├── executor_v2.py       # 14 ה-kinds של Agent Fabric (v2)
│   ├── reporter.py          # מדווח תוצאות
│   ├── reporter_v2.py       # דיווח בצ'אנקים (streaming)
│   ├── policy.py            # אכיפת policy
│   ├── config.py            # policy config (v1) + RuntimeConfig (v3)
│   ├── runner.py            # thread pool, retries, ביטול (v3)
│   ├── taskqueue.py         # עדיפויות, תלויות, חלונות זמן (v3)
│   ├── metrics.py           # אוסף מדדים + /metrics ו-/health (v3)
│   ├── ws_bridge.py         # תחבורת WebSocket מאומתת (v3)
│   ├── locks.py             # GIT_PUSH_LOCK המשותף (v3)
│   ├── signing.py           # חתימת תוצאות Ed25519 (v3 Track B)
│   ├── verify.py            # מאמת חתימות מה-CLI (v3)
│   ├── audit.py             # יומן ביקורת בשרשרת HMAC (v3)
│   ├── secrets_vault.py     # כספת מוצפנת + {{secret:name}} (v3)
│   ├── sandbox.py           # none / light / strict (v3)
│   ├── approval.py          # local / notification / remote (v3)
│   └── health_server.py     # /health, /audit/tail, /policy (v3)
├── installer/               # התקנה מהירה
│   ├── install_windows.ps1  # Windows one-liner
│   ├── install_linux.sh     # Linux/DGX
│   ├── install_mac.sh       # macOS
│   ├── gen_keys.py          # יצירת זוג מפתחות למכונה (v3)
│   └── policy_lint.py       # לינטר ל-policy.yaml (v3)
├── docs/
│   ├── quickstart.md
│   ├── security.md
│   ├── security_v3.md
│   ├── v2_spec.md
│   ├── v3_track_a.md
│   └── troubleshooting.md
├── config.yaml              # קונפיג ריצה (v3)
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
