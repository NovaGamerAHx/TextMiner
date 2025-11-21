import os
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from pymongo import MongoClient
from pymongo.server_api import ServerApi

app = Flask(__name__)

# --- تنظیمات و متغیرهای محیطی ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = "my_rag_db"
COLLECTION_NAME = "chunks"
INDEX_NAME = "vector_index"  # نام دقیق ایندکس که در MongoDB ساختی

# تنظیم هوش مصنوعی
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("⚠️ هشدار: GEMINI_API_KEY تنظیم نشده است!")

# --- اتصال به دیتابیس ---
try:
    if not MONGO_URI:
        raise ValueError("MONGO_URI تنظیم نشده است!")
        
    mongo_client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
    # تست اتصال
    mongo_client.admin.command('ping')
    print("✅ اتصال به MongoDB Atlas با موفقیت برقرار شد.")
    
    db = mongo_client[DB_NAME]
    collection = db[COLLECTION_NAME]
    
except Exception as e:
    print(f"❌ خطا در اتصال به دیتابیس: {e}")
    collection = None

# --- توابع حیاتی ---

def chunk_text(text, chunk_size=1000):
    """
    این تابع تضمین می‌کند هیچ تکه‌ای بزرگتر از 1000 کاراکتر نباشد.
    حتی اگر متن یک خط طولانی باشد، آن را برش می‌زند.
    """
    chunks = []
    
    # 1. تلاش برای تقسیم با پاراگراف (دو اینتر)
    splits = text.split('\n\n')
    
    # 2. اگر پاراگراف نداشت، تقسیم با خط جدید
    if len(splits) < 2:
        splits = text.split('\n')
        
    current_chunk = ""
    
    for split in splits:
        split = split.strip()
        if not split: continue
        
        # 3. برش اجباری: اگر خودِ این بخش به تنهایی از حد مجاز بزرگتر بود
        if len(split) > chunk_size:
            # اگر چیزی در بافر داریم اول آن را ذخیره کن
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            
            # متن طولانی را کاراکتر به کاراکتر برش بزن
            for i in range(0, len(split), chunk_size):
                chunks.append(split[i:i+chunk_size])
            continue

        # 4. چسباندن تکه‌های کوچک به هم تا رسیدن به سقف مجاز
        if len(current_chunk) + len(split) < chunk_size:
            current_chunk += split + "\n"
        else:
            chunks.append(current_chunk.strip())
            current_chunk = split + "\n"
    
    # ذخیره آخرین تکه باقی‌مانده
    if current_chunk:
        chunks.append(current_chunk.strip())
        
    return chunks

def get_embedding(text):
    """متن را به بردار تبدیل می‌کند"""
    # یک لایه محافظتی برای جلوگیری از ارورهای عجیب
    if not text or len(text.strip()) == 0:
        return None
        
    try:
        result = genai.embed_content(
            model="models/text-embedding-004",
            content=text,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"Embedding Error for chunk: {text[:50]}... -> {e}")
        return None

# --- مسیرهای سایت (Routes) ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/upload_and_process', methods=['POST'])
def upload_and_process():
    if collection is None:
        return jsonify({"error": "ارتباط با دیتابیس قطع است"}), 500

    if 'file' not in request.files:
        return jsonify({"error": "فایلی یافت نشد"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "نام فایل خالی است"}), 400

    try:
        # خواندن فایل
        text_content = file.read().decode('utf-8')
        
        # چانک کردن هوشمند
        text_chunks = chunk_text(text_content, chunk_size=1000)
        
        print(f"تعداد چانک‌های تولید شده: {len(text_chunks)}") # برای دیباگ در لاگ‌ها
        
        documents_to_insert = []
        
        for chunk in text_chunks:
            vector = get_embedding(chunk)
            if vector: # فقط اگر وکتور با موفقیت ساخته شد
                documents_to_insert.append({
                    "text": chunk,
                    "embedding": vector,
                    "source_file": file.filename
                })
        
        if documents_to_insert:
            collection.insert_many(documents_to_insert)
            return jsonify({
                "status": "success", 
                "message": f"فایل با موفقیت پردازش شد. {len(documents_to_insert)} قطعه متن ذخیره شد."
            })
        else:
            return jsonify({"error": "هیچ متن قابل پردازشی یافت نشد یا خطا در ساخت وکتور."}), 400

    except Exception as e:
        print(f"Upload Error: {e}")
        return jsonify({"error": f"خطا در پردازش: {str(e)}"}), 500

@app.route('/search', methods=['POST'])
def search():
    if collection is None:
        return jsonify({"error": "ارتباط با دیتابیس قطع است"}), 500

    data = request.get_json()
    query = data.get('query')
    
    if not query: return jsonify({"error": "سوال خالی است"}), 400

    try:
        # ساخت وکتور برای سوال کاربر
        query_res = genai.embed_content(
            model="models/text-embedding-004",
            content=query,
            task_type="retrieval_query"
        )
        query_embedding = query_res['embedding']
        
        # پایپ‌لاین جستجو
        pipeline = [
            {
                "$vectorSearch": {
                    "index": INDEX_NAME,       # استفاده از نام دقیق ایندکس شما
                    "path": "embedding",
                    "queryVector": query_embedding,
                    "numCandidates": 100,
                    "limit": 5
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "text": 1,
                    "source_file": 1,
                    "score": { "$meta": "vectorSearchScore" }
                }
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        return jsonify({"results": results})

    except Exception as e:
        print(f"Search Error: {e}")
        return jsonify({"error": f"خطا در جستجو: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True)
