# Iddo Harness ↔ Almaware Protocol (AMP v1.0)

## למה זה חשוב

Iddo Harness הוא **brick** לכל דבר לפי AMP. זה אומר:

1. **כל מודל** — Perplexity, Claude, GPT, Gemini, Grok — יכול לתת לו הוראות באותה שפה
2. **אותו envelope** לכולם — אין "המודל של קלוד לחוד"
3. **descriptor `/almaware`** — הוא מכריז על עצמו: מי אני, איזו גרסה, מה אני יודע לעשות

זה בדיוק הפתרון לאבני-הלגו-המזויפות שראית: **stud pattern אחד, אינסוף bricks.**

---

## Task Packet כ-AMP envelope

במקום ה-JSON הישן, כל task packet מ-**כל מודל** נשלח בפורמט AMP:

```json
{
  "v": 1,
  "id": "01J8Z3K9RXTG3V6P6M8AH1QF7C",
  "ts": "2026-07-23T00:42:00Z",
  "direction": "outbound",
  "source": {
    "brick": "perplexity-computer",     // או "claude-code", "chatgpt-codex", "gemini"
    "instance": "session-abc123"
  },
  "channel": "harness",
  "identity": {
    "self": true,
    "canonical": "user:iddo111",
    "display_name": "Iddo"
  },
  "payload": {
    "type": "harness_task",
    "body": {
      "kind": "shell",
      "command": "dir D:\\CLAUDE /s /b",
      "paths": ["D:\\CLAUDE"],
      "timeout_sec": 120
    }
  },
  "reply": {
    "to_channel": "harness",
    "to_address": "session-abc123"
  }
}
```

**כל מודל שרוצה להשתמש ב-Harness** מייצר envelope עם ה-`source.brick` שלו וכותב לריפו bridge.

---

## Descriptor — הצהרה עצמית

Iddo Harness עונה על `/almaware`:

```json
{
  "brick": "iddo-harness",
  "version": "1.0.0",
  "amp_version": 1,
  "channels": ["harness"],
  "payload_types": ["harness_task"],
  "capabilities": [
    "shell.exec",
    "file.read",
    "file.write",
    "dir.list"
  ],
  "health": "ok",
  "rate_limits": {
    "tasks_per_minute": 12,
    "concurrent_tasks": 3
  },
  "consumers_accepted": [
    "perplexity-computer",
    "claude-code",
    "chatgpt-codex",
    "gemini",
    "grok",
    "*"
  ]
}
```

---

## Multi-model access — איך זה עובד

```
Perplexity  ─┐
Claude Code ─┤
ChatGPT     ─┼──▶ bridge repo (iddo-harness-bridge) ──▶ Iddo Harness ──▶ D:\, Yigal, Eran
Gemini      ─┤       (AMP envelopes)
Grok        ─┘
```

- כל מודל מבצע `git commit` של envelope לתיקיית `tasks/`
- ה-Harness מעבד לפי סדר timestamp
- תוצאה חוזרת כ-envelope עם `direction: "inbound"`, ה-`reply.to_address` מציין את מקור הבקשה
- כל מודל קורא רק את התוצאות המיועדות לו

---

## Ownership & auth

- ריפו bridge = **פרטי**, בבעלות `iddo111`
- כל מודל שאתה רוצה לתת גישה — אתה מוסיף אותו כ-collaborator לריפו
- ה-Harness עצמו לא מכיר את המודל, רק את ה-envelope
- **אתה בשליטה מלאה** על מי כותב שם

---

## הצהרת התאמה

בהתאם ל-§7 MUST #12 של AMP: Iddo Harness **לא מוכרז כ-AMP-conformant** עד שיהיו:
- [ ] envelope validation מלאה מול השמע של AMP
- [ ] `/almaware` descriptor endpoint עובד
- [ ] test suite עם envelope לדוגמה מכל 5 המודלים
- [ ] rate-limit enforcement
- [ ] audit log ב-JSONL מלא

**סטטוס נוכחי:** MVP, טרם AMP-conformant. יעד: v1.0 AMP-conformant תוך 3 sprints.
