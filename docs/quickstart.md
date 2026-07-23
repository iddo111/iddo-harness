# Iddo Harness — התחלה מהירה

## מה זה נותן לך

**גישה מלאה של Perplexity Computer למחשב שלך** — קריאה, כתיבה, הרצה, שליטה — עם policy שאתה שולט בה.

---

## מה תצטרך (5 דקות)

- **מחשב Windows/Linux/Mac** מחובר לרשת
- **Python 3.10+** מותקן
- **git** מותקן
- **GitHub CLI (gh)** מותקן ומחובר (`gh auth login`)

---

## התקנה — 60 שניות

### Windows (על D:\ machine):

פותחים PowerShell כרגיל (לא כאדמין), ומריצים:

```powershell
iwr -useb https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_windows.ps1 | iex
```

### Linux/DGX (על Yigal ועל Eran):

```bash
curl -sSL https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_linux.sh | bash
```

בסיום — הסוכן חי. אתה יכול לסג��ר את החלון.

---

## איך זה עובד ביום-יום

כשאני (Perplexity) צריך לעשות משהו על המחשב שלך:

1. אני כותב **task packet** לריפו `iddo111/iddo-harness-bridge`
2. הסוכן מזהה תוך 5 שניות
3. Policy engine בודק:
   - **auto** → רץ מייד
   - **confirm** → שולח לך התראה
   - **block** → נדחה
4. הסוכן מריץ, שולח לי תוצאה
5. אני קורא את התוצאה בסבב הבא, ממשיך

**אתה לא צריך לעשות כלום ביום-יום.** רק לאשר פה ושם.

---

## דוגמאות למה שאני יכול לעשות

### דברים שרצים מיד (auto)
- לרכז את כל הקבצים שלך ב-D:\CLAUDE ולתת לך אינדקס
- לבדוק איזה שירותים רצים על Yigal
- לקרוא לוגים של שירות שנתקע
- להריץ `git status` על 30 ריפוים ולסכם מה השתנה

### דברים שדורשים אישור (confirm)
- להתקין חבילת Python חדשה
- לעשות commit + push לפרויקט
- להפעיל/לעצור systemd service
- לכתוב קובץ חדש בפרויקט

### דברים אסורים לחלוטין (block)
- מחיקת מערכת ההפעלה
- שינויים ב-registry
- קריאת סיסמאות/טוקנים
- format של דיסק

---

## סטטוס וניהול

### לראות מה הסוכן עושה
```bash
# Windows
Get-ScheduledTaskInfo -TaskName IddoHarness
Get-Content $HOME\.iddo-harness\audit.log -Tail 50

# Linux
systemctl --user status iddo-harness
journalctl --user -u iddo-harness -f
```

### לעצור את הסוכן
```bash
# Windows
Stop-ScheduledTask -TaskName IddoHarness

# Linux
systemctl --user stop iddo-harness
```

### להסיר לגמרי
```bash
# Windows
Unregister-ScheduledTask -TaskName IddoHarness -Confirm:$false
Remove-Item -Recurse $HOME\.iddo-harness

# Linux
systemctl --user disable --now iddo-harness
rm -rf ~/.iddo-harness
```

---

## שאלות נפוצות

**ש: זה בטוח?**
כן. הסוכן קורא task packets רק מ-repo פרטי שלך. Policy engine חוסם פקודות מסוכנות. יש audit log מלא של כל פעולה. יש kill switch.

**ש: זה חייב להיות פתוח 24/7?**
לא. הוא רץ אוטומטית כשהמחשב שלך פועל. כשהמחשב כבוי — פשוט לא רץ. כשמתחיל — ממשיך מהמקום שעצר.

**ש: מה אם משהו נתקע?**
- audit.log מראה מה הפעולה האחרונה
- systemctl restart / Start-ScheduledTask מרענן
- אם בטוח שיש בעיה — מסירים בהוראה אחת

**ש: אני יכול לערוך את ה-policy?**
כן. `~/.iddo-harness/policy.yaml`. שנה, שמור, הסוכן מטעין מחדש בסבב הבא.
