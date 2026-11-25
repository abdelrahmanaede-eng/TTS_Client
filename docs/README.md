# Bussma EA — دليل المشروع الشامل

> اقرأ هذا الملف أولًا إذا أردت أن تفهم "الدنيا" عندي: ما هو المشروع، أين كل حاجة، كيف تشغّله وتختبره وتبنيه وتنشره، وإيه اللي لسه مفتوح.
> المرجع التقني الكامل بالإنجليزية موجود في `timetracker/README.md`. هذا الملف ملخّص شامل بالعربي + مرجع سريع بالإنجليزي في الآخر.

---

## 1. فكرة عامة

- **المنتج:** `TimeTracker` — تطبيق Windows يعمل في شريط المهام (system tray) يجيب على سؤال واحد: «كام ساعة اشتغلت النهارده؟».
- **الفلسفة:** محلي بالكامل (local-first) — مفيش telemetry، ومفيش صلاحيات admin. الميزات الشبكية اختيارية ومُفعّلة يدويًا (تحديث من مجلد/UNC محلي، أو dashboard يعمل على `127.0.0.1` فقط).
- **الخصوصية:** بيراقب **نشاط الفأرة/الكيبوارد فقط** عبر Win32 `GetLastInputInfo`. **ما بيسجّلش** كلمات سر، ولا عناوين النوافذ، ولا روابط، ولا Clipboard، ولا صور شاشة.
- **الإصدار الحالي:** **1.3.8** — المصدر الوحيد للحقيقة هو `timetracker/config.py` → `APP_VERSION`. الملفات المبنية في `timetracker/dist/` مطابقة له.
- **اللغة:** الواجهة عربية/إنجليزية مع RTL كامل (Tkinter + pystray). أسماء أعمدة التقارير إنجليزية، وملفات CSV تُكتب UTF-8 **مع BOM** عشان Excel يفتح العربي صح.
- **البيئة:** Windows + Python 3.11. تبعيات مثبّتة في `timetracker/requirements.txt`: `pystray`, `Pillow`, `openpyxl`, `pyinstaller`.

---

## 2. هيكل المجلدات

```
Bussma_EA/                          ← جذر مساحة العمل
├── README.md                       ← هذا الملف
├── docs/
│   └── REMAINING_WORK.md           ← المرجع الوحيد للشغل المفتوح
├── scripts/
│   ├── build_installer.bat         ← اختصار: يروح timetracker/ وينادي البناء الحقيقي
│   └── upload_installer.bat        ← اختصار: يروح timetracker وينادي رفع الإصدار
└── timetracker/                    ← التطبيق نفسه (كل الكود هنا)
    ├── README.md                   ← المرجع التقني الكامل (ابدأ منه للتفاصيل)
    ├── main.py ... visualizer.py   ← وحدات الكود (17 ملف .py)
    ├── assets/                     ← أيقونات التراَ، الشعار، egypt_holidays.json
    ├── docs/
    │   ├── DECISIONS.md             ← سجل القرارات (9.2.1 = مفاتيح الإعدادات، 30 = اللوجينج، 31-33 = مراجعة أمنية/أداء، 34 = Timeline)
    │   ├── USER_GUIDE.md            ← دليل المستخدم النهائي (عربي)
    │   └── test_checklist.md        ← قائمة الاختبار اليدوي + قياسات أداء تاريخية
    ├── scripts/                    ← أدوات: set_version, smoke_test, benchmark, seed_fake_data, token_store, upload_release
    ├── tests/                      ← 27 ملف اختبار / 332 اختبار (pytest، أوفلاين بالكامل)
    ├── dist/                       ← المخرجات: TimeTracker.exe + TimeTracker_Setup.exe + version.json
    ├── build_installer.bat         ← سكربت البناء الحقيقي (PyInstaller + Inno Setup 6)
    ├── upload_installer.bat        ← رفع الإصدار على GitHub
    ├── run_dev.bat                 ← تشغيل وضع التطوير
    ├── TimeTracker.spec            ← إعداد PyInstaller
    ├── installer.iss               ← إعداد Inno Setup 6
    └── requirements.txt            ← الحزم المثبّتة
```

### ⚠️ قاعدة مهمة: لا تنقل ملفات الكود أو البناء من جذر `timetracker/`
السبب تقني بحت: `TimeTracker.spec` بيسمّي `main.py` و`assets/`؛ `installer.iss` بيحل المسارات بالنسبة لمكانه؛ `tray.py` و`egypt_holidays.py` بيحسبوا الأصول من `Path(__file__).parent / "assets"`؛ `autostart.py` من نفس المكان؛ `reporter.py` بيحسب الشعار من مسار الملف؛ و`scripts/*.py` بيفترضوا إن الجذر هو `parent.parent`. أي نقل يعني كسر البناء أو الاختبارات من غير ما يظهر خطأ واضح.

الكاشات (`__pycache__`, `.pytest_cache`, `build/`) **اتمسحت عمدًا** — بتترجع تلقائي لما تشغّل التطبيق أو الاختبارات.

---

## 3. أي ملف بيعمل إيه (داخل `timetracker/`)

| الملف | المسؤولية |
|---|---|
| `main.py` | نقطة الدخول: حلقة التراَ +flags: `--dashboard`/`-d`, `--verbose`, `--diagnostics` |
| `config.py` | `APP_VERSION` + الإعدادات المحفوظة في `%LOCALAPPDATA%\TimeTracker\config.json` |
| `database.py` | SQLite `data.db`، إصدار schema = 6، الـ migrations، النسخ الاحتياطي الذري |
| `calculator.py` | حسابات الوقت النقية: الـ grace period (180 ثانية)، تصنيفworked/overtime/break |
| `command_bus.py` | «حافل أوامر» بكاتب واحد لحالة الـ tracker القابلة للتغيير |
| `tracker.py` | محرك المراقبة: أحداث الإدخال، الجلسات، النبضات (heartbeats)، السكون والحضور |
| `dialogs.py` | نوافذ Tkinter (الإعدادات، Pause، Overtime، Meeting، Consent، About...) |
| `tray.py` | أيقونة `pystray` وقوائم التراَ + تحميل الأيقونات من `assets/` |
| `reporter.py` | تقارير Excel/CSV/PDF: Daily Summary, App Usage, Sessions, Timeline, Projects |
| `visualizer.py` | الداشبورد HTML + سيرفر HTTP اختياري على loopback فقط |
| `retention.py` | تجميع الأيام القديمة في `daily_summary`، الحذف حسب الإحتفاظ، تحليلات الأيام |
| `egypt_holidays.py` | محمّل أوفلاين لـ `assets/egypt_holidays.json` (العطل الرسمية المصرية) |
| `i18n.py` | النصوص الإنجليزية/العربية + أدوات RTL و bidi |
| `logging_setup.py` | لوجات دوّارة، التقاط الأعطال (`faulthandler`)، حزمة التشخيص |
| `updater.py` | تحديث تلقائي محلي/HTTPS مع تحقق SHA-256 وتثبيت صامت |
| `autostart.py` | تسجيل التطبيق في Startup ويندوز |
| `aggregate_reports.py` | أداة المدير: تجمع تقارير الموظفين في تقرير واحد |

---

## 4. تشغيل / اختبار / بناء / نشر

```cmd
cd timetracker

:: 1) تطوير
run_dev.bat
:: أو: pip install -r requirements.txt && python main.py
:: الداشبورد بس: python main.py --dashboard

:: 2) اختبارات (332 اختبار، أوفلاين)
python -m pytest tests -q

:: 3) بناء المُثبّت (PyInstaller + Inno Setup 6)
build_installer.bat
:: الناتج: dist\TimeTracker.exe + dist\TimeTracker_Setup.exe + dist\version.json

:: 4) نشر
upload_installer.bat
```

من الجذر تقدر تنادي `scripts\build_installer.bat` / `scripts\upload_installer.bat` — دول مجرد اختصارات بتروح `..\timetracker`.

**ترتيب البناء:** `set_version.py` بيقرأ/يضبط الإصدار من `config.py` ← PyInstaller بيعمل `TimeTracker.exe` من `TimeTracker.spec` (مع استبعاد مجلدات Java JDK من مسارات البحث لتجنب ضم DLLs غير لازمة) ← توقيع اختياري (يفضّل `SIGN_CERT_THUMBPRINT`) ← Inno Setup بيعمل `TimeTracker_Setup.exe` من `installer.iss` ← يتكتب `dist/version.json` بالـ SHA-256.

**للنشر للموظفين:** ابعت `dist/TimeTracker_Setup.exe` (مع `version.json` لو هتستخدم التحديث التلقائي) وخلي `update_source_path` يشاور على المجلد/UNC المشترك. المُثبّت بيتثبّت في مساحة المستخدم من غير Admin، وبيسجّل نفسه في Startup، وبيحفظ قاعدة البيانات والإعدادات وقت التحديث. بعد انتهاء التثبيت الصامت تظهر نافذة فيها «افتح البرنامج» أو «لاحقًا»؛ التطبيق لا يبدأ تلقائيًا أثناء التثبيت.

**أدوات إضافية في `scripts/`:** `smoke_test.py` (محاكاة أسبوع عمل كامل)، `benchmark.py` (قياسات الأداء)، `seed_fake_data.py` (بيانات تجريبية)، `set_version.py`، `token_store.py`، `upload_release.py`.

---

## 5. أين تعيش البيانات (مش في مجلد المشروع)

| البيانات | المكان |
|---|---|
| قاعدة البيانات | `%LOCALAPPDATA%\TimeTracker\data.db` |
| الإعدادات | `%LOCALAPPDATA%\TimeTracker\config.json` |
| النسخ الاحتياطية | `%USERPROFILE%\Documents\TimeTracker\Backups` |
| التقارير | `%USERPROFILE%\Documents\TimeTracker\Reports` |
| اللوجات | `%LOCALAPPDATA%\TimeTracker\timetracker*.log` |
| الداشبورد | `%LOCALAPPDATA%\TimeTracker\...` أو Temp حسب الإعداد |

> البيانات **ما بتخرجش من الجهاز**. الـ dashboard سيرفر محلي `127.0.0.1` بتوكن 128-bit، والتحديث التلقائي (opt-in) من مجلد محلي أو HTTPS.

---

## 6. الحالة الحالية (اتأكد منها 2026-11-18)

- الإصدار **1.3.8**، الاختبارات **332 passed**.
- `dist/` فيه النسخة المبنية + `version.json` (محفوظة زي ما طُلب).
- **مفتوح:** `PERF-02` (اتصال SQLite لكل عملية)، `QUAL-01` (دالة إعدادات عملاقة ~830 سطر)، `QUAL-02` (استثناءات مبتلعة)، `QUAL-03` (تكرار تجميع صفوف التقارير). التفاصيل في `docs/REMAINING_WORK.md`.
- **مطلوب من خارج الكود:** `SEC-01` — عمل Revoke للتوكن القديم على GitHub ومراجعة سجل النشاط (التوكن اتشال من الكود بالفعل).
- `MANUAL_QA.md` مذكور في ملاحظات الإصدارات القديمة لكنه **مش موجود** في مساحة العمل — فحوصات الـ GUI مش موثّقة حاليًا.
- أرقام الاختبارات/الأحجام داخل أقسام «What's changed» في `timetracker/README.md` **تاريخية** (بتوصف الإصدارات القديمة)، مش الحالة الحالية.
- مساحة العمل **مش repo جيت**، ومفيش `.git`.

---

## 7. ملاحظة للمساعد الذكي

لو هتسألني حاجة، ابدأ من `timetracker/README.md` (المرجع الكامل) ثم ارجع لـ `docs/REMAINING_WORK.md` للشغل المفتوح و`docs/DECISIONS.md` لسبب القرارات. متفترضش ملفات مش موجودة (زى `MANUAL_QA.md`)، ومتنقلش الملفات من جذر `timetracker/`، ومتحذفش `dist/version.json` لأن الـ updater بيحتاجه. ملفات العرض التنفيذي (`Bussma_EA_Executive_Presentation.pptx` ومولّدها `generate_presentation.py`) كانت في الجذر واختفت أثناء التنظيم — مش متوفرة حاليًا.

---

## Quick reference (English — for AI assistants)

- **Project:** `TimeTracker`, a local-first Windows system-tray app for employee work hours. Version **1.3.8** (`timetracker/config.py` → `APP_VERSION`).
- **Layout:** `timetracker/` = application + source + `assets/ docs/ scripts/ tests/ dist/`; root `docs/` = `REMAINING_WORK.md`; root `scripts/` = two `.bat` wrappers.
- **Never relocate** files at `timetracker/` root: PyInstaller spec, Inno Setup script, `__file__`-relative asset lookups, and bare-name test imports depend on that layout.
- **Commands (from `timetracker/`):** `run_dev.bat`, `python -m pytest tests -q` (**332 passed**), `build_installer.bat` → `dist/TimeTracker_Setup.exe` + `version.json`, `upload_installer.bat`.
- **Data:** `%LOCALAPPDATA%\TimeTracker\data.db` and `config.json`; backups/reports under `Documents\TimeTracker`. No telemetry; dashboard is loopback-only.
- **Open work:** PERF-02, QUAL-01/02/03 in `docs/REMAINING_WORK.md`; SEC-01 = revoke old GitHub token externally.
- **Caveats:** `MANUAL_QA.md` referenced but missing; changelog numbers are historical; no git repo; generated caches intentionally removed; keep `dist/version.json`.
