import os
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from pymongo import MongoClient
from pymongo.server_api import ServerApi

app = Flask(__name__)

# --- دریافت تنظیمات از Render ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI") 

# تنظیم هوش مصنوعی
genai.configure(api_key=GEMINI_API_KEY)

# اتصال به دیتابیس
try:
    # اتصال ایمن به Atlas
    mongo_client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
    
    # تست اتصال
    mongo_client.admin.command('ping')
    print("✅ Connected to MongoDB Atlas Successfully!")
    
    db = mongo_client["my_rag_db"]     # اسم دیتابیس
    collection = db["chunks"]          # اسم کالکشن
    
except Exception as e:
    print(f"❌ Database Connection Error: {e}")

# --- توابع ---

def get_embedding(text):
    """متن را به بردار تبدیل می‌کند"""
    result = genai.embed_content(
        model="models/text-embedding-004",
        content=text,
        task_type="retrieval_document"
    )
    return result['embedding']

def chunk_text(text, chunk_size=1000):
    """تقسیم متن به تکه‌های کوچکتر"""
    chunks = []
    paragraphs = text.split('\n\n')
    current_chunk = ""
    for para in paragraphs:
        if len(current_chunk) + len(para) < chunk_size:
            current_chunk += para + "\n\n"
        else:
            if current_chunk: chunks.append(current_chunk.strip())
            current_chunk = para + "\n\n"
    if current_chunk: chunks.append(current_chunk.strip())
    return chunks

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/upload_and_process', methods=['POST'])
def upload_and_process():
    if 'file' not in request.files:
        return jsonify({"error": "فایلی یافت نشد"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "نام فایل خالی است"}), 400

    try:
        text_content = file.read().decode('utf-8')
        text_chunks = chunk_text(text_content)
        
        documents_to_insert = []
        for chunk in text_chunks:
            if chunk.strip():
                vector = get_embedding(chunk)
                documents_to_insert.append({
                    "text": chunk,
                    "embedding": vector,
                    "source_file": file.filename
                })
        
        if documents_to_insert:
            collection.insert_many(documents_to_insert)
            
        return jsonify({
            "status": "success", 
            "message": f"{len(documents_to_insert)} بخش ذخیره شد."
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/search', methods=['POST'])
def search():
    data = request.get_json()
    query = data.get('query')
    
    if not query: return jsonify({"error": "سوال خالی است"}), 400

    try:
        query_embedding = genai.embed_content(
            model="models/text-embedding-004",
            content=query,
            task_type="retrieval_query"
        )['embedding']
        
        # پایپ‌لاین جستجو
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "vector_index",        # نام ایندکس (پیش‌فرض default است)
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

