import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai

app = Flask(__name__)

# --- تنظیمات ---
# کلید امنیتی برای نشست‌ها (یک متن تصادفی)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'mysecretkey123')

# تنظیم دیتابیس (روی Render اگر دیتابیس وصل کنید از آن استفاده می‌کند، وگرنه فایل لوکال)
database_url = os.environ.get('DATABASE_URL')
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- تنظیمات جمینای ---
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# *** نام مدل را اینجا دقیق وارد کنید ***
# اگر دسترسی به 2.5 دارید دقیقا نامش را جایگزین کنید (مثلا gemini-2.5-flash)
# فعلا gemini-1.5-flash یا gemini-2.0-flash-exp رایج هستند.
MODEL_NAME = "gemini-1.5-flash" 

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# --- مدل‌های دیتابیس ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    chats = db.relationship('Chat', backref='owner', lazy=True)

class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), default="چت جدید")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('Message', backref='chat', lazy=True, cascade="all, delete-orphan")

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('chat.id'), nullable=False)
    role = db.Column(db.String(10), nullable=False) # 'user' or 'model'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- روت‌ها ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        return render_template('index.html', user=current_user)
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'نام کاربری یا رمز عبور اشتباه است'})
    return render_template('index.html', page='login') # قالب هوشمند هندل میکند

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('username')
    pass1 = data.get('password')
    pass2 = data.get('confirm_password')

    if pass1 != pass2:
        return jsonify({'success': False, 'message': 'رمزهای عبور مطابقت ندارند'})
    
    if User.query.filter_by(username=username).first():
        return jsonify({'success': False, 'message': 'نام کاربری قبلا گرفته شده است'})

    new_user = User(username=username, password=generate_password_hash(pass1))
    db.session.add(new_user)
    db.session.commit()
    login_user(new_user)
    return jsonify({'success': True})

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- API های چت ---

@app.route('/api/chats', methods=['GET'])
@login_required
def get_chats():
    chats = Chat.query.filter_by(user_id=current_user.id).order_by(Chat.created_at.desc()).all()
    return jsonify([{'id': c.id, 'title': c.title} for c in chats])

@app.route('/api/chats', methods=['POST'])
@login_required
def create_chat():
    new_chat = Chat(user_id=current_user.id)
    db.session.add(new_chat)
    db.session.commit()
    return jsonify({'id': new_chat.id, 'title': new_chat.title})

@app.route('/api/chats/<int:chat_id>', methods=['GET'])
@login_required
def get_chat_history(chat_id):
    chat = Chat.query.get_or_404(chat_id)
    if chat.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    messages = [{'role': m.role, 'content': m.content} for m in chat.messages]
    return jsonify({'id': chat.id, 'title': chat.title, 'messages': messages})

@app.route('/api/send_message', methods=['POST'])
@login_required
def send_message():
    data = request.get_json()
    chat_id = data.get('chat_id')
    user_message = data.get('message')
    web_search_enabled = data.get('web_search', False)

    chat = Chat.query.get_or_404(chat_id)
    if chat.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403

    # ذخیره پیام کاربر
    db.session.add(Message(chat_id=chat.id, role='user', content=user_message))
    
    # آپدیت تایتل چت اگر اولین پیام باشد
    if len(chat.messages) == 0:
        chat.title = user_message[:30] + "..."
    
    db.session.commit()

    bot_reply = ""

    if web_search_enabled:
        # حالت جستجوی وب روشن (متن ثابت)
        bot_reply = "جستجوی وب فعال است، اما در حال حاضر این قابلیت به صورت نمایشی می‌باشد و نتایج وب بازیابی نمی‌شوند."
    else:
        # حالت عادی: ارسال به جمینای
        try:
            # ساخت تاریخچه برای ارسال به مدل
            history = []
            # سیستم پرامپت
            system_instruction = "شما یک دستیار هوشمند، مودب و دقیق هستید. پاسخ‌ها را با فرمت Markdown ارائه دهید."
            
            # تبدیل تاریخچه دیتابیس به فرمت جمینای
            # (نکته: جمینای 1.5 فلش context window بزرگی دارد، کل تاریخچه را می‌فرستیم)
            existing_msgs = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).all()
            
            chat_history_for_model = []
            for m in existing_msgs:
                role = "user" if m.role == "user" else "model"
                # آخرین پیام (که الان ذخیره کردیم) را هم شامل می‌شود
                chat_history_for_model.append({"role": role, "parts": [m.content]})
            
            # چون پیام آخر کاربر را در دیتابیس ذخیره کردیم و در لیست بالا هست،
            # باید لیست را به گونه ای به مدل بدهیم که پیام آخر را به عنوان پرامپت جدید تلقی کند یا از chat session استفاده کنیم.
            # روش ساده تر با SDK:
            
            model = genai.GenerativeModel(
                model_name=MODEL_NAME,
                system_instruction=system_instruction
            )
            
            # ارسال تاریخچه به جز پیام آخر برای ساخت سشن
            history_obj = chat_history_for_model[:-1] 
            chat_session = model.start_chat(history=history_obj)
            
            response = chat_session.send_message(user_message)
            bot_reply = response.text

        except Exception as e:
            bot_reply = f"خطا در ارتباط با هوش مصنوعی: {str(e)}"

    # ذخیره پاسخ مدل
    db.session.add(Message(chat_id=chat.id, role='model', content=bot_reply))
    db.session.commit()

    return jsonify({'response': bot_reply})

# ایجاد جداول دیتابیس قبل از اولین درخواست
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
