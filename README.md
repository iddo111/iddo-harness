# Iddo Harness

**הארנס גישה מלאה למחשב של עידו — יד ורגל של Perplexity על המחשב שלך.**

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
│   ├── executor.py          # מבצע פקודות
│   ├── reporter.py          # מדווח תוצאות
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
