"""
setup_points_db.py
------------------
يُنشئ قاعدة البيانات web_point.db مع جدول points والنقاط الافتراضية.
شغّله مرة واحدة قبل تشغيل البرنامج الأساسي.

استخدام:
    python setup_points_db.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_point.db")


def setup():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # إنشاء الجدول لو مش موجود
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS points (
            name TEXT PRIMARY KEY,
            j1   REAL NOT NULL DEFAULT 0.0,
            j2   REAL NOT NULL DEFAULT 0.0,
            j3   REAL NOT NULL DEFAULT 0.0,
            j4   REAL NOT NULL DEFAULT 0.0,
            j5   REAL NOT NULL DEFAULT 0.0,
            j6   REAL NOT NULL DEFAULT 0.0
        )
    """)

    # نقاط افتراضية — غيّر القيم حسب موقع الروبوت الحقيقي
    default_points = [
        ("cam",    0.0, 0.0,  0.0, 0.0, 0.0, 0.0),  # موضع قراءة الباركود
        ("water1", 0.0, 0.0,  0.0, 0.0, 0.0, 0.0),  # نقطة homing للبرنامج 1
        ("10kg_1", 0.0, 0.0,  0.0, 0.0, 0.0, 0.0),
        ("10kg_2", 0.0, 0.0,  0.0, 0.0, 0.0, 0.0),
        ("10kg_3", 0.0, 0.0,  0.0, 0.0, 0.0, 0.0),
        ("10kg_4", 0.0, 0.0,  0.0, 0.0, 0.0, 0.0),
        ("10kg_5", 0.0, 0.0,  0.0, 0.0, 0.0, 0.0),
    ]

    cursor.executemany(
        "INSERT OR IGNORE INTO points (name, j1, j2, j3, j4, j5, j6) VALUES (?,?,?,?,?,?,?)",
        default_points,
    )
    conn.commit()

    # عرض محتوى الجدول
    cursor.execute("SELECT * FROM points ORDER BY name")
    rows = cursor.fetchall()
    conn.close()

    print(f"✅ Database ready: {DB_PATH}")
    print(f"{'Name':<12} {'j1':>8} {'j2':>8} {'j3':>8} {'j4':>8} {'j5':>8} {'j6':>8}")
    print("-" * 68)
    for row in rows:
        print(f"{row[0]:<12} {row[1]:>8.3f} {row[2]:>8.3f} {row[3]:>8.3f} {row[4]:>8.3f} {row[5]:>8.3f} {row[6]:>8.3f}")

    print()
    print("⚠️  القيم الافتراضية كلها 0.0 — عدّل كل نقطة بالزوايا الحقيقية للروبوت")
    print("    يمكن التعديل مباشرة بـ DB Browser for SQLite أو بالـ web dashboard")


if __name__ == "__main__":
    setup()
