import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai

app = Flask(__name__)

# --- تنظیمات ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-key-for-dev')

# تنظیمات دیتابیس
database_url = os.environ.get('DATABASE_URL')
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# تنظیمات جمینای
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# نام مدل طبق دستور شما
MODEL_NAME = os.environ.get('MODEL_NAME', 'gemini-2.5-flash') 

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# --- مدل‌های دیتابیس (اصلاح شده) ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    # تغییر به Text برای جلوگیری از ارور طول پسورد
    password = db.Column(db.Text, nullable=False)
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
    role = db.Column(db.String(20), nullable=False)
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
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    if request.method == 'POST':
        data = request.get_json()
        user = User.query.filter_by(username=data.get('username')).first()
        if user and check_password_hash(user.password, data.get('password')):
            login_user(user)
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'نام کاربری یا رمز اشتباه است'})
    return render_template('index.html', user=current_user)

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if User.query.filter_by(username=data.get('username')).first():
        return jsonify({'success': False, 'message': 'کاربر تکراری است'})
    
    new_user = User(username=data.get('username'), password=generate_password_hash(data.get('password')))
    db.session.add(new_user)
    db.session.commit()
    login_user(new_user)
    return jsonify({'success': True})

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- API چت ---
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
    return jsonify({
        'id': chat.id, 
        'title': chat.title, 
        'messages': [{'role': m.role, 'content': m.content} for m in chat.messages]
    })

@app.route('/api/send_message', methods=['POST'])
@login_required
def send_message():
    print("--- Request received at /api/send_message ---")
    try:
        data = request.get_json()
        # تبدیل اجباری به int چون گاهی json عدد را رشته میفرستد
        chat_id = int(data.get('chat_id'))
        user_msg = data.get('message')
        web_search = data.get('web_search', False)

        chat = Chat.query.get(chat_id)
        if not chat or chat.user_id != current_user.id:
            return jsonify({'error': 'Chat not found or access denied'}), 403

        # ذخیره پیام کاربر
        db.session.add(Message(chat_id=chat.id, role='user', content=user_msg))
        if not chat.messages:
            chat.title = user_msg[:30] + "..."
        db.session.commit()

        # هوش مصنوعی
        bot_reply = "..."
        if web_search:
            bot_reply = "جستجوی وب فعال است (در حال حاضر غیرفعال)."
        else:
            try:
                # ساخت تاریخچه
                history = []
                previous_msgs = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).all()
                # به جز پیام آخر که الان اضافه کردیم
                for m in previous_msgs[:-1]:
                    role = "user" if m.role == "user" else "model"
                    history.append({"role": role, "parts": [m.content]})

                model = genai.GenerativeModel(
                    model_name=MODEL_NAME,
                    system_instruction="You are a helpful AI."
                )
                chat_session = model.start_chat(history=history)
                response = chat_session.send_message(user_msg)
                bot_reply = response.text
            except Exception as e:
                print(f"Gemini Error: {e}")
                bot_reply = f"خطا در مدل {MODEL_NAME}: {str(e)}"

        # ذخیره جواب
        db.session.add(Message(chat_id=chat.id, role='model', content=bot_reply))
        db.session.commit()
        
        return jsonify({'response': bot_reply})

    except Exception as e:
        print(f"Server Error: {e}")
        return jsonify({'error': str(e)}), 500

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
