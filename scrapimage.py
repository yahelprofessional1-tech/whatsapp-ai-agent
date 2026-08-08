import requests
import uuid # <-- NEW: This generates random, safe English names
from supabase import create_client, Client

# --- 1. SETUP YOUR KEYS HERE ---
SUPABASE_URL = "https://xqvtrudvlzxhqdrvddfu.supabase.co"
# Paste your SECRET SERVICE ROLE KEY back in here!
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhxdnRydWR2bHp4aHFkcnZkZGZ1Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2OTA4OTAyNywiZXhwIjoyMDg0NjY1MDI3fQ.lClPLm4Nyi_Art1HR8uh__ueXkPNA7slwuy0XLEncWM" 

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
BUCKET_NAME = "meats"

def migrate_images():
    print("Fetching products from database...")
    response = supabase.table("products").select("*").execute()
    products = response.data

    success_count = 0

    for product in products:
        old_url = product.get("image", "")
        product_id = product["id"]
        product_name = product["name"]

        # Only process images from the old website
        if not old_url or "boaron-market.co.il" not in old_url:
            continue

        try:
            print(f"Processing: {product_name}...")
            
            # 1. Download the image
            img_response = requests.get(old_url)
            img_response.raise_for_status() 
            img_bytes = img_response.content
            
            # Extract the extension (jpg, png)
            ext = old_url.split(".")[-1].split("?")[0]
            if ext.lower() not in ["jpg", "jpeg", "png", "webp"]:
                ext = "jpg" # fallback just in case
                
            # --- THE FIX: Generate a completely random English filename ---
            # This creates a name like "product_7b2f9c8a4d.jpg"
            safe_english_id = uuid.uuid4().hex
            safe_filename = f"product_{safe_english_id}.{ext}"

            # 2. Upload to Supabase Storage
            content_type = "image/png" if ext.lower() == "png" else "image/jpeg"
            
            supabase.storage.from_(BUCKET_NAME).upload(
                file=img_bytes,
                path=safe_filename,
                file_options={"content-type": content_type, "upsert": "true"}
            )

            # 3. Get the new permanent public URL
            new_url = supabase.storage.from_(BUCKET_NAME).get_public_url(safe_filename)

            # 4. Update the database row with the new clean link
            supabase.table("products").update({"image": new_url}).eq("id", product_id).execute()
            
            print(f"✅ Success! Saved as {safe_filename}")
            success_count += 1

        except Exception as e:
            print(f"❌ Failed to process {product_name}. Error: {e}")

    print(f"\nMigration complete! Successfully moved {success_count} images.")

if __name__ == "__main__":
    migrate_images()