# optimized_text_messenger_http.py
from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
import sqlite3
import bcrypt
import jwt
import datetime
import time
import re
from collections import defaultdict
import os
import ssl
import logging
from logging.handlers import RotatingFileHandler
import hashlib
from functools import wraps
import threading
import queue
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
    "https://confidingly-charming-angelfish.cloudpub.ru"
])

# ========== ОПТИМИЗИРОВАННЫЕ ХРАНИЛИЩА ==========

class RateLimiter:
    def __init__(self):
        self._storage = defaultdict(list)
        self._lock = threading.RLock()
    
    def check_rate(self, identifier, window=60, max_requests=60):
        with self._lock:
            now = time.time()
            timestamps = self._storage[identifier]
            
            # Удаляем старые записи
            timestamps[:] = [ts for ts in timestamps if now - ts < window]
            
            if len(timestamps) >= max_requests:
                return False
                
            timestamps.append(now)
            return True
    
    def get_count(self, identifier, window=60):
        with self._lock:
            now = time.time()
            timestamps = self._storage[identifier]
            timestamps[:] = [ts for ts in timestamps if now - ts < window]
            return len(timestamps)

class ConnectionManager:
    def __init__(self):
        self.active_sessions = defaultdict(int)
        self._lock = threading.RLock()
    
    def increment(self, user_id):
        with self._lock:
            self.active_sessions[user_id] += 1
    
    def decrement(self, user_id):
        with self._lock:
            if self.active_sessions[user_id] > 0:
                self.active_sessions[user_id] -= 1
    
    def get_count(self, user_id):
        with self._lock:
            return self.active_sessions.get(user_id, 0)

# Инициализация менеджеров
rate_limiter = RateLimiter()
connection_manager = ConnectionManager()
ip_blacklist = set()
suspicious_activity = defaultdict(list)

# ========== ОПТИМИЗИРОВАННАЯ БАЗА ДАННЫХ ==========

DB_PATH = 'chat_optimized.db'
DB_POOL = queue.Queue(maxsize=15)

def init_db_pool():
    """Инициализация пула соединений с БД"""
    for _ in range(15):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")
        DB_POOL.put(conn)

def get_db_connection():
    """Получение соединения из пула"""
    try:
        return DB_POOL.get(timeout=10)
    except queue.Empty:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

def return_db_connection(conn):
    """Возврат соединения в пул"""
    try:
        DB_POOL.put(conn, block=False)
    except queue.Full:
        conn.close()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def execute_with_retry(query, params=None):
    """Выполнение запроса с повторными попытками"""
    conn = get_db_connection()
    try:
        cursor = conn.execute(query, params or [])
        conn.commit()
        return cursor
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        return_db_connection(conn)

def init_db():
    """Инициализация базы данных с индексами и расширенными таблицами"""
    conn = get_db_connection()
    try:
        # Основная таблица пользователей
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                display_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                last_activity TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                is_online BOOLEAN DEFAULT 0
            )
        ''')
        
        # Расширенная таблица сообщений
        conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                display_name TEXT NOT NULL,
                room TEXT NOT NULL,
                content TEXT NOT NULL,
                message_type TEXT DEFAULT 'text',
                reply_to INTEGER,
                edited BOOLEAN DEFAULT 0,
                edited_at TIMESTAMP,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (reply_to) REFERENCES messages (id)
            )
        ''')
        
        # Таблица статусов сообщений
        conn.execute('''
            CREATE TABLE IF NOT EXISTS message_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (message_id) REFERENCES messages (id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(message_id, user_id)
            )
        ''')
        
        # Таблица комнат
        conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_private BOOLEAN DEFAULT 0,
                max_users INTEGER DEFAULT 100,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (created_by) REFERENCES users (id)
            )
        ''')
        
        # Таблица безопасности
        conn.execute('''
            CREATE TABLE IF NOT EXISTS security_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                user_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # FTS таблица для быстрого поиска
        conn.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts 
            USING fts5(content, room, username, tokenize="porter unicode61")
        ''')
        
        # Триггеры для FTS
        conn.execute('''
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages
            BEGIN
                INSERT INTO messages_fts(rowid, content, room, username)
                VALUES (new.id, new.content, new.room, new.username);
            END
        ''')
        
        conn.execute('''
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages
            BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, room, username)
                VALUES('delete', old.id, old.content, old.room, old.username);
            END
        ''')
        
        # Создание индексов для оптимизации
        conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_room_timestamp ON messages(room, timestamp DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_message_status_composite ON message_status(message_id, user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_security_logs_ip ON security_logs(ip)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_security_logs_timestamp ON security_logs(timestamp DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_users_online ON users(is_online)')
        
        # Создание стандартных комнат
        default_rooms = [
            ('general', 'General Chat', 1),
            ('random', 'Random Chat', 1)
        ]
        
        for room_name, description, created_by in default_rooms:
            conn.execute('''
                INSERT OR IGNORE INTO chat_rooms (name, description, created_by) 
                VALUES (?, ?, ?)
            ''', [room_name, description, created_by])
        
        conn.commit()
        print("Database initialized successfully with optimized schema")
        
    except Exception as e:
        print(f"Database initialization error: {e}")
        raise
    finally:
        return_db_connection(conn)

# ========== УЛУЧШЕННАЯ ВАЛИДАЦИЯ И БЕЗОПАСНОСТЬ ==========

def is_valid_username(username):
    """Валидация имени пользователя"""
    if len(username) < 3 or len(username) > 20:
        return False
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return False
    forbidden_names = ['admin', 'root', 'system', 'moderator', 'null', 'undefined', 'support']
    if username.lower() in forbidden_names:
        return False
    return True

def is_valid_password(password):
    """Валидация пароля"""
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
    # Проверка на распространенные пароли
    common_passwords = ['password', '12345678', 'qwerty123', 'admin123', 'letmein']
    if password.lower() in common_passwords:
        return False
    return True

def safe_message_content(text, max_length=2000):
    """Безопасная обработка текста сообщения"""
    if len(text) > max_length:
        return None
    
    # Экранирование HTML
    text = html.escape(text)
    
    # Удаление опасных паттернов
    dangerous_patterns = [
        r'javascript:', r'vbscript:', r'data:', r'on\w+=',
        r'<script', r'</script>', r'<iframe', r'</iframe>'
    ]
    
    for pattern in dangerous_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    # Удаление избыточных пробелов
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()

def is_valid_room_name(room_name):
    """Валидация названия комнаты"""
    if len(room_name) < 2 or len(room_name) > 50:
        return False
    if not re.match(r'^[a-zA-Z0-9_\- ]+$', room_name):
        return False
    forbidden_rooms = ['admin', 'root', 'system', 'null', 'undefined']
    if room_name.lower() in forbidden_rooms:
        return False
    return True

# ========== УЛУЧШЕННАЯ АУТЕНТИФИКАЦИЯ ==========

def verify_token(token):
    """Проверка JWT токена"""
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def require_auth(f):
    """Декоратор для проверки аутентификации"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'success': False, 'error': 'Token required'}), 401
        
        user_data = verify_token(token)
        if not user_data:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        
        # Обновляем время последней активности
        conn = get_db_connection()
        try:
            conn.execute(
                'UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE id = ?',
                [user_data['user_id']]
            )
            conn.commit()
        except Exception as e:
            app.logger.error(f"Error updating user activity: {e}")
        finally:
            return_db_connection(conn)
        
        g.user_data = user_data
        return f(*args, **kwargs)
    return decorated_function

# ========== ЗАЩИТА ОТ DDoS И ПАРСИНГА ==========

def get_client_fingerprint():
    """Создание отпечатка клиента для идентификации"""
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')
    accept_language = request.headers.get('Accept-Language', '')
    
    fingerprint_string = f"{ip}:{user_agent}:{accept_language}"
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()[:32]

def multi_level_rate_limit(ip, user_id=None, action_type=None):
    """Многоуровневый rate limiting"""
    client_fingerprint = get_client_fingerprint()
    
    # Уровень 1: По IP-адресу (самый строгий)
    if not rate_limiter.check_rate(f"ip:{ip}", window=60, max_requests=200):
        return False
    
    # Уровень 2: По отпечатку клиента
    if not rate_limiter.check_rate(f"fingerprint:{client_fingerprint}", window=60, max_requests=150):
        return False
    
    # Уровень 3: По пользователю (если аутентифицирован)
    if user_id:
        if not rate_limiter.check_rate(f"user:{user_id}", window=60, max_requests=100):
            return False
    
    # Уровень 4: По типу действия
    if action_type:
        if not rate_limiter.check_rate(f"action:{action_type}:{user_id or ip}", window=300, max_requests=50):
            return False
    
    return True

def check_security_limits(ip, action_type):
    """Проверка безопасности с автоматической блокировкой"""
    current_time = time.time()
    
    # Очищаем старые события
    suspicious_activity[ip] = [
        (t, action) for t, action in suspicious_activity[ip] 
        if current_time - t < 600  # 10 минут
    ]
    
    # Добавляем новое событие
    suspicious_activity[ip].append((current_time, action_type))
    
    # Проверяем условия блокировки
    events = suspicious_activity[ip]
    
    # Критерии блокировки:
    # 1. Более 100 событий за 10 минут
    if len(events) > 100:
        ip_blacklist.add(ip)
        log_security_event(ip, 'AUTO_BLACKLIST', f"Too many events: {len(events)}")
        return False
    
    # 2. Быстрые последовательные запросы (DDoS)
    recent_events = [t for t, action in events if current_time - t < 30]
    if len(recent_events) > 50:
        ip_blacklist.add(ip)
        log_security_event(ip, 'AUTO_BLACKLIST', "Flood detection")
        return False
    
    # 3. Множественные неудачные логины
    failed_logins = [t for t, action in events if action == 'LOGIN_FAILED' and current_time - t < 300]
    if len(failed_logins) > 10:
        ip_blacklist.add(ip)
        log_security_event(ip, 'AUTO_BLACKLIST', "Multiple failed logins")
        return False
    
    return True

# ========== ОПТИМИЗИРОВАННЫЕ ENDPOINTS ==========

@app.route('/', methods=['GET'])
def root():
    """Облегченный корневой endpoint"""
    ip = request.remote_addr
    
    if ip in ip_blacklist:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    if not multi_level_rate_limit(ip, action_type='root'):
        return jsonify({'success': False, 'error': 'Rate limit exceeded'}), 429
    
    return jsonify({
        'success': True,
        'message': 'Optimized Text Messenger Server (HTTP Only)',
        'status': 'running',
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'version': '2.0'
    })

@app.route('/register', methods=['POST'])
def register():
    """Оптимизированная регистрация"""
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
        # Проверка существования пользователя
        conn = get_db_connection()
        existing_user = conn.execute(
            'SELECT id FROM users WHERE username = ?', 
            [username]
        ).fetchone()
        
        if existing_user:
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Username already exists'}), 400
        
        # Хеширование пароля
        hashed_password = bcrypt.hashpw(
            password.encode('utf-8'), 
            bcrypt.gensalt(rounds=10)
        ).decode('utf-8')
        
        display_name = safe_message_content(data.get('display_name', username), 100)
        if not display_name:
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Invalid display name'}), 400
        
        # Создание пользователя
        cursor = conn.execute(
            'INSERT INTO users (username, password, display_name) VALUES (?, ?, ?)',
            [username, hashed_password, display_name]
        )
        conn.commit()
        return_db_connection(conn)
        
        log_security_event(ip, 'REGISTRATION_SUCCESS', f'User registered: {username}')
        
        return jsonify({
            'success': True,
            'message': 'User created successfully'
        }), 201
        
    except Exception as e:
        return_db_connection(conn)
        log_security_event(ip, 'REGISTRATION_ERROR', str(e))
        return jsonify({'success': False, 'error': 'Registration failed'}), 500

@app.route('/login', methods=['POST'])
def login():
    """Оптимизированный вход"""
    ip = request.remote_addr
    
    if ip in ip_blacklist:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    if not multi_level_rate_limit(ip, action_type='login'):
        return jsonify({'success': False, 'error': 'Too many login attempts'}), 429
    
    data = request.get_json()
    username = data.get('username', '').lower()
    password = data.get('password', '')
    
    # Задержка для защиты от брутфорса
    time.sleep(0.2)
    
    conn = get_db_connection()
    try:
        user = conn.execute(
            'SELECT * FROM users WHERE username = ? AND is_active = 1',
            [username]
        ).fetchone()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            # Обновляем время последнего входа, активности и статус онлайн
            conn.execute(
                'UPDATE users SET last_login = CURRENT_TIMESTAMP, last_activity = CURRENT_TIMESTAMP, is_online = 1 WHERE id = ?',
                [user['id']]
            )
            conn.commit()
            
            # Создаем JWT токен
            token = jwt.encode({
                'user_id': user['id'],
                'username': user['username'],
                'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
            }, app.config['SECRET_KEY'], algorithm='HS256')
            
            # Увеличиваем счетчик активных сессий
            connection_manager.increment(user['id'])
            
            log_security_event(ip, 'LOGIN_SUCCESS', f'User logged in: {username}', user['id'])
            
            return jsonify({
                'success': True,
                'data': {
                    'token': token,
                    'userId': user['id'],
                    'username': user['username'],
                    'displayName': user['display_name']
                }
            })
        
        # Неудачная попытка входа
        log_security_event(ip, 'LOGIN_FAILED', f'Failed login for: {username}')
        check_security_limits(ip, 'LOGIN_FAILED')
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401
        
    except Exception as e:
        log_security_event(ip, 'LOGIN_ERROR', str(e))
        return jsonify({'success': False, 'error': 'Login failed'}), 500
    finally:
        return_db_connection(conn)

@app.route('/logout', methods=['POST'])
@require_auth
def logout():
    """Выход из системы"""
    try:
        user_id = g.user_data['user_id']
        
        # Обновляем статус пользователя
        conn = get_db_connection()
        try:
            conn.execute(
                'UPDATE users SET is_online = 0 WHERE id = ?',
                [user_id]
            )
            conn.commit()
        finally:
            return_db_connection(conn)
        
        # Уменьшаем счетчик активных сессий
        connection_manager.decrement(user_id)
        
        log_security_event(request.remote_addr, 'LOGOUT', f'User logged out: {g.user_data["username"]}', user_id)
        
        return jsonify({
            'success': True,
            'message': 'Logged out successfully'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': 'Logout failed'}), 500

@app.route('/profile', methods=['GET'])
@require_auth
def get_profile():
    """Получение профиля пользователя"""
    conn = get_db_connection()
    try:
        user = conn.execute(
            'SELECT id, username, display_name, created_at, last_login, last_activity, is_online FROM users WHERE id = ?',
            [g.user_data['user_id']]
        ).fetchone()
        
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Получаем статистику сообщений
        stats = conn.execute(
            'SELECT COUNT(*) as message_count FROM messages WHERE user_id = ?',
            [g.user_data['user_id']]
        ).fetchone()
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['id'],
                'username': user['username'],
                'displayName': user['display_name'],
                'createdAt': user['created_at'],
                'lastLogin': user['last_login'],
                'lastActivity': user['last_activity'],
                'isOnline': bool(user['is_online']),
                'stats': {
                    'messageCount': stats['message_count']
                }
            }
        })
    finally:
        return_db_connection(conn)

# ========== УЛУЧШЕННЫЕ КОМНАТЫ И СООБЩЕНИЯ ==========

@app.route('/chat/rooms', methods=['GET'])
@require_auth
def get_chat_rooms():
    """Получение списка комнат"""
    conn = get_db_connection()
    try:
        rooms = conn.execute('''
            SELECT id, name, description, created_by, created_at, is_private, max_users
            FROM chat_rooms 
            WHERE is_active = 1 
            ORDER BY name
        ''').fetchall()
        
        rooms_list = [{
            'id': room['id'],
            'name': room['name'],
            'description': room['description'],
            'is_private': bool(room['is_private']),
            'max_users': room['max_users'],
            'created_at': room['created_at']
        } for room in rooms]
        
        return jsonify({
            'success': True,
            'data': rooms_list
        })
    finally:
        return_db_connection(conn)

@app.route('/chat/rooms', methods=['POST'])
@require_auth
def create_chat_room():
    """Создание новой комнаты"""
    data = request.get_json()
    room_name = data.get('name', '').strip()
    description = data.get('description', '')
    
    if not room_name:
        return jsonify({'success': False, 'error': 'Room name is required'}), 400
    
    if not is_valid_room_name(room_name):
        return jsonify({'success': False, 'error': 'Invalid room name'}), 400
    
    conn = get_db_connection()
    try:
        # Проверяем, существует ли комната
        existing_room = conn.execute(
            'SELECT id FROM chat_rooms WHERE name = ?',
            [room_name]
        ).fetchone()
        
        if existing_room:
            return jsonify({'success': False, 'error': 'Room already exists'}), 400
        
        # Создаем комнату
        cursor = conn.execute(
            'INSERT INTO chat_rooms (name, description, created_by) VALUES (?, ?, ?)',
            [room_name, description, g.user_data['user_id']]
        )
        conn.commit()
        
        room_id = cursor.lastrowid
        
        log_security_event(request.remote_addr, 'ROOM_CREATED', 
                          f'User {g.user_data["username"]} created room: {room_name}', 
                          g.user_data['user_id'])
        
        return jsonify({
            'success': True,
            'data': {
                'id': room_id,
                'name': room_name,
                'description': description,
                'created_by': g.user_data['user_id']
            }
        }), 201
        
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Error creating room: {e}")
        return jsonify({'success': False, 'error': 'Failed to create room'}), 500
    finally:
        return_db_connection(conn)

@app.route('/chat/messages/<room>', methods=['GET'])
@require_auth
def get_room_messages(room):
    """Получение истории сообщений комнаты с пагинацией по курсору"""
    limit = min(request.args.get('limit', 50, type=int), 100)
    last_id = request.args.get('last_id', type=int)
    
    conn = get_db_connection()
    try:
        if last_id:
            # Пагинация по курсору (более эффективно чем OFFSET)
            messages = conn.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.room = ? AND m.id < ?
                ORDER BY m.id DESC 
                LIMIT ?
            ''', [room, last_id, limit]).fetchall()
        else:
            # Первая загрузка
            messages = conn.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.room = ? 
                ORDER BY m.id DESC 
                LIMIT ?
            ''', [room, limit]).fetchall()
        
        messages_list = []
        for msg in reversed(messages):  # Переворачиваем чтобы новые были в конце
            messages_list.append({
                'id': msg['id'],
                'user_id': msg['user_id'],
                'username': msg['username'],
                'display_name': msg['display_name'],
                'content': msg['content'],
                'message_type': msg['message_type'],
                'reply_to': msg['reply_to'],
                'edited': bool(msg['edited']),
                'edited_at': msg['edited_at'],
                'timestamp': msg['timestamp'],
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

@app.route('/chat/messages', methods=['POST'])
@require_auth
def send_message():
    """Отправка нового сообщения"""
    try:
        user_id = g.user_data['user_id']
        username = g.user_data['username']
        ip = request.remote_addr
        
        # Многоуровневый rate limiting
        if not multi_level_rate_limit(ip, user_id, 'send_message'):
            return jsonify({'success': False, 'error': 'Message rate limit exceeded'}), 429
        
        data = request.get_json()
        content = data.get('content', '').strip()
        room = data.get('room', 'general')
        
        if not content:
            return jsonify({'success': False, 'error': 'Message cannot be empty'}), 400
        
        # Безопасная обработка контента
        sanitized_content = safe_message_content(content)
        if not sanitized_content:
            return jsonify({'success': False, 'error': 'Invalid message content'}), 400
        
        # Получаем display_name пользователя
        conn = get_db_connection()
        try:
            user_record = conn.execute(
                'SELECT display_name FROM users WHERE id = ?',
                [user_id]
            ).fetchone()
            display_name = user_record['display_name'] if user_record else username
            
            # Сохраняем в базу данных
            cursor = execute_with_retry(
                '''INSERT INTO messages (user_id, username, display_name, room, content) 
                   VALUES (?, ?, ?, ?, ?)''',
                [user_id, username, display_name, room, sanitized_content]
            )
            
            message_id = cursor.lastrowid
            
            # Получаем полные данные сообщения
            new_message = conn.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.id = ?
            ''', [message_id]).fetchone()
            
            message_obj = {
                'id': new_message['id'],
                'user_id': new_message['user_id'],
                'username': new_message['username'],
                'display_name': new_message['display_name'],
                'content': new_message['content'],
                'room': new_message['room'],
                'message_type': new_message['message_type'],
                'timestamp': new_message['timestamp']
            }
            
            app.logger.info(f"Message from {username} saved in room {room}")
            
            return jsonify({
                'success': True,
                'data': message_obj
            })
            
        except Exception as e:
            app.logger.error(f"Error saving message: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to save message'}), 500
        finally:
            return_db_connection(conn)
        
    except Exception as e:
        app.logger.error(f"Error in send_message: {str(e)}")
        return jsonify({'success': False, 'error': 'Failed to send message'}), 500

@app.route('/chat/search', methods=['GET'])
@require_auth
def search_messages():
    """Быстрый поиск сообщений с FTS"""
    query = request.args.get('q', '').strip()
    room = request.args.get('room', '')
    
    if not query or len(query) < 2:
        return jsonify({'success': False, 'error': 'Search query too short'}), 400
    
    if len(query) > 100:
        return jsonify({'success': False, 'error': 'Search query too long'}), 400
    
    conn = get_db_connection()
    try:
        if room:
            # Поиск в конкретной комнате
            messages = conn.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.id IN (
                    SELECT rowid FROM messages_fts 
                    WHERE messages_fts MATCH ? AND room = ?
                )
                ORDER BY m.timestamp DESC 
                LIMIT 50
            ''', [f'"{query}"', room]).fetchall()
        else:
            # Глобальный поиск
            messages = conn.execute('''
                SELECT m.*, u.display_name 
                FROM messages m 
                JOIN users u ON m.user_id = u.id 
                WHERE m.id IN (
                    SELECT rowid FROM messages_fts 
                    WHERE messages_fts MATCH ?
                )
                ORDER BY m.timestamp DESC 
                LIMIT 50
            ''', [f'"{query}"']).fetchall()
        
        messages_list = []
        for msg in messages:
            messages_list.append({
                'id': msg['id'],
                'user_id': msg['user_id'],
                'username': msg['username'],
                'display_name': msg['display_name'],
                'content': msg['content'],
                'timestamp': msg['timestamp'],
                'room': msg['room']
            })
        
        return jsonify({
            'success': True,
            'data': messages_list
        })
    finally:
        return_db_connection(conn)

@app.route('/chat/users/online', methods=['GET'])
@require_auth
def get_online_users():
    """Получение списка онлайн пользователей"""
    conn = get_db_connection()
    try:
        online_users = conn.execute('''
            SELECT id, username, display_name, last_activity 
            FROM users 
            WHERE is_online = 1 AND is_active = 1
            ORDER BY username
        ''').fetchall()
        
        users_list = [{
            'id': user['id'],
            'username': user['username'],
            'display_name': user['display_name'],
            'last_activity': user['last_activity']
        } for user in online_users]
        
        return jsonify({
            'success': True,
            'data': users_list
        })
    finally:
        return_db_connection(conn)

# ========== УЛУЧШЕННОЕ ЛОГИРОВАНИЕ ==========

def setup_logging():
    """Настройка логирования"""
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    # Форматтер для логов
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [%(name)s]'
    )
    
    # Логи безопасности
    security_handler = RotatingFileHandler(
        'logs/security.log', 
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    security_handler.setFormatter(formatter)
    security_handler.setLevel(logging.WARNING)
    
    # Логи приложения
    app_handler = RotatingFileHandler(
        'logs/app.log',
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3
    )
    app_handler.setFormatter(formatter)
    app_handler.setLevel(logging.INFO)
    
    # Логи ошибок
    error_handler = RotatingFileHandler(
        'logs/error.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)
    
    app.logger.addHandler(security_handler)
    app.logger.addHandler(app_handler)
    app.logger.addHandler(error_handler)
    app.logger.setLevel(logging.INFO)
    
    # Отключаем логи Flask по умолчанию
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

def log_security_event(ip, action, details, user_id=None):
    """Логирование security событий"""
    app.logger.warning(f"SECURITY: {ip} - {action} - {details}")
    
    try:
        execute_with_retry(
            'INSERT INTO security_logs (ip, action, details, user_id) VALUES (?, ?, ?, ?)',
            [ip, action, details, user_id]
        )
    except Exception as e:
        app.logger.error(f"Failed to log security event: {e}")

# ========== MIDDLEWARE И ФИЛЬТРЫ ==========

@app.before_request
def security_checks():
    """Упрощенные проверки безопасности"""
    ip = request.remote_addr
    
    if ip in ip_blacklist:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    if not multi_level_rate_limit(ip, action_type='general_request'):
        log_security_event(ip, 'GLOBAL_RATE_LIMIT', 'Global rate limit exceeded')
        return jsonify({'success': False, 'error': 'Rate limit exceeded'}), 429
    
    # Проверка User-Agent
    user_agent = request.headers.get('User-Agent', '')
    if not user_agent or len(user_agent) > 500:
        return jsonify({'success': False, 'error': 'Invalid request'}), 400
    
    # Проверка размера запроса
    if request.content_length and request.content_length > 2 * 1024 * 1024:  # 2MB
        return jsonify({'success': False, 'error': 'Request too large'}), 413

# ========== HEALTH CHECK И МОНИТОРИНГ ==========

@app.route('/health', methods=['GET'])
def health_check():
    """Проверка здоровья сервера"""
    try:
        # Проверяем соединение с БД
        conn = get_db_connection()
        conn.execute("SELECT 1").fetchone()
        return_db_connection(conn)
        
        # Собираем статистику
        stats = {
            'timestamp': datetime.datetime.utcnow().isoformat(),
            'status': 'healthy',
            'db_connections': DB_POOL.qsize(),
            'memory_usage': f"{os.getpid()}",
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

# ========== ФОНОВЫЕ ЗАДАЧИ ==========

def background_cleanup():
    """Фоновая очистка старых данных"""
    while True:
        try:
            time.sleep(3600)  # Каждый час
            
            conn = get_db_connection()
            try:
                # Очистка старых логов безопасности (старше 30 дней)
                conn.execute(
                    "DELETE FROM security_logs WHERE timestamp < datetime('now', '-30 days')"
                )
                
                # Очистка старых статусов сообщений
                conn.execute('''
                    DELETE FROM message_status 
                    WHERE message_id IN (
                        SELECT id FROM messages WHERE timestamp < datetime('now', '-7 days')
                    )
                ''')
                
                # Обновление статуса онлайн для неактивных пользователей
                conn.execute('''
                    UPDATE users SET is_online = 0 
                    WHERE last_activity < datetime('now', '-5 minutes') AND is_online = 1
                ''')
                
                conn.commit()
                app.logger.info("Background cleanup completed successfully")
                
            except Exception as e:
                app.logger.error(f"Background cleanup error: {e}")
            finally:
                return_db_connection(conn)
                
        except Exception as e:
            app.logger.error(f"Background cleanup thread error: {e}")
            time.sleep(60)

# ========== ОБРАБОТЧИКИ ОШИБОК ==========

@app.errorhandler(413)
def too_large(e):
    return jsonify({'success': False, 'error': 'File too large'}), 413

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({'success': False, 'error': 'Rate limit exceeded'}), 429

@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    app.logger.error(f"Internal server error: {e}")
    return jsonify({'success': False, 'error': 'Internal server error'}), 500

# ========== ЗАПУСК СЕРВЕРА ==========

app_start_time = time.time()

if __name__ == '__main__':
    # Инициализация
    setup_logging()
    init_db_pool()
    
    with app.app_context():
        init_db()
    
    # Запуск фоновых задач
    cleanup_thread = threading.Thread(target=background_cleanup, daemon=True)
    cleanup_thread.start()
    
    print("=" * 60)
    print("Optimized Text Messenger Server v2.0 (HTTP Only)")
    print("=" * 60)
    print("Features:")
    print("  ✅ Database connection pooling")
    print("  ✅ Multi-level rate limiting")
    print("  ✅ Enhanced security filters")
    print("  ✅ Full-text search (FTS5)")
    print("  ✅ Message status tracking")
    print("  ✅ Background cleanup tasks")
    print("  ✅ Cursor-based pagination")
    print("  ✅ Automatic DDoS protection")
    print("  ✅ Online users tracking")
    print("=" * 60)
    
    # Получаем порт из переменной окружения (для Render) или используем 5000 по умолчанию
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}")
    
    # SSL конфигурация (не нужно для Render - они сами обрабатывают HTTPS)
    ssl_context = None
    
    # Запуск сервера
    try:
        app.run(
            host='0.0.0.0', 
            port=port,
            debug=False,
            ssl_context=ssl_context
        )
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        print("Cleaning up resources...")
        # Очистка пула соединений
        while not DB_POOL.empty():
            try:
                conn = DB_POOL.get_nowait()
                conn.close()
            except queue.Empty:
                break
        print("Server shutdown complete")
