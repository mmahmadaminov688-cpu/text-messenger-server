# optimized_text_messenger_postgres.py
from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool
import bcrypt
import jwt
import datetime
import time
import re
from collections import defaultdict
import os
import logging
from logging.handlers import RotatingFileHandler
import hashlib
from functools import wraps
import threading
import html
from tenacity import retry, stop_after_attempt, wait_exponential

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-secret-key-change-in-production')

# ========== КОНФИГУРАЦИЯ ==========

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "200 per hour"],
    storage_uri="memory://",
    strategy="moving-window"
)

CORS(app, origins=[
    "http://localhost:3000",
    "http://127.0.0.1:3000", 
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "https://localhost",
    "https://your-app-name.onrender.com",  # ← ЗАМЕНИТЕ на ваше имя
    "https://confidingly-charming-angelfish.cloudpub.ru"
])

# ========== POSTGRESQL ПОДКЛЮЧЕНИЕ ==========

# Пул соединений PostgreSQL
DB_POOL = None

def init_db_pool():
    """Инициализация пула соединений с PostgreSQL"""
    global DB_POOL
    try:
        DB_POOL = psycopg2.pool.SimpleConnectionPool(
            1, 20, os.environ['DATABASE_URL']
        )
        print("✅ PostgreSQL connection pool initialized")
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        # Для локальной разработки без Render
        DB_POOL = psycopg2.pool.SimpleConnectionPool(
            1, 20, "postgresql://localhost/messenger"
        )

def get_db_connection():
    """Получение соединения из пула"""
    try:
        conn = DB_POOL.getconn()
        conn.cursor_factory = RealDictCursor  # Аналог row_factory
        return conn
    except Exception as e:
        print(f"❌ Error getting connection: {e}")
        raise

def return_db_connection(conn):
    """Возврат соединения в пул"""
    try:
        DB_POOL.putconn(conn)
    except Exception as e:
        print(f"❌ Error returning connection: {e}")
        conn.close()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def execute_with_retry(query, params=None):
    """Выполнение запроса с повторными попытками"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query, params or ())
        conn.commit()
        return cursor
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        return_db_connection(conn)

def init_db():
    """Инициализация базы данных PostgreSQL"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Основная таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password TEXT NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                last_activity TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                is_online BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Таблица сообщений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username VARCHAR(50) NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                room VARCHAR(100) NOT NULL,
                content TEXT NOT NULL,
                message_type VARCHAR(20) DEFAULT 'text',
                reply_to INTEGER,
                edited BOOLEAN DEFAULT FALSE,
                edited_at TIMESTAMP,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица статусов сообщений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_status (
                id SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'sent',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_id, user_id)
            )
        ''')
        
        # Таблица комнат
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_rooms (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                description TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_private BOOLEAN DEFAULT FALSE,
                max_users INTEGER DEFAULT 100,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # Таблица безопасности
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS security_logs (
                id SERIAL PRIMARY KEY,
                ip VARCHAR(45) NOT NULL,
                action VARCHAR(100) NOT NULL,
                details TEXT,
                user_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Создание стандартных комнат
        default_rooms = [
            ('general', 'General Chat', 1),
            ('random', 'Random Chat', 1)
        ]
        
        for room_name, description, created_by in default_rooms:
            cursor.execute('''
                INSERT INTO chat_rooms (name, description, created_by) 
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO NOTHING
            ''', (room_name, description, created_by))
        
        conn.commit()
        print("✅ PostgreSQL database initialized successfully")
        
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        raise
    finally:
        return_db_connection(conn)

# ========== ОСТАЛЬНОЙ КОД АНАЛОГИЧЕН ==========

class RateLimiter:
    def __init__(self):
        self._storage = defaultdict(list)
        self._lock = threading.RLock()
    
    def check_rate(self, identifier, window=60, max_requests=60):
        with self._lock:
            now = time.time()
            timestamps = self._storage[identifier]
            
            timestamps[:] = [ts for ts in timestamps if now - ts < window]
            
            if len(timestamps) >= max_requests:
                return False
                
            timestamps.append(now)
            return True

# Инициализация менеджеров
rate_limiter = RateLimiter()
ip_blacklist = set()
suspicious_activity = defaultdict(list)

# ========== ВАЛИДАЦИЯ ==========

def is_valid_username(username):
    if len(username) < 3 or len(username) > 20:
        return False
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return False
    forbidden_names = ['admin', 'root', 'system', 'moderator', 'null', 'undefined', 'support']
    if username.lower() in forbidden_names:
        return False
    return True

def is_valid_password(password):
    if len(password) < 8:
        return False
    if not any(c.islower() for c in password):
        return False
    if not any(c.isupper() for c in password):
        return False
    if not any(c.isdigit() for c in password):
        return False
    if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?`~' for c in password):
        return False
    common_passwords = ['password', '12345678', 'qwerty123', 'admin123', 'letmein']
    if password.lower() in common_passwords:
        return False
    return True

def safe_message_content(text, max_length=2000):
    if len(text) > max_length:
        return None
    
    text = html.escape(text)
    
    dangerous_patterns = [
        r'javascript:', r'vbscript:', r'data:', r'on\w+=',
        r'<script', r'</script>', r'<iframe', r'</iframe>'
    ]
    
    for pattern in dangerous_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# ========== АУТЕНТИФИКАЦИЯ ==========

def verify_token(token):
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'success': False, 'error': 'Token required'}), 401
        
        user_data = verify_token(token)
        if not user_data:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        
        g.user_data = user_data
        return f(*args, **kwargs)
    return decorated_function

# ========== RATE LIMITING ==========

def get_client_fingerprint():
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')
    accept_language = request.headers.get('Accept-Language', '')
    
    fingerprint_string = f"{ip}:{user_agent}:{accept_language}"
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()[:32]

def multi_level_rate_limit(ip, user_id=None, action_type=None):
    client_fingerprint = get_client_fingerprint()
    
    if not rate_limiter.check_rate(f"ip:{ip}", window=60, max_requests=200):
        return False
    
    if not rate_limiter.check_rate(f"fingerprint:{client_fingerprint}", window=60, max_requests=150):
        return False
    
    if user_id:
        if not rate_limiter.check_rate(f"user:{user_id}", window=60, max_requests=100):
            return False
    
    if action_type:
        if not rate_limiter.check_rate(f"action:{action_type}:{user_id or ip}", window=300, max_requests=50):
            return False
    
    return True

# ========== ENDPOINTS ==========

@app.route('/', methods=['GET'])
def root():
    ip = request.remote_addr
    
    if ip in ip_blacklist:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    if not multi_level_rate_limit(ip, action_type='root'):
        return jsonify({'success': False, 'error': 'Rate limit exceeded'}), 429
    
    return jsonify({
        'success': True,
        'message': 'Optimized Text Messenger Server with PostgreSQL',
        'status': 'running',
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'version': '2.0'
    })

@app.route('/register', methods=['POST'])
def register():
    ip = request.remote_addr
    
    if ip in ip_blacklist:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    if not multi_level_rate_limit(ip, action_type='register'):
        return jsonify({'success': False, 'error': 'Too many registration attempts'}), 429
    
    data = request.get_json()
    if not data or 'username' not in data or 'password' not in data:
        return jsonify({'success': False, 'error': 'Username and password required'}), 400
    
    username = data['username'].strip().lower()
    password = data['password']
    
    if not is_valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username format'}), 400
    
    if not is_valid_password(password):
        return jsonify({'success': False, 'error': 'Password does not meet security requirements'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT id FROM users WHERE username = %s', (username,))
        existing_user = cursor.fetchone()
        
        if existing_user:
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Username already exists'}), 400
        
        hashed_password = bcrypt.hashpw(
            password.encode('utf-8'), 
            bcrypt.gensalt(rounds=10)
        ).decode('utf-8')
        
        display_name = safe_message_content(data.get('display_name', username), 100)
        if not display_name:
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Invalid display name'}), 400
        
        cursor.execute(
            'INSERT INTO users (username, password, display_name) VALUES (%s, %s, %s) RETURNING id',
            (username, hashed_password, display_name)
        )
        user_id = cursor.fetchone()['id']
        conn.commit()
        return_db_connection(conn)
        
        return jsonify({
            'success': True,
            'message': 'User created successfully',
            'userId': user_id
        }), 201
        
    except Exception as e:
        return_db_connection(conn)
        return jsonify({'success': False, 'error': 'Registration failed'}), 500

@app.route('/login', methods=['POST'])
def login():
    ip = request.remote_addr
    
    if ip in ip_blacklist:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    if not multi_level_rate_limit(ip, action_type='login'):
        return jsonify({'success': False, 'error': 'Too many login attempts'}), 429
    
    data = request.get_json()
    username = data.get('username', '').lower()
    password = data.get('password', '')
    
    time.sleep(0.2)
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = %s AND is_active = TRUE', (username,))
        user = cursor.fetchone()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            cursor.execute(
                'UPDATE users SET last_login = CURRENT_TIMESTAMP, last_activity = CURRENT_TIMESTAMP, is_online = TRUE WHERE id = %s',
                (user['id'],)
            )
            conn.commit()
            
            token = jwt.encode({
                'user_id': user['id'],
                'username': user['username'],
                'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
            }, app.config['SECRET_KEY'], algorithm='HS256')
            
            return jsonify({
                'success': True,
                'data': {
                    'token': token,
                    'userId': user['id'],
                    'username': user['username'],
                    'displayName': user['display_name']
                }
            })
        
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401
        
    except Exception as e:
        return jsonify({'success': False, 'error': 'Login failed'}), 500
    finally:
        return_db_connection(conn)

@app.route('/profile', methods=['GET'])
@require_auth
def get_profile():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, username, display_name, created_at, last_login, last_activity, is_online FROM users WHERE id = %s',
            (g.user_data['user_id'],)
        )
        user = cursor.fetchone()
        
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        cursor.execute('SELECT COUNT(*) as message_count FROM messages WHERE user_id = %s', (g.user_data['user_id'],))
        stats = cursor.fetchone()
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['id'],
                'username': user['username'],
                'displayName': user['display_name'],
                'createdAt': user['created_at'].isoformat() if user['created_at'] else None,
                'lastLogin': user['last_login'].isoformat() if user['last_login'] else None,
                'lastActivity': user['last_activity'].isoformat() if user['last_activity'] else None,
                'isOnline': user['is_online'],
                'stats': {
                    'messageCount': stats['message_count']
                }
            }
        })
    finally:
        return_db_connection(conn)

@app.route('/chat/messages', methods=['POST'])
@require_auth
def send_message():
    try:
        user_id = g.user_data['user_id']
        username = g.user_data['username']
        ip = request.remote_addr
        
        if not multi_level_rate_limit(ip, user_id, 'send_message'):
            return jsonify({'success': False, 'error': 'Message rate limit exceeded'}), 429
        
        data = request.get_json()
        content = data.get('content', '').strip()
        room = data.get('room', 'general')
        
        if not content:
            return jsonify({'success': False, 'error': 'Message cannot be empty'}), 400
        
        sanitized_content = safe_message_content(content)
        if not sanitized_content:
            return jsonify({'success': False, 'error': 'Invalid message content'}), 400
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT display_name FROM users WHERE id = %s', (user_id,))
            user_record = cursor.fetchone()
            display_name = user_record['display_name'] if user_record else username
            
            cursor.execute(
                '''INSERT INTO messages (user_id, username, display_name, room, content) 
                   VALUES (%s, %s, %s, %s, %s) RETURNING id''',
                (user_id, username, display_name, room, sanitized_content)
            )
            message_id = cursor.fetchone()['id']
            
            cursor.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.id = %s
            ''', (message_id,))
            new_message = cursor.fetchone()
            
            conn.commit()
            
            message_obj = {
                'id': new_message['id'],
                'user_id': new_message['user_id'],
                'username': new_message['username'],
                'display_name': new_message['display_name'],
                'content': new_message['content'],
                'room': new_message['room'],
                'message_type': new_message['message_type'],
                'timestamp': new_message['timestamp'].isoformat()
            }
            
            return jsonify({
                'success': True,
                'data': message_obj
            })
            
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': 'Failed to save message'}), 500
        finally:
            return_db_connection(conn)
        
    except Exception as e:
        return jsonify({'success': False, 'error': 'Failed to send message'}), 500

@app.route('/chat/messages/<room>', methods=['GET'])
@require_auth
def get_room_messages(room):
    limit = min(request.args.get('limit', 50, type=int), 100)
    last_id = request.args.get('last_id', type=int)
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        if last_id:
            cursor.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.room = %s AND m.id < %s
                ORDER BY m.id DESC 
                LIMIT %s
            ''', (room, last_id, limit))
        else:
            cursor.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.room = %s 
                ORDER BY m.id DESC 
                LIMIT %s
            ''', (room, limit))
        
        messages = cursor.fetchall()
        
        messages_list = []
        for msg in reversed(messages):
            messages_list.append({
                'id': msg['id'],
                'user_id': msg['user_id'],
                'username': msg['username'],
                'display_name': msg['display_name'],
                'content': msg['content'],
                'message_type': msg['message_type'],
                'reply_to': msg['reply_to'],
                'edited': msg['edited'],
                'edited_at': msg['edited_at'].isoformat() if msg['edited_at'] else None,
                'timestamp': msg['timestamp'].isoformat(),
                'room': msg['room']
            })
        
        return jsonify({
            'success': True,
            'data': {
                'messages': messages_list,
                'has_more': len(messages) == limit
            }
        })
    finally:
        return_db_connection(conn)

@app.route('/chat/rooms', methods=['GET'])
@require_auth
def get_chat_rooms():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, description, created_by, created_at, is_private, max_users
            FROM chat_rooms 
            WHERE is_active = TRUE 
            ORDER BY name
        ''')
        rooms = cursor.fetchall()
        
        rooms_list = [{
            'id': room['id'],
            'name': room['name'],
            'description': room['description'],
            'is_private': room['is_private'],
            'max_users': room['max_users'],
            'created_at': room['created_at'].isoformat()
        } for room in rooms]
        
        return jsonify({
            'success': True,
            'data': rooms_list
        })
    finally:
        return_db_connection(conn)

@app.route('/health', methods=['GET'])
def health_check():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        return_db_connection(conn)
        
        stats = {
            'timestamp': datetime.datetime.utcnow().isoformat(),
            'status': 'healthy',
            'database': 'postgresql_connected',
            'uptime': int(time.time() - app_start_time)
        }
        
        return jsonify({
            'success': True,
            'data': stats
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'status': 'unhealthy',
            'error': str(e)
        }), 500

# ========== ПИНГЕР ДЛЯ WEB-СЕРВЕРА ==========

def keep_alive_pinger():
    """Пингер чтобы сервер не засыпал на бесплатном плане"""
    import requests
    while True:
        try:
            # Замените на ваш URL после деплоя
            url = "https://your-app-name.onrender.com/health"
            response = requests.get(url, timeout=10)
            print(f"✅ Pinger: {response.status_code} - {datetime.datetime.now()}")
        except Exception as e:
            print(f"❌ Pinger error: {e}")
        time.sleep(300)  # Пинг каждые 5 минут

# ========== ЗАПУСК СЕРВЕРА ==========

app_start_time = time.time()

if __name__ == '__main__':
    # Инициализация
    init_db_pool()
    
    with app.app_context():
        init_db()
    
    # Запуск пингера в отдельном потоке
    pinger_thread = threading.Thread(target=keep_alive_pinger, daemon=True)
    pinger_thread.start()
    
    print("=" * 60)
    print("🚀 Optimized Text Messenger with PostgreSQL")
    print("=" * 60)
    print("✅ PostgreSQL database connected")
    print("✅ Pinger started (keeps server awake)")
    print("✅ All endpoints ready")
    print("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
