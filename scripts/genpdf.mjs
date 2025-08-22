import { PDFDocument, StandardFonts, rgb } from "pdf-lib";
import fs from "fs";

const run = async () => {
	const pdfDoc = await PDFDocument.create();
	const page = pdfDoc.addPage([595, 842]);
	const font = await pdfDoc.embedFont(StandardFonts.Helvetica);
	page.drawText("Hello PDF", { x: 50, y: 800, size: 24, font, color: rgb(0, 0, 0) });
	const bytes = await pdfDoc.save();
	fs.writeFileSync("/tmp/valid.pdf", Buffer.from(bytes));
	console.log("/tmp/valid.pdf");
};
run();
