from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import bcrypt
import jwt
import datetime
import time
import re
import os
import requests
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-secret-key-change-in-production')

# CORS
CORS(app)

# Rate limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"]
)

# Database connection
def get_db_connection():
    """Get database connection"""
    try:
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        conn.cursor_factory = RealDictCursor
        return conn
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

def init_db():
    """Initialize database tables"""
    conn = get_db_connection()
    if not conn:
        return
    
    try:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password TEXT NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                is_online BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username VARCHAR(50) NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                room VARCHAR(100) NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Rooms table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_rooms (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create default rooms
        cursor.execute('''
            INSERT INTO chat_rooms (name, description) 
            VALUES ('general', 'General Chat')
            ON CONFLICT (name) DO NOTHING
        ''')
        
        cursor.execute('''
            INSERT INTO chat_rooms (name, description) 
            VALUES ('random', 'Random Chat')
            ON CONFLICT (name) DO NOTHING
        ''')
        
        conn.commit()
        print("✅ Database initialized successfully")
        
    except Exception as e:
        print(f"❌ Database init error: {e}")
    finally:
        conn.close()

# Helper functions
def is_valid_username(username):
    return 3 <= len(username) <= 20 and re.match(r'^[a-zA-Z0-9_]+$', username)

def safe_message_content(text, max_length=1000):
    if len(text) > max_length:
        return None
    # Basic sanitization
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    return text.strip()

def verify_token(token):
    try:
        return jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
    except:
        return None

def require_auth(f):
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

# Routes
@app.route('/')
def root():
    return jsonify({
        'success': True,
        'message': 'Text Messenger API',
        'status': 'running'
    })

@app.route('/health')
def health():
    try:
        conn = get_db_connection()
        if conn:
            conn.close()
            return jsonify({'success': True, 'status': 'healthy'})
        else:
            return jsonify({'success': False, 'status': 'db_error'}), 500
    except:
        return jsonify({'success': False, 'status': 'error'}), 500

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data or 'username' not in data or 'password' not in data:
        return jsonify({'success': False, 'error': 'Username and password required'}), 400
    
    username = data['username'].strip().lower()
    password = data['password']
    
    if not is_valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    
    if len(password) < 6:
        return jsonify({'success': False, 'error': 'Password too short'}), 400
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database error'}), 500
    
    try:
        cursor = conn.cursor()
        
        # Check if user exists
        cursor.execute('SELECT id FROM users WHERE username = %s', (username,))
        if cursor.fetchone():
            return jsonify({'success': False, 'error': 'Username exists'}), 400
        
        # Create user
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        display_name = data.get('display_name', username)
        
        cursor.execute(
            'INSERT INTO users (username, password, display_name) VALUES (%s, %s, %s) RETURNING id',
            (username, hashed_password, display_name)
        )
        user_id = cursor.fetchone()['id']
        conn.commit()
        
        return jsonify({
            'success': True,
            'message': 'User created',
            'userId': user_id
        }), 201
        
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': 'Registration failed'}), 500
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '').lower()
    password = data.get('password', '')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database error'}), 500
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = %s', (username,))
        user = cursor.fetchone()
        
        if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
            # Update last login
            cursor.execute(
                'UPDATE users SET last_login = CURRENT_TIMESTAMP, is_online = TRUE WHERE id = %s',
                (user['id'],)
            )
            conn.commit()
            
            # Create token
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
        conn.close()

@app.route('/chat/messages', methods=['POST'])
@require_auth
def send_message():
    data = request.get_json()
    content = data.get('content', '').strip()
    room = data.get('room', 'general')
    
    if not content:
        return jsonify({'success': False, 'error': 'Message empty'}), 400
    
    sanitized_content = safe_message_content(content)
    if not sanitized_content:
        return jsonify({'success': False, 'error': 'Invalid message'}), 400
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database error'}), 500
    
    try:
        cursor = conn.cursor()
        
        cursor.execute(
            'INSERT INTO messages (user_id, username, display_name, room, content) VALUES (%s, %s, %s, %s, %s) RETURNING id, timestamp',
            (g.user_data['user_id'], g.user_data['username'], g.user_data['username'], room, sanitized_content)
        )
        result = cursor.fetchone()
        conn.commit()
        
        return jsonify({
            'success': True,
            'data': {
                'id': result['id'],
                'user_id': g.user_data['user_id'],
                'username': g.user_data['username'],
                'display_name': g.user_data['username'],
                'content': sanitized_content,
                'room': room,
                'timestamp': result['timestamp'].isoformat()
            }
        })
        
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': 'Send failed'}), 500
    finally:
        conn.close()

@app.route('/chat/messages/<room>')
def get_messages(room):
    limit = min(request.args.get('limit', 50, type=int), 100)
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database error'}), 500
    
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.*, u.display_name 
            FROM messages m 
            JOIN users u ON m.user_id = u.id 
            WHERE m.room = %s 
            ORDER BY m.timestamp DESC 
            LIMIT %s
        ''', (room, limit))
        
        messages = cursor.fetchall()
        
        # Reverse to show oldest first
        messages.reverse()
        
        messages_list = []
        for msg in messages:
            messages_list.append({
                'id': msg['id'],
                'user_id': msg['user_id'],
                'username': msg['username'],
                'display_name': msg['display_name'],
                'content': msg['content'],
                'room': msg['room'],
                'timestamp': msg['timestamp'].isoformat()
            })
        
        return jsonify({
            'success': True,
            'data': messages_list
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': 'Failed to get messages'}), 500
    finally:
        conn.close()

@app.route('/chat/rooms')
def get_rooms():
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database error'}), 500
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT name, description FROM chat_rooms ORDER BY name')
        rooms = cursor.fetchall()
        
        return jsonify({
            'success': True,
            'data': [{'name': room['name'], 'description': room['description']} for room in rooms]
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': 'Failed to get rooms'}), 500
    finally:
        conn.close()

# Pinger to keep server awake
def keep_alive():
    while True:
        try:
            app_url = os.environ.get('RENDER_EXTERNAL_URL', '')
            if app_url:
                requests.get(f"{app_url}/health", timeout=10)
                print(f"✅ Pinged at {datetime.datetime.now()}")
        except Exception as e:
            print(f"❌ Pinger error: {e}")
        time.sleep(300)  # 5 minutes

# Start server
if __name__ == '__main__':
    print("🚀 Starting Text Messenger Server...")
    init_db()
    
    # Start pinger in background
    pinger_thread = threading.Thread(target=keep_alive, daemon=True)
    pinger_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    print(f"✅ Server running on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
