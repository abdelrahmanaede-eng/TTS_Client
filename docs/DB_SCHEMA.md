# Database Schema — TimeTracker

قاعدة البيانات من نوع **SQLite**، ومعرّفة في `timetracker/database.py` (الثابت `SCHEMA` + دوال الـ migrations في `_init_db`).

- **ملف القاعدة:** `%APPDATA%/TimeTracker/data.db` (افتراضياً عبر `get_app_dir() / "data.db"`).
- **إصدار الـ schema الحالي:** `6` (ثابت `SCHEMA_VERSION`)، مخزّن في جدول `schema_version`.
- **النسخ الاحتياطي:** قبل أي ترقية schema يُعمل backup تلقائي في `Documents\TimeTracker\Backups`.

> ملاحظة: لا يوجد ملف `.db` داخل مجلد المشروع؛ الـ schema أدناه هو ما ينشئه الكود عند أول تشغيل.

---

## الجداول (6 جداول)

| # | الجدول | الغرض |
|---|--------|-------|
| 1 | `schema_version` | تخزين رقم إصدار الـ schema الحالي |
| 2 | `input_events` | أحداث الإدخال (طوابع زمنية + التطبيق النشط + المشروع) |
| 3 | `sessions` | جلسات الاجتماع / الإضافي / الاستراحة |
| 4 | `settings` | الإعدادات والقيم المساندة (key/value) |
| 5 | `projects` | المشاريع |
| 6 | `daily_summary` | ملخّصات يومية مجمّعة (للاحتفاظ بالبيانات بعد حذف الأحداث القديمة) |

---

## 1. `schema_version`
| العمود | النوع | القيود |
|--------|-------|--------|
| `version` | INTEGER | NOT NULL |

> صف واحد فقط يحمل رقم الإصدار (القيمة الحالية `6`).

## 2. `input_events`
| العمود | النوع | القيود / الافتراضي |
|--------|-------|-------------------|
| `ts` | INTEGER | **PRIMARY KEY** (طابع زمني يونكس) |
| `app_name` | TEXT | NULL — اسم التطبيق النشط، الافتراضي `Unknown` عند الإدراج |
| `source` | TEXT | DEFAULT `'input'` (مثلاً `input` / `confirm`) |
| `project_id` | INTEGER | NULL — يُضاف عبر migration، مرجع منطقي إلى `projects.id` |
| `day` | TEXT | NULL — اليوم بصيغة `YYYY-MM-DD` (محسوب محلياً ومُفهرس) |

## 3. `sessions`
| العمود | النوع | القيود / الافتراضي |
|--------|-------|-------------------|
| `id` | INTEGER | **PRIMARY KEY AUTOINCREMENT** |
| `start_ts` | INTEGER | NOT NULL |
| `end_ts` | INTEGER | NULL — جلسة مفتوحة إذا كان NULL |
| `session_type` | TEXT | NOT NULL — CHECK في: `meeting`, `overtime`, `break` |
| `source` | TEXT | NOT NULL — CHECK في: `auto`, `tray`, `manual` |
| `note` | TEXT | NULL |
| `reason` | TEXT | NULL |
| `is_pause` | INTEGER | NOT NULL DEFAULT `0` |
| `project_id` | INTEGER | NULL — مرجع منطقي إلى `projects.id` |
| `day` | TEXT | NULL — تاريخ البداية `YYYY-MM-DD` |
| `change_day` | TEXT | NULL — تاريخ آخر تعديل/إغلاق `YYYY-MM-DD` |

## 4. `settings`
| العمود | النوع | القيود |
|--------|-------|--------|
| `key` | TEXT | **PRIMARY KEY** |
| `value` | TEXT | NOT NULL |

> يُستخدم أيضاً لحفظ عدّاد إصدار البيانات لكل يوم بالمفتاح `dayver:YYYY-MM-DD`.

## 5. `projects`
| العمود | النوع | القيود / الافتراضي |
|--------|-------|-------------------|
| `id` | INTEGER | **PRIMARY KEY AUTOINCREMENT** |
| `name` | TEXT | NOT NULL, **UNIQUE** (مع `COLLATE NOCASE`) |
| `archived` | INTEGER | NOT NULL DEFAULT `0` |
| `created_ts` | INTEGER | NULL |
| `color` | TEXT | NULL |

## 6. `daily_summary`
| العمود | النوع | القيود / الافتراضي |
|--------|-------|-------------------|
| `date` | TEXT | **PRIMARY KEY** — `YYYY-MM-DD` |
| `worked_sec` | INTEGER | NOT NULL DEFAULT `0` |
| `overtime_sec` | INTEGER | NOT NULL DEFAULT `0` |
| `meeting_sec` | INTEGER | NOT NULL DEFAULT `0` |
| `break_sec` | INTEGER | NOT NULL DEFAULT `0` |
| `target_sec` | INTEGER | NOT NULL DEFAULT `0` |
| `app_secs` | TEXT | NOT NULL DEFAULT `'{}'` — JSON: ثوانٍ لكل تطبيق |
| `project_secs` | TEXT | NOT NULL DEFAULT `'{}'` — JSON: ثوانٍ لكل مشروع |
| `unclassified_meeting_sec` | INTEGER | NOT NULL DEFAULT `0` (أُضيف في v4) |
| `updated_ts` | INTEGER | NULL |
| `data_version` | INTEGER | NOT NULL DEFAULT `0` (أُضيف في v4) |

---

## الفهارس (Indexes)

| الفهرس | الجدول | الأعمدة |
|--------|--------|---------|
| `idx_events_ts` | `input_events` | `ts` |
| `idx_events_project` | `input_events` | `project_id` |
| `idx_events_day` | `input_events` | `day` |
| `idx_sessions_start_end` | `sessions` | `start_ts, end_ts` |
| `idx_sessions_open` | `sessions` | `session_type, end_ts` |
| `idx_sessions_project` | `sessions` | `project_id` |
| `idx_sessions_day` | `sessions` | `day` |
| `idx_sessions_change_day` | `sessions` | `change_day` |
| `idx_daily_summary_date` | `daily_summary` | `date` |

> ملاحظة: `date` في `daily_summary` هو PRIMARY KEY، لكن الفهرس `idx_daily_summary_date` يُنشأ صراحةً أيضاً.

---

## العلاقات (منطقية — لا توجد FOREIGN KEY صريحة)

- `input_events.project_id` → `projects.id`
- `sessions.project_id` → `projects.id`
- `daily_summary.date` يلخّص بيانات `input_events` و`sessions` لنفس اليوم.

## ملاحظات إضافية

- العطلة (holidays) **لا تُخزّن في قاعدة البيانات**؛ هي محفوظة في ملف الإعدادات (`config.json`) كقائمة تواريخ.
- الـ migrations تصير داخل معاملة واحدة (`BEGIN IMMEDIATE`) وهي idempotent، وتشمل:
  - إضافة أعمدة `app_name` / `source` / `project_id` / `day` إلى `input_events`.
  - إضافة `note` / `reason` / `is_pause` / `day` / `change_day` / `project_id` إلى `sessions`.
  - ترقية قيود CHECK في `sessions` (إضافة `break` و`manual`).
  - إضافة `unclassified_meeting_sec` و`data_version` إلى `daily_summary`.
- كل الأعمدة المضافة حديثاً تُراجَع عبر `PRAGMA table_info` قبل الإضافة، حتى تعمل الترقية على قواعد بيانات قديمة جداً.
