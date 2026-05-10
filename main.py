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

# =========================
# LOAD ENV
# =========================

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

# =========================
# FASTAPI
# =========================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pct-ats.nanohub.page"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {
        "status": "OCR API RUNNING"
    }

# =========================
# DIRECTORIES
# =========================

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================
# CLEAN OCR TEXT
# =========================

def clean_text(text: str) -> str:
    return " ".join(text.split())

# =========================
# UPDATE OCR JOB
# =========================

def update_job(batch_id, data):

    try:

        data["updated_at"] = datetime.utcnow().isoformat()

        if (
            data.get("status") == "completed" or
            data.get("status") == "failed"
        ):
            data["mysql_synced"] = False

        supabase.table("ocr_jobs").update(
            data
        ).eq(
            "batch_id",
            batch_id
        ).execute()

    except Exception as e:

        print("UPDATE JOB ERROR:", e)

# =========================
# TRIGGER PHP MYSQL SYNC
# =========================

def trigger_mysql_sync(batch_id):

    try:

        sync_url = (
            "https://pct-ats.nanohub.page/"
            f"sync_ocr_jobs.php?batch_id={batch_id}"
        )

        response = requests.get(
            sync_url,
            timeout=60,
            verify=False
        )

        print("MYSQL SYNC STATUS:", response.status_code)
        print("MYSQL SYNC RESPONSE:", response.text)

    except Exception as e:

        print("MYSQL SYNC ERROR:", e)

# =========================
# GET NEXT PAGE NUMBER
# =========================

def get_next_page_number(folder):

    existing = [
        f for f in os.listdir(folder)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ]

    numbers = []

    for file in existing:

        try:

            name = os.path.splitext(file)[0]

            if name.startswith("page_"):

                num = int(name.replace("page_", ""))

                numbers.append(num)

        except:
            pass

    if not numbers:
        return 1

    return max(numbers) + 1

# =========================
# BACKGROUND OCR PROCESS
# =========================

def process_ocr(batch_folder, batch_id):

    temp_pdf_path = None
    pdf_path = None

    try:

        print(f"\nSTARTING OCR PROCESS: {batch_id}")

        # =========================
        # GET ALL IMAGES
        # =========================

        all_images = sorted(
            [
                os.path.join(batch_folder, f)
                for f in os.listdir(batch_folder)
                if f.lower().endswith((".jpg", ".jpeg", ".png"))
            ],
            key=lambda x: os.path.basename(x)
        )

        if not all_images:

            print("NO IMAGES FOUND:", batch_id)

            update_job(batch_id, {
                "status": "failed",
                "message": "No uploaded images found"
            })

            return

        print(f"TOTAL IMAGES: {len(all_images)}")

        # =========================
        # CREATE TEMP PDF
        # =========================

        update_job(batch_id, {
            "progress": 20,
            "message": "Creating PDF from images"
        })

        temp_pdf_path = os.path.join(
            OUTPUT_DIR,
            f"{batch_id}_temp.pdf"
        )

        with open(temp_pdf_path, "wb") as f:

            f.write(
                img2pdf.convert(all_images)
            )

        print("TEMP PDF CREATED")

        # =========================
        # FINAL SEARCHABLE PDF
        # =========================

        pdf_path = os.path.join(
            OUTPUT_DIR,
            f"{batch_id}.pdf"
        )

        update_job(batch_id, {
            "progress": 40,
            "message": "Generating searchable PDF"
        })

        print("STARTING OCRMY PDF...")

        ocrmypdf.ocr(
            temp_pdf_path,
            pdf_path,

            force_ocr=True,

            optimize=0,

            language="eng",

            jobs=2,

        )
        print("OCRMY PDF FINISHED")
        print("SEARCHABLE OCR PDF CREATED")

        # =========================
        # EXTRACT TEXT FROM PDF
        # =========================

        update_job(batch_id, {
            "progress": 75,
            "message": "Extracting searchable text"
        })

        combined_text = ""

        try:

            doc = fitz.open(pdf_path)

            total_pages = len(doc)

            print(f"TOTAL PDF PAGES: {total_pages}")

            for index, page in enumerate(doc):

                text = page.get_text()

                cleaned_text = clean_text(text)

                combined_text += cleaned_text + "\n\n"

                # =========================
                # SAVE PAGE OCR
                # =========================

                try:

                    supabase.table("ocr_pages").insert({
                        "batch_id": batch_id,
                        "page_number": index + 1,
                        "extracted_text": cleaned_text
                    }).execute()

                except Exception as db_error:

                    print("DB OCR PAGE ERROR:", db_error)

                percent = int(
                    75 + ((index + 1) / total_pages) * 10
                )

                update_job(batch_id, {
                    "progress": percent,
                    "message": f"Extracting text page {index + 1} of {total_pages}"
                })

            doc.close()

        except Exception as e:

            print("PDF TEXT EXTRACTION ERROR:", e)

            raise Exception(
                "Failed extracting searchable text"
            )

        # =========================
        # VALIDATE TEXT
        # =========================

        if not combined_text.strip():

            raise Exception(
                "No searchable text extracted from PDF"
            )

        # =========================
        # UPLOAD PDF TO SUPABASE
        # =========================

        update_job(batch_id, {
            "progress": 90,
            "message": "Uploading PDF"
        })

        pdf_storage_path = f"{batch_id}/final.pdf"

        with open(pdf_path, "rb") as f:

            supabase.storage.from_("pct-ocr-pdfs").upload(
                path=pdf_storage_path,
                file=f,
                file_options={
                    "content-type": "application/pdf",
                    "upsert": "true"
                }
            )

        print("UPLOADED TO SUPABASE")

        pdf_url = supabase.storage.from_(
            "pct-ocr-pdfs"
        ).get_public_url(pdf_storage_path)

        print("PUBLIC URL:", pdf_url)

        # =========================
        # SAVE OCR RESULT
        # =========================

        update_job(batch_id, {
            "status": "completed",
            "progress": 100,
            "message": "OCR completed",
            "pdf_url": pdf_url,
            "ocr_text": combined_text
        })

        # =========================
        # TRIGGER MYSQL SYNC
        # =========================

        trigger_mysql_sync(batch_id)

        print("OCR COMPLETED:", batch_id)

    except Exception as e:

        print("BACKGROUND OCR ERROR:", e)

        update_job(batch_id, {
            "status": "failed",
            "message": str(e)
        })

        # =========================
        # TRIGGER MYSQL SYNC
        # =========================

        trigger_mysql_sync(batch_id)

    finally:

        # =========================
        # CLEANUP
        # =========================

        try:

            if os.path.exists(batch_folder):
                shutil.rmtree(batch_folder)

        except Exception as e:

            print("BATCH CLEANUP ERROR:", e)

        try:

            if temp_pdf_path and os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)

        except Exception as e:

            print("TEMP PDF CLEANUP ERROR:", e)

        print("BACKGROUND TASK FINISHED")

# =========================
# MAIN API
# =========================

@app.post("/process-images")
async def process_images(
    request: Request,
    background_tasks: BackgroundTasks
):

    try:

        # =========================
        # READ FORM DATA
        # =========================

        form = await request.form()

        print("\n========== RAW FORM ==========")

        for key, value in form.multi_items():

            print("KEY:", key)
            print("TYPE:", type(value))
            print("-------------------")

        # =========================
        # BASIC FIELDS
        # =========================

        title = form.get("title")
        batch_id = form.get("batch_id")
        is_final = int(form.get("is_final", 0))

        print("TITLE:", title)
        print("BATCH:", batch_id)
        print("IS FINAL:", is_final)

        # =========================
        # VALIDATION
        # =========================

        if not batch_id:

            return JSONResponse({
                "success": False,
                "message": "Missing batch_id"
            }, status_code=400)

        # =========================
        # EXTRACT IMAGES
        # =========================

        images = []

        for key, value in form.multi_items():

            if key == "images":

                images.append(value)

        print("TOTAL IMAGES:", len(images))

        if len(images) == 0:

            return JSONResponse({
                "success": False,
                "message": "No images received"
            }, status_code=400)

        # =========================
        # CREATE BATCH FOLDER
        # =========================

        batch_folder = os.path.join(
            UPLOAD_DIR,
            batch_id
        )

        os.makedirs(
            batch_folder,
            exist_ok=True
        )

        # =========================
        # PAGE NUMBERING
        # =========================

        page_number = get_next_page_number(
            batch_folder
        )

        # =========================
        # SAVE IMAGES
        # =========================

        for img in images:

            print("PROCESSING:", img.filename)

            extension = os.path.splitext(
                img.filename
            )[1]

            if not extension:
                extension = ".jpg"

            filename = f"page_{page_number:04d}{extension}"

            path = os.path.join(
                batch_folder,
                filename
            )

            with open(path, "wb") as buffer:

                shutil.copyfileobj(
                    img.file,
                    buffer
                )

            print("SAVED:", path)

            page_number += 1

        # =========================
        # CREATE / UPDATE JOB
        # =========================

        supabase.table("ocr_jobs").upsert({
            "batch_id": batch_id,
            "status": "processing",
            "progress": 5,
            "message": "Receiving images",
            "mysql_synced": False
        }).execute()

        # =========================
        # NOT FINAL YET
        # =========================

        if not is_final:

            return JSONResponse({
                "success": True,
                "message": "Batch uploaded"
            })

        # =========================
        # START OCR
        # =========================

        update_job(batch_id, {
            "status": "processing",
            "progress": 15,
            "message": "Starting OCR processing"
        })

        background_tasks.add_task(
            process_ocr,
            batch_folder,
            batch_id
        )

        # =========================
        # SUCCESS
        # =========================

        return JSONResponse({
            "success": True,
            "message": "OCR processing started"
        })

    except Exception as e:

        print("\nUPLOAD ERROR:")
        print(str(e))

        return JSONResponse({
            "success": False,
            "message": str(e)
        }, status_code=500)