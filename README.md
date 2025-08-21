# آپلود PDF

- نصب وابستگی‌ها:

```bash
npm install
```

- اجرا:

```bash
npm start
```

- آدرس صفحه ساده آپلود:

- `http://localhost:3000/`

- API آپلود:

- `POST /api/upload` با فیلد فایل `file` (multipart/form-data)

- فایل‌های آپلود شده از `http://localhost:3000/uploads/...` قابل دسترسی هستند.