# v3 Track B — Security & Trust

v2 נתן ל-harness יכולות. Track B עונה על השאלה שנשארה פתוחה: **למה שמישהו
יסמוך על הדבר הזה?**

ה-harness מריץ פקודות שרירותיות על מכונה אישית, לפי הוראות שמגיעות דרך ריפו
git. שלוש הנחות היו מובלעות עד עכשיו, ואף אחת מהן לא נבדקה:

1. שהתוצאה ב-`results/` באמת הגיעה מה-harness (ולא ממי שיש לו push access).
2. שמה שקרה באמת קרה — ושאי אפשר למחוק את השורה שמעידה על זה.
3. שפקודה שה-policy אישרה מוגבלת למה שהיא הייתה אמורה לגעת בו.

Track B הופך את שלושתן לניתנות לאימות. **הכל opt-in או degrade-to-v2**:
`policy.yaml` בלי בלוק `security:` מתנהג בדיוק כמו v2.

---

## 1. חתימת תוצאות — `agent/signing.py`

הריפו פרטי, אבל "פרטי" זה לא "אותנטי". PAT שדלף, סוכן שני עם push access, או
CI token ישן — כל אחד מהם יכול לכתוב `results/build-42.json`, והצרכן יפעל לפיו.

כל תוצאה שיוצאת נחתמת **Ed25519 מעל JSON קנוני**:

```json
{ "v": 1, "id": "...", "payload": {...}, "signature": "<base64>" }
```

* `signature` **לא נכלל** בבייטים שנחתמים — אחרת הערך היה צריך להכיל את עצמו.
* `amp.py` מתעלם משדות top-level לא מוכרים, ולכן מעטפת חתומה עוברת פרסור אצל
  צרכן v1/v2 שלא יודע כלום על חתימות. **ב-`amp.py` לא נגענו.**
* המפתחות: `python -m installer.gen_keys` יוצר `~/.iddo-harness/agent.key`
  (PKCS#8, `0600`) ו-`agent.pub`, ומעתיק את הציבורי ל-`docs/agent.pub` כדי
  שיהיה אפשר לקמיט אותו — צרכן מאמת בלי לגעת במכונה אף פעם.
* **אין מפתח → אין חתימה, עם warning אחד.** התקנה שלא הריצה `gen_keys` עדיין
  מדווחת תוצאות. לא לדווח היה גרוע יותר מלדווח בלי חתימה.
* אימות: `python -m agent.verify results/*.json` — שורת `OK`/`FAIL` לקובץ,
  exit code לא-אפס אם משהו נכשל, כך שזה נכנס ישר ל-CI.

`reporter_v2` חותם **כל chunk בנפרד**, ולא את המשימה כולה: צרכן פועל לפי
chunk 7 הרבה לפני ש-chunk אחרון קיים, וחתימה ברמת ה-task הייתה מגיעה מאוחר מדי.

---

## 2. יומן ביקורת בשרשרת — `agent/audit.py`

`~/.iddo-harness/audit.jsonl` — שורה אחת לכל אירוע רגיש: החלטות policy (עם
החוק שהתאים), קריאות סוד, kill/cancel, שימוש במפתח, אישורים, startup ו-shutdown.

קובץ append-only שווה בדיוק כמו מערכת הקבצים שמתחתיו: מי שיכול לערוך את
`audit.jsonl` יכול גם למחוק את השורה שמספרת שהוא ערך. לכן כל שורה נושאת hash
ממופתח שכולל את ה-hash של קודמתה:

```
hmac(N) = HMAC-SHA256(key, hmac(N-1) || canonical_json(record בלי hmac))
```

עריכה, שינוי סדר או מחיקה של שורה אחת שוברים כל hash מאותה נקודה והלאה, וזיוף
תיקון דורש את `~/.iddo-harness/audit.key` (`0600`, נוצר בשימוש ראשון).
`iddo-harness audit verify` מדווח על **השורה הראשונה** שנשברה.

* `record()` **אף פעם לא זורק.** כשל בכתיבת ביקורת לא מפיל את הפעולה שהוא
  אמור לתעד — הוא רק צועק בלוג.
* כל שדה עובר masking לפני הכתיבה: היומן הוא בדיוק השורה שאדם יקרא אחר כך,
  ולכן המקום האחרון שבו סוד אמור להופיע.
* רוטציה יומית, והקובץ נפרד מ-`audit.log` הרגיל — שורת לוג תועה הייתה שוברת
  את כל השרשרת.

---

## 3. כספת סודות — `agent/secrets_vault.py`

task packet עובר דרך ריפו git ונוחת ב-`results/` לנצח. ולכן פקודה שצריכה מפתח
API לא יכולה **להכיל** אותו. היא נושאת הפניה:

```json
{"kind": "shell", "payload": {
  "command": "curl -H 'X-API-Key: {{secret:openai_key}}' https://api.openai.com/v1/models"}}
```

הערך מוחלף רגע אחד לפני `Popen`, וקיים רק ברשימת הארגומנטים של אותה קריאה:
הוא לא נכתב ל-task packet, לא ללוג, לא ל-audit, לא ל-results, וכל הופעה שלו
ב-**פלט של הפקודה עצמה** נמחקת לפני הפרסום.

* אחסון: `~/.iddo-harness/secrets.enc` — Fernet token יחיד. מפתח ה-Fernet
  נגזר (HKDF-SHA256) מ-`agent.key`, כך שהכספת קשורה לאותו סוד-שורש כמו החתימה.
* ניהול: `iddo-harness secret set|list|rm`. `list` מחזיר שמות בלבד.
* **placeholder שלא נפתר לא נשלח.** בלי כספת, פקודה שמפנה ל-`{{secret:x}}`
  נכשלת ברעש — עדיף מלשלוח `{{secret:x}}` מילולי ל-endpoint מרוחק.
* ה-policy מחליט על ה-**צורה הממוסכת**: `{{secret:foo}}` הופך ל-`<vault:foo>`,
  כך שדפוסי החסימה `*secret*` / `*token*` / `*password*` ממשיכים לדחות סיסמה
  שהודבקה inline, בזמן שהפניה לכספת עוברת. השימוש בכספת הוא הדרך המאושרת.

---

## 4. Sandbox — `agent/sandbox.py`

ה-policy מחליט **אם** פקודה תרוץ. ה-sandbox מגביל **במה היא יכולה לגעת** אחרי
שהיא כבר רצה — עד היום כל `shell_stream` ירש את מלוא ההרשאות של תהליך הסוכן.

| רמה | מה מוגבל |
|---|---|
| `none` | כלום. **ברירת המחדל** — התנהגות לא משתנה עד שמישהו בוחר אחרת |
| `light` | `/tmp` פרטי. רשת ומערכת קבצים אחרת לא נוגעים |
| `strict` | בלי רשת, וקריאה-בלבד מחוץ ל-`cwd` של המשימה |

נקבע לפי `kind` ב-`policy.yaml` תחת `sandbox.per_kind`, עם נפילה ל-`sandbox.default`.

Backends: `firejail` ב-Linux, Job Object דרך `pywin32` ב-Windows. **חסר backend
→ warning והרצה רגילה**, לא כישלון: sandbox שמפיל משימות זה sandbox שמכבים.

---

## 5. זרימת אישורים — `agent/approval.py`

`ConfirmManager` כבר מחנה משימה כקובץ וממתין ל-`approved-<id>.json`. זה עובד —
אבל רק אם מישהו מסתכל על התיקייה. המודול הזה שומר על תור הקבצים כמקור האמת
ומוסיף את החלק שמושך תשומת לב אנושית אליו:

| mode | מה קורה |
|---|---|
| `local` | קבצים בלבד — התנהגות v1, **ברירת המחדל** |
| `notification` | + התראת דסקטופ (`notify-send` / toast / `osascript`) |
| `remote` | + דחיפה דרך ה-WS bridge של Track A |

המצבים **מצטברים ולא חלופיים**: `remote` עדיין כותב את קובץ ה-pending, כך
שבקשה שנשלחה ב-socket ואיש לא ענה עליה עדיין ניתנת לאישור מה-CLI, וקריסה
באמצע לא מאבדת כלום.

שני הבדלים מכוונים מול `ConfirmManager`:

* **timeout של 300 שניות** במקום 1800. prompt אבטחה שמתמהמה חצי שעה הוא prompt
  שמאשרים בלי לקרוא.
* **כל החלטה נרשמת** — כולל ה-deny האוטומטי ב-timeout.

---

## 6. Health endpoint — `agent/health_server.py`

ה-harness הוא שירות רקע. כשהוא מפסיק לקחת משימות, הדרך היחידה לגלות הייתה
לקרוא קובץ לוג. עכשיו יש לו פנים:

```
GET /health            {"status": "ok"|"degraded", "uptime_sec", "version"}
GET /metrics           מונים (JSON, או Prometheus עם ?format=prom)
GET /audit/tail?n=100  N הרשומות האחרונות
GET /policy            ה-policy הפעיל, לדיבוג החלטה
```

**כבוי כברירת מחדל** (`health.enabled: false`), ו-localhost בלבד **פעמיים**:
ה-socket נקשר ל-`127.0.0.1`, *וגם* כל handler בודק מחדש את כתובת ה-peer ומחזיר
`403` אחרת. הבדיקה השנייה חשובה כי קונפיג עתידי עלול להרחיב את ה-bind, ובגלל
ש-`/audit/tail` ו-`/policy` חושפים בדיוק את מה שתוקף ירצה לדעת בשלב הבא —
אילו חוקים נאכפים, ומה כבר הותר.

`n` נחתך ל-1000: probe בריאות לא אמור להיות דרך לקרוא את כל ההיסטוריה.

---

## 7. Policy linter — `installer/policy_lint.py`

```
python -m installer.policy_lint policy.yaml     # או: iddo-harness policy lint
```

`policy.yaml` הוא הדבר היחיד שעומד בין task packet למכונה, והוא נערך ביד.
טעות הקלדה לא מכריזה על עצמה: `block:` ריק, דפוס שלא מתאים לכלום, או אותו נתיב
גם ב-`auto_allow` וגם ב-`block` — כולם נטענים מצוין ואוכפים פחות ממה שהמחבר חשב.

Exit codes ל-CI: `0` נקי, `1` אזהרות, `2` שגיאות (אסור לפרוס).

> **הערה:** הדרישה "כל דפוס חייב להיות regex תקין" מיושמת כ-
> `fnmatch.translate(pattern)` מתקמפל — כי דפוסי policy הם globs של fnmatch,
> לא regexes (`PolicyEngine` מתאים אותם ב-`fnmatch.fnmatchcase`). זו הצורה
> המשמעותית של הדרישה: היא תופסת את מחלקות התווים והסוגריים ש-fnmatch מעביר
> ישירות ל-`re` ושנכשלים בשקט.

---

## תלות חדשה

`cryptography>=42.0.0` — extra בשם `security`. בלעדיו ה-harness רץ, אבל תוצאות
יוצאות בלי חתימה ו-`{{secret:...}}` לא ניתן לפתרון. מומלץ בחום.

```bash
pip install 'iddo-harness[security]'
```

---

## קבצים

| קובץ | תפקיד |
|---|---|
| `agent/signing.py` | חתימה ואימות Ed25519 מעל JSON קנוני |
| `agent/verify.py` | `python -m agent.verify <file>...` |
| `agent/audit.py` | יומן JSONL בשרשרת HMAC + `verify_chain` |
| `agent/secrets_vault.py` | כספת Fernet, `{{secret:name}}`, redaction |
| `agent/sandbox.py` | `none` / `light` / `strict`, firejail / Job Object |
| `agent/approval.py` | `local` / `notification` / `remote` |
| `agent/health_server.py` | `/health`, `/metrics`, `/audit/tail`, `/policy` |
| `installer/gen_keys.py` | יצירת זוג מפתחות חד-פעמית למכונה |
| `installer/policy_lint.py` | לינטר ל-`policy.yaml` |
| `agent/policy.py` | *(שילוב)* masking + רישום כל החלטה ליומן |
| `agent/executor.py` / `executor_v2.py` | *(שילוב)* פתרון סודות ועטיפת sandbox |
| `agent/reporter.py` / `reporter_v2.py` | *(שילוב)* חתימה של תוצאות ו-chunks |
| `agent/main.py` | *(שילוב)* הרכבה, אירועי startup/shutdown, health |
