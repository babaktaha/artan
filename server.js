import express from 'express';
import multer from 'multer';
import path from 'path';
import fs from 'fs';
import cors from 'cors';
import dotenv from 'dotenv';

dotenv.config();

const app = express();
app.use(cors());
app.use(express.json());

const uploadDir = path.resolve('./uploads');
if (!fs.existsSync(uploadDir)) {
	fs.mkdirSync(uploadDir, { recursive: true });
}

const storage = multer.diskStorage({
	destination: (_req, _file, cb) => {
		cb(null, uploadDir);
	},
	filename: (_req, file, cb) => {
		const timestamp = Date.now();
		const sanitizedOriginal = file.originalname.replace(/[^a-zA-Z0-9_.-]/g, '_');
		cb(null, `${timestamp}-${sanitizedOriginal}`);
	},
});

const pdfFileFilter = (_req, file, cb) => {
	const isPdf = file.mimetype === 'application/pdf' || file.originalname.toLowerCase().endsWith('.pdf');
	if (!isPdf) {
		return cb(new Error('Only PDF files are allowed'));
	}
	cb(null, true);
};

const upload = multer({
	storage,
	fileFilter: pdfFileFilter,
	limits: { fileSize: 20 * 1024 * 1024 }, // 20MB
});

// Static hosting for uploads and public html
app.use('/uploads', express.static(uploadDir));
app.use('/', express.static(path.resolve('./public')));

app.post('/api/upload', upload.single('file'), (req, res) => {
	if (!req.file) {
		return res.status(400).json({ error: 'No file uploaded' });
	}
	const { filename, mimetype, size } = req.file;
	return res.status(201).json({
		message: 'PDF uploaded successfully',
		file: {
			filename,
			url: `/uploads/${filename}`,
			mimetype,
			size,
		},
	});
});

app.use((err, _req, res, _next) => {
	// Multer and other errors
	const status = err.message?.includes('PDF') || err.code === 'LIMIT_FILE_SIZE' ? 400 : 500;
	res.status(status).json({ error: err.message || 'Server error' });
});

const port = process.env.PORT ? Number(process.env.PORT) : 3000;
app.listen(port, () => {
	console.log(`Server running on http://localhost:${port}`);
});