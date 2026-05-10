from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from supabase import create_client, Client
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
import os
import shutil
import img2pdf
import ocrmypdf
import fitz
import requests
from dotenv import load_dotenv
import time

# =========================
# LOAD ENV
# =========================
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# FASTAPI
# =========================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://pct-ats.nanohub.page"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "OCR API RUNNING"}

# =========================
# DIRECTORIES
# =========================
UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================
# HELPERS
# =========================
def clean_text(text: str) -> str:
    return " ".join(text.split())

def update_job(batch_id, data):
    try:
        data["updated_at"] = datetime.utcnow().isoformat()
        if data.get("status") in ["completed", "failed"]:
            data["mysql_synced"] = False
        supabase.table("ocr_jobs").update(data).eq("batch_id", batch_id).execute()
    except Exception as e:
        print("UPDATE JOB ERROR:", e)

def trigger_mysql_sync(batch_id, title, status, message="", pdf_url=None, ocr_text=None):
    url = "https://pct-ats.nanohub.page/sync_ocr_jobs.php"
    payload = {
        "batch_id": batch_id,
        "title": title,
        "status": status,
        "message": message,
        "pdf_url": pdf_url,
        "ocr_text": ocr_text
    }
    print("TRIGGERING MYSQL SYNC WITH PAYLOAD:", payload)
    for attempt in range(2):  # retry once if fails
        try:
            requests.post(url, json=payload, timeout=10, verify=False)
            print("WEBHOOK SENT")
            break
        except requests.exceptions.RequestException as e:
            print(f"WEBHOOK ERROR ATTEMPT {attempt+1}:", e)
            time.sleep(1)

def get_next_page_number(folder):
    existing = [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    numbers = []
    for file in existing:
        try:
            name = os.path.splitext(file)[0]
            if name.startswith("page_"):
                numbers.append(int(name.replace("page_", "")))
        except: pass
    return max(numbers) + 1 if numbers else 1

# =========================
# OCR PROCESS
# =========================
def process_ocr(batch_folder, batch_id, title):
    temp_pdf_path = None
    pdf_path = None
    try:
        print(f"\nSTARTING OCR PROCESS: {batch_id}")
        all_images = sorted(
            [os.path.join(batch_folder, f) for f in os.listdir(batch_folder)
             if f.lower().endswith((".jpg", ".jpeg", ".png"))],
            key=lambda x: os.path.basename(x)
        )
        if not all_images:
            print("NO IMAGES FOUND:", batch_id)
            update_job(batch_id, {"status": "failed", "message": "No uploaded images found"})
            trigger_mysql_sync(batch_id, title, "failed", "No uploaded images found")
            return
        print(f"TOTAL IMAGES: {len(all_images)}")
        # TEMP PDF
        update_job(batch_id, {"progress": 20, "message": "Creating PDF from images"})
        temp_pdf_path = os.path.join(OUTPUT_DIR, f"{batch_id}_temp.pdf")
        with open(temp_pdf_path, "wb") as f:
            f.write(img2pdf.convert(all_images))
        print("TEMP PDF CREATED")
        # FINAL PDF
        pdf_path = os.path.join(OUTPUT_DIR, f"{batch_id}.pdf")
        update_job(batch_id, {"progress": 40, "message": "Generating searchable PDF"})
        ocrmypdf.ocr(temp_pdf_path, pdf_path, force_ocr=True, optimize=0, language="eng", jobs=2)
        print("SEARCHABLE OCR PDF CREATED")
        # EXTRACT TEXT
        update_job(batch_id, {"progress": 75, "message": "Extracting searchable text"})
        combined_text = ""
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        for index, page in enumerate(doc):
            cleaned_text = clean_text(page.get_text())
            combined_text += cleaned_text + "\n\n"
            try:
                supabase.table("ocr_pages").insert({
                    "batch_id": batch_id,
                    "page_number": index + 1,
                    "extracted_text": cleaned_text
                }).execute()
            except Exception as db_error:
                print("DB OCR PAGE ERROR:", db_error)
            percent = int(75 + ((index + 1)/total_pages)*10)
            update_job(batch_id, {"progress": percent, "message": f"Extracting text page {index+1} of {total_pages}"})
        doc.close()
        if not combined_text.strip():
            raise Exception("No searchable text extracted from PDF")
        # UPLOAD PDF
        update_job(batch_id, {"progress": 90, "message": "Uploading PDF"})
        pdf_storage_path = f"{batch_id}/final.pdf"
        with open(pdf_path, "rb") as f:
            supabase.storage.from_("pct-ocr-pdfs").upload(path=pdf_storage_path, file=f, file_options={"content-type": "application/pdf", "upsert": "true"})
        pdf_url = supabase.storage.from_("pct-ocr-pdfs").get_public_url(pdf_storage_path)
        print("PUBLIC URL:", pdf_url)
        # UPDATE JOB
        update_job(batch_id, {"status": "completed", "progress": 100, "message": "OCR completed", "pdf_url": pdf_url, "ocr_text": combined_text})
        # TRIGGER PHP SYNC
        trigger_mysql_sync(batch_id, title, "completed", message="OCR completed", pdf_url=pdf_url, ocr_text=combined_text)
        print("OCR COMPLETED:", batch_id)
    except Exception as e:
        print("BACKGROUND OCR ERROR:", e)
        update_job(batch_id, {"status": "failed", "message": str(e)})
        trigger_mysql_sync(batch_id, title, "failed", message=str(e))
    finally:
        # CLEANUP
        try: shutil.rmtree(batch_folder)
        except Exception as e: print("BATCH CLEANUP ERROR:", e)
        try: os.remove(temp_pdf_path) if temp_pdf_path and os.path.exists(temp_pdf_path) else None
        except Exception as e: print("TEMP PDF CLEANUP ERROR:", e)
        print("BACKGROUND TASK FINISHED")

# =========================
# MAIN API
# =========================
@app.post("/process-images")
async def process_images(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        title = form.get("title")
        batch_id = form.get("batch_id")
        is_final = int(form.get("is_final", 0))
        if not batch_id:
            return JSONResponse({"success": False, "message": "Missing batch_id"}, status_code=400)
        images = [v for k,v in form.multi_items() if k=="images"]
        if not images:
            return JSONResponse({"success": False, "message": "No images received"}, status_code=400)
        batch_folder = os.path.join(UPLOAD_DIR, batch_id)
        os.makedirs(batch_folder, exist_ok=True)
        page_number = get_next_page_number(batch_folder)
        for img in images:
            extension = os.path.splitext(img.filename)[1] or ".jpg"
            path = os.path.join(batch_folder, f"page_{page_number:04d}{extension}")
            with open(path, "wb") as buffer:
                shutil.copyfileobj(img.file, buffer)
            page_number += 1
        supabase.table("ocr_jobs").upsert({"batch_id": batch_id, "status":"processing", "progress":5, "message":"Receiving images","mysql_synced":False}).execute()
        if not is_final:
            return JSONResponse({"success": True, "message": "Batch uploaded"})
        update_job(batch_id, {"status":"processing","progress":15,"message":"Starting OCR processing"})
        background_tasks.add_task(process_ocr, batch_folder, batch_id, title)
        return JSONResponse({"success": True, "message": "OCR processing started"})
    except Exception as e:
        print("UPLOAD ERROR:", e)
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)