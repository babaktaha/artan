# آپلود PDF

- نصب وابستگی‌ها:

```bash
npm install
```

- اجرا:

```bash
npm start
```

- صفحات:
- آپلود: `http://localhost:3000/`
- ویرایش: `http://localhost:3000/modify.html`

- API ها:

- آپلود: `POST /api/upload` با فیلد فایل `file` (multipart/form-data)
- ویرایش: `POST /api/pdf/modify`
  - پارامترها (multipart/form-data یا x-www-form-urlencoded):
    - `action`: یکی از `watermark`، `rotate`، `extract`
    - `pages`: مثل `all` یا `1,3-5`
    - برای `watermark`: `text`، `size`، `angle`
    - برای `rotate`: `degrees`
    - منبع: یکی از `file` (آپلود جدید) یا `filename` (نام فایل موجود در uploads)

- فایل‌های آپلود شده از `http://localhost:3000/uploads/...` قابل دسترسی هستند.