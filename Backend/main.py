import io, re, os
from pathlib import Path
from datetime import datetime
import sqlite3

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import pytesseract
from PIL import Image
import PyPDF2
import aiofiles

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

# FastAPI 
app = FastAPI(title="Document Intelligence MVP")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Storage / DB 
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

engine = create_engine("sqlite:///documents.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    content_type = Column(String)
    text = Column(Text)
    invoice_number = Column(String, nullable=True)
    amount = Column(String, nullable=True)
    email = Column(String, nullable=True)
    date = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Helpers
def extract_fields(text: str) -> dict:
    fields = {"invoice_number": None, "amount": None, "email": None, "date": None}

    m = re.search(r"(invoice\s*(?:no|#|number)?\s*[:\-]?\s*)([A-Za-z0-9\-_/]+)", text, re.I)
    if m: fields["invoice_number"] = m.group(2)

    m = re.search(r"(?<!\d)(?:USD|INR|Rs\.?|₹|\$)?\s?([0-9]{1,3}(?:[, ]?[0-9]{3})*(?:\.[0-9]{1,2})?)", text)
    if m: fields["amount"] = m.group(0).strip()

    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if m: fields["email"] = m.group(0)

    m = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b", text)
    if m: fields["date"] = m.group(1)

    return fields

def ocr_image_bytes(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return pytesseract.image_to_string(img)

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    # trying to pick selectable text first
    # in case it fails then it uses OCR
   
    text = ""
    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    for page in reader.pages:
        page_text = page.extract_text() or ""
        text += page_text + "\n"

    text = text.strip()

    # If it's a scanned PDF (no text), try OCR via pdf2image if available
    if not text:
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(pdf_bytes, dpi=300)
            ocr_text = []
            for img in images:
                ocr_text.append(pytesseract.image_to_string(img))
            text = "\n".join(ocr_text).strip()
        except Exception:
            # pdf2image or poppler not available then it returns empty
            text = ""
    return text

# Routes (UI)
@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str | None = None):
    db = SessionLocal()
    if q:
        docs = db.query(Document).filter(Document.text.contains(q)).order_by(Document.created_at.desc()).all()
    else:
        docs = db.query(Document).order_by(Document.created_at.desc()).all()
    db.close()
    return templates.TemplateResponse("index.html", {"request": request, "docs": docs, "q": q or ""})

@app.get("/document/{doc_id}", response_class=HTMLResponse)
def view_document(request: Request, doc_id: int):
    db = SessionLocal()
    doc = db.query(Document).get(doc_id)
    db.close()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return templates.TemplateResponse("document.html", {"request": request, "doc": doc})

# API
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    filename = file.filename
    content_type = file.content_type or ""

    # Save the original file
    safe_name = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}_{filename.replace(' ', '_')}"
    save_path = UPLOAD_DIR / safe_name
    async with aiofiles.open(save_path, "wb") as f:
        await f.write(content)

    # Extract text
    extracted_text = ""
    if content_type in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp", "image/tiff"):
        extracted_text = ocr_image_bytes(content)
    elif content_type == "application/pdf":
        extracted_text = extract_text_from_pdf(content)
    else:
        # last resort: try as image
        try:
            extracted_text = ocr_image_bytes(content)
        except Exception:
            return JSONResponse({"error": f"Unsupported file type: {content_type}"}, status_code=400)

    extracted_text = (extracted_text or "").strip()

    fields = extract_fields(extracted_text)

    # Persist
    db = SessionLocal()
    doc = Document(
        filename=safe_name,
        content_type=content_type,
        text=extracted_text,
        invoice_number=fields["invoice_number"],
        amount=fields["amount"],
        email=fields["email"],
        date=fields["date"],
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    db.close()

    return {"ok": True, "id": doc.id}

# Simple JSON list (for testing and integrating later)
@app.get("/api/documents")
def api_documents():
    db = SessionLocal()
    docs = db.query(Document).order_by(Document.created_at.desc()).all()
    out = [{
        "id": d.id,
        "filename": d.filename,
        "invoice_number": d.invoice_number,
        "amount": d.amount,
        "email": d.email,
        "date": d.date,
        "created_at": d.created_at.isoformat()
    } for d in docs]
    db.close()
    return out
@app.get("/all_docs")
def all_docs():
    conn = sqlite3.connect("documents.db")
    c = conn.cursor()
    c.execute("SELECT id, text FROM documents")
    rows = c.fetchall()
    conn.close()
    return {"docs": rows}
