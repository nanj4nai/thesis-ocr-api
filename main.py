from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from typing import List
from supabase import create_client, Client
from fastapi import Request

import os
import shutil
import pytesseract
import img2pdf
import ocrmypdf

from PIL import Image
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

if os.name == "nt":

    pytesseract.pytesseract.tesseract_cmd = (
        r"C:\Users\enna\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
    )

app = FastAPI()

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# =========================
# CLEAN OCR TEXT
# =========================

def clean_text(text):
    return " ".join(text.split())


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

# =========================
# BACKGROUND OCR PROCESS
# =========================

def process_ocr(batch_folder, batch_id):

    try:

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

            return

        combined_text = ""

        total_images = len(all_images)

        # =========================
        # OCR TEXT EXTRACTION
        # =========================

        for index, image_path in enumerate(all_images):

            try:

                with Image.open(image_path) as img:

                    image = img.convert("RGB")

                    text = pytesseract.image_to_string(
                        image,
                        lang="eng"
                    )

                cleaned_text = clean_text(text)

                combined_text += cleaned_text + "\n\n"

                # OPTIONAL:
                # STORE PAGE OCR TEXT
                try:

                    supabase.table("ocr_pages").insert({
                        "batch_id": batch_id,
                        "page_number": index + 1,
                        "extracted_text": cleaned_text
                    }).execute()

                except Exception as db_error:

                    print("DB OCR PAGE ERROR:", db_error)

                percent = int(
                    10 + ((index + 1) / total_images) * 60
                )

                print(
                    f"OCR {batch_id}: "
                    f"{index + 1}/{total_images} "
                    f"({percent}%)"
                )

                supabase.table("ocr_jobs").update({
                    "progress": percent,
                    "message": f"OCR page {index + 1} of {total_images}"
                }).eq("batch_id", batch_id).execute()

            except Exception as e:

                print("OCR ERROR:", e)

        # =========================
        # CREATE TEMP PDF
        # =========================

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

        supabase.table("ocr_jobs").update({
            "progress": 80,
            "message": "Generating searchable PDF"
        }).eq("batch_id", batch_id).execute()

        # =========================
        # OCR SEARCHABLE PDF
        # =========================

        ocrmypdf.ocr(
            input_file=temp_pdf_path,
            output_file=pdf_path,

            force_ocr=True,
            skip_text=True,

            deskew=True,
            optimize=3,

            language="eng",

            jobs=4
        )

        print("SEARCHABLE OCR PDF CREATED")

        # =========================
        # UPLOAD PDF TO SUPABASE
        # =========================

        supabase.table("ocr_jobs").update({
            "progress": 90,
            "message": "Uploading PDF"
        }).eq("batch_id", batch_id).execute()

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
        # SAVE OCR TEXT
        # =========================

        supabase.table("ocr_jobs").update({
            "status": "completed",
            "progress": 100,
            "message": "OCR completed",
            "pdf_url": pdf_url,
            "ocr_text": combined_text
        }).eq("batch_id", batch_id).execute()

        # =========================
        # CLEANUP
        # =========================

        try:

            shutil.rmtree(batch_folder)

        except Exception as e:

            print("CLEANUP ERROR:", e)

        try:

            os.remove(temp_pdf_path)

        except:
            pass

        print("OCR COMPLETED:", batch_id)

    except Exception as e:

        supabase.table("ocr_jobs").update({
            "status": "failed",
            "message": str(e)
        }).eq("batch_id", batch_id).execute()

        print("BACKGROUND OCR ERROR:", e)


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
        # EXTRACT IMAGES
        # =========================

        images = []

        for key, value in form.multi_items():

            if key.startswith("images["):

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

        # ALWAYS CREATE/UPDATE JOB FIRST
        supabase.table("ocr_jobs").upsert({
            "batch_id": batch_id,
            "status": "processing",
            "progress": 5,
            "message": "Receiving images"
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

        supabase.table("ocr_jobs").update({
            "status": "processing",
            "progress": 15,
            "message": "Starting OCR processing"
        }).eq("batch_id", batch_id).execute()

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