import os
import redis
from dotenv import load_dotenv

load_dotenv()
r = redis.from_url(os.getenv("REDIS_URL"))

confirm = input("❗ هل أنت متأكد من حذف كل بيانات Redis؟ (نعم/لا): ").strip().lower()
if confirm == "نعم":
    deleted = r.flushdb()
    print("✅ تم حذف جميع مفاتيح Redis.")
else:
    print("⛔ تم الإلغاء.")