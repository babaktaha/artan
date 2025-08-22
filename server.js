import express from 'express';
import multer from 'multer';
import path from 'path';
import fs from 'fs';
import cors from 'cors';
import dotenv from 'dotenv';
import { PDFDocument, degrees, rgb, StandardFonts } from 'pdf-lib';

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

// Helpers
const parsePagesParam = (pagesParam, totalPages) => {
	if (!pagesParam || pagesParam === 'all') {
		return Array.from({ length: totalPages }, (_v, i) => i);
	}
	const selections = String(pagesParam).split(',').map(s => s.trim()).filter(Boolean);
	const result = new Set();
	for (const sel of selections) {
		if (sel.includes('-')) {
			const [startStr, endStr] = sel.split('-');
			let start = parseInt(startStr, 10) - 1;
			let end = parseInt(endStr, 10) - 1;
			if (Number.isNaN(start) || Number.isNaN(end)) continue;
			if (start > end) [start, end] = [end, start];
			start = Math.max(0, start);
			end = Math.min(totalPages - 1, end);
			for (let i = start; i <= end; i += 1) result.add(i);
		} else {
			const idx = parseInt(sel, 10) - 1;
			if (!Number.isNaN(idx) && idx >= 0 && idx < totalPages) result.add(idx);
		}
	}
	return Array.from(result.values()).sort((a, b) => a - b);
};

app.post('/api/pdf/modify', upload.single('file'), async (req, res, next) => {
	try {
		const action = (req.body.action || '').toLowerCase();
		if (!action) {
			return res.status(400).json({ error: 'action is required (watermark|rotate|extract)' });
		}

		let sourceBuffer;
		let sourceName = 'input.pdf';
		if (req.file) {
			sourceBuffer = fs.readFileSync(req.file.path);
			sourceName = req.file.originalname || req.file.filename || sourceName;
		} else if (req.body.filename) {
			const safeName = String(req.body.filename).replace(/[^a-zA-Z0-9_.-]/g, '_');
			const fullPath = path.join(uploadDir, safeName);
			if (!fs.existsSync(fullPath)) {
				return res.status(404).json({ error: 'Source file not found' });
			}
			sourceBuffer = fs.readFileSync(fullPath);
			sourceName = safeName;
		} else {
			return res.status(400).json({ error: 'Provide a PDF via multipart field "file" or an existing "filename"' });
		}

		let pdfDoc = await PDFDocument.load(sourceBuffer);
		const totalPages = pdfDoc.getPageCount();

		if (action === 'watermark') {
			const text = req.body.text || 'WATERMARK';
			const size = req.body.size ? Number(req.body.size) : 48;
			const angle = req.body.angle ? Number(req.body.angle) : 45;
			const pages = parsePagesParam(req.body.pages, totalPages);
			const font = await pdfDoc.embedFont(StandardFonts.HelveticaBold);
			for (const pageIndex of pages) {
				const page = pdfDoc.getPage(pageIndex);
				const { width, height } = page.getSize();
				const textWidth = font.widthOfTextAtSize(text, size);
				const x = (width - textWidth) / 2;
				const y = height / 2;
				page.drawText(text, {
					x,
					y,
					size,
					font,
					color: rgb(0.7, 0.7, 0.7),
					rotate: degrees(angle),
				});
			}
		} else if (action === 'rotate') {
			const angle = req.body.degrees ? Number(req.body.degrees) : 90;
			const pages = parsePagesParam(req.body.pages, totalPages);
			for (const pageIndex of pages) {
				const page = pdfDoc.getPage(pageIndex);
				page.setRotation(degrees(angle));
			}
		} else if (action === 'extract') {
			const pages = parsePagesParam(req.body.pages, totalPages);
			const newDoc = await PDFDocument.create();
			const copiedPages = await newDoc.copyPages(pdfDoc, pages);
			for (const p of copiedPages) newDoc.addPage(p);
			pdfDoc = newDoc;
		} else {
			return res.status(400).json({ error: 'Unsupported action. Use watermark|rotate|extract' });
		}

		const bytes = await pdfDoc.save();
		const baseName = path.basename(sourceName, path.extname(sourceName));
		const outName = `${Date.now()}-${baseName}-modified.pdf`;
		const outPath = path.join(uploadDir, outName);
		fs.writeFileSync(outPath, Buffer.from(bytes));

		return res.status(200).json({
			message: 'PDF modified successfully',
			file: {
				filename: outName,
				url: `/uploads/${outName}`,
				mimetype: 'application/pdf',
			}
		});
	} catch (err) {
		next(err);
	}
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