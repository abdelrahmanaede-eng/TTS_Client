# الباقي من مراجعة الكود

آخر تحديث: 2026-11-18 — الحالة دي هي المرجع الوحيد للشغل المتبقي.
السجل الكامل للي اتعمل موجود في `timetracker/DECISIONS.md` (أقسام 31–33).

---

## ⚠️ مطلوب منك (برّه الكود)
- **SEC-01 — Revoke توكن GitHub:** التوكن القديم اتشال من `scripts/upload_release.py`،
  بس لسه لازم تعمل Revoke/rotation في GitHub وتراجع سجل النشاط.
  أي تأخير هنا يقلّل من قيمة باقي الإصلاحات الأمنية.

---

## مفتوح في الكود (مرتب حسب الحجم)

### 1. PERF-02 — اتصال SQLite لكل عملية — 🔴 كبير
- **المكان:** `timetracker/database.py` — `_get_connection()` / `_execute_with_retry()`.
- **المطلوب:** thread-local persistent connection بدل فتح/إغلاق اتصال لكل استعلام،
  مع تجميع حقول الحالة في استعلام واحد وتخزين open-session IDs مؤقتاً.
- **الخطر:** بيلمس كل استعلام في التطبيق → محتاج جولة اختبارات كاملة لوحده.

### 2. QUAL-01 — دالة الإعدادات العملاقة — 🔴 كبير
- **المكان:** `timetracker/dialogs.py` — `show_settings_dialog_async()` (~830 سطر).
- **المطلوب:** تقسيمها إلى builder لكل تبويب + save/apply مشترك، من غير تغيير السلوك.

### 3. QUAL-02 — استثناءات مبتلعة — 🟡 متوسط
- **المكان:** `timetracker/dialogs.py` — ~31 `except Exception: pass`.
- **المطلوب:** تضييق نوع الاستثناء وتسجيل WARNING بسياق، مع helper مركزي للأخطاء.

### 4. QUAL-03 (باقي) — تكرار تجميع صفوف التقارير — 🟡 متوسط
- **المكان:** `timetracker/reporter.py` — تجميع صفوف الـ Daily Summary متكرر بين
  مسار Excel ومسار CSV/PDF.
- **المطلوب:** مصدر بيانات واحد للصفوف يستخدمه كل المُصدِرين.
  (جزء الـ bucket-by-day اتعمل بالفعل في `retention.bucket_records_by_day()`.)

---

## اتقفل في آخر جولة (للعلم فقط)
- **SEC-07 (الباقي):** التوكن اتحذف من سجل المتصفح (history.replaceState) ومن سجلات
  الخادم (redaction في `log_message`) ومن ملف الداشبورد على القرص.
- **PERF-06:** أعمدة `day` / `change_day` محسوبة ومُفهرسة + backfill للقواعد القديمة،
  و`get_recorded_dates` / `get_last_data_change_by_day` ما بيعملوش full scan بعد كده.
  رقم الـ schema بقى 6.
- اختبارات جديدة: `tests/test_perf06_sec07.py` (4).
- **تحسين الداشبورد:** الـ Timeline بقى يعرض نطاق العمل الفعلي (09:00–18:00 مثلًا)
  بدل 24 ساعة ثابتة، مع ruler ديناميكي — `tests/test_timeline_window.py` (4).
- **إصلاح Refresh:** زرار التحديث/F5 بقى شغال عبر Strict cookie + sessionStorage (التوكن مش في
  السجل ولا في ملف الداشبورد)، وفشل التحديث بقى بيظهر رسالة بدل ما يعمل reload لصفحة 401.
- **RTL:** مؤشرات الـ Checkbox/Radio بقت على اليمين في العربي (ttk layout + factory موحّد)،
  وترجمة قائمة التراَ بقت بتمرّ عبر `bidi_text`. سهم القوائم الفرعية في قائمة Windows الأصلية
  مش قابل للعكس من Python (حدّ المنصّة). `tests/test_rtl_widgets.py` (6).
- **الاختبارات الكاملة:** `python -m pytest tests -q` → **332 passed**.
