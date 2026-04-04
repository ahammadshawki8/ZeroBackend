from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
from models import db_connection, Model, setup_database
from dotenv import load_dotenv
import os
import importlib
import bcrypt
import jwt
import json
from urllib.parse import urlsplit
from threading import Lock
from auth import token_required, role_required, superadmin_required, invalidate_cached_user
from superadmin_routes import ensure_default_superadmin

load_dotenv()

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')

if not app.config['SECRET_KEY']:
    raise RuntimeError('Missing required SECRET_KEY environment variable. Set it in ZeroBackend/.env')

# Configure CORS
frontend_origins_env = os.getenv('FRONTEND_ORIGINS', '').strip()

def _normalize_origin(origin: str) -> str:
    """Normalize origin format for strict CORS matching."""
    candidate = origin.strip().strip('"').strip("'").rstrip('/')
    if not candidate:
        return ''

    try:
        parsed = urlsplit(candidate)
        if not parsed.scheme or not parsed.netloc:
            return candidate
        # Normalize scheme/host casing and keep explicit port if present.
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    except Exception:
        return candidate


def _is_allowed_origin(origin: str) -> bool:
    """Check whether an origin should receive CORS headers."""
    normalized = _normalize_origin(origin)
    if not normalized:
        return False
    if normalized in allowed_origins_set:
        return True

    # Allow Vercel-hosted frontends by default unless explicitly disabled.
    allow_vercel = os.getenv('ALLOW_VERCEL_ORIGINS', 'true').lower() == 'true'
    if allow_vercel and normalized.startswith('https://') and normalized.endswith('.vercel.app'):
        return True

    return False

if frontend_origins_env:
    allowed_origins = [
        normalized
        for normalized in (_normalize_origin(origin) for origin in frontend_origins_env.split(','))
        if normalized
    ]
else:
    allowed_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]

allowed_origins_set = set(allowed_origins)

_login_request_locks = {}
_login_request_locks_lock = Lock()


def _acquire_login_request_lock(email: str) -> bool:
    """Allow only one in-flight login attempt per email in this worker."""
    normalized_email = email.strip().lower()
    with _login_request_locks_lock:
        lock = _login_request_locks.get(normalized_email)
        if lock is None:
            lock = Lock()
            _login_request_locks[normalized_email] = lock

    return lock.acquire(blocking=False)


def _release_login_request_lock(email: str) -> None:
    normalized_email = email.strip().lower()
    with _login_request_locks_lock:
        lock = _login_request_locks.get(normalized_email)

    if not lock:
        return

    try:
        lock.release()
    except RuntimeError:
        return

CORS(app, 
     resources={r"/api/*": {"origins": allowed_origins}},
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
     allow_headers=['Content-Type', 'Authorization'],
     supports_credentials=True)


@app.before_request
def handle_api_preflight():
    """Ensure API preflight requests always receive a concrete response."""
    if request.method == 'OPTIONS' and request.path.startswith('/api/'):
        return app.make_default_options_response()


@app.after_request
def add_cors_headers(response):
    """Fallback CORS headers for API routes to avoid preflight mismatches in production proxies."""
    origin = request.headers.get('Origin', '').strip()
    normalized_origin = _normalize_origin(origin) if origin else ''

    if request.path.startswith('/api/') and _is_allowed_origin(normalized_origin):
        response.headers['Access-Control-Allow-Origin'] = normalized_origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'

    return response

# Initialize database connection pool (schema assumed to be pre-initialized)
db_connection.create_pool()

if os.getenv('BOOTSTRAP_SUPERADMIN_ON_START', 'true').lower() == 'true':
    try:
        ensure_default_superadmin()
    except Exception as exc:
        print(f"⚠ Superadmin bootstrap skipped during startup: {exc}")

# Create model instances
users_model = Model(db_connection, 'users')
citizen_profiles_model = Model(db_connection, 'citizen_profiles')
cleaner_profiles_model = Model(db_connection, 'cleaner_profiles')
admin_profiles_model = Model(db_connection, 'admin_profiles')

# Route modules attach endpoints onto blueprint objects via decorators.
ROUTE_MODULES = [
        # Citizen route modules
        'citizen_profile_routes',
        'citizen_report_routes',
        'citizen_engagement_routes',
        'citizen_notification_routes',
        'citizen_account_routes',

        # Cleaner route modules
        'cleaner_profile_routes',
        'cleaner_task_routes',
        'cleaner_payment_routes',
        'cleaner_community_routes',
        
        # Admin route modules
        'admin_profile_routes',
        'admin_management_routes',
        'admin_report_routes',
]


def load_required_route_modules():
    """Import route modules so decorators attach endpoints to blueprint objects."""
    for module_name in ROUTE_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            raise RuntimeError(f"Failed to load route module '{module_name}': {exc}") from exc


load_required_route_modules()

# Every registered blueprint now follows one module+symbol+prefix pattern.
BLUEPRINT_SPECS = [
    ('citizen_blueprint', 'citizen_bp', '/api/citizen'),
    ('admin_blueprint', 'admin_bp', '/api/admin'),
    ('cleaner_blueprint', 'cleaner_bp', '/api/cleaner'),
    ('admin_tasks', 'admin_tasks_bp', '/api/admin'),
    ('admin_zones', 'admin_zones_bp', '/api/admin'),
    ('admin_payments', 'admin_payments_bp', '/api/admin'),
    ('shared_endpoints', 'shared_bp', '/api'),
    ('notifications', 'notifications_bp', '/api'),
    ('leaderboards', 'leaderboards_bp', '/api'),
    ('ai_analysis', 'ai_bp', '/api'),
    ('superadmin_routes', 'superadmin_bp', '/api'),
]


def load_and_register_blueprints(flask_app: Flask):
    """Import blueprint symbols from modules and register them with prefixes."""
    for module_name, blueprint_attr, prefix in BLUEPRINT_SPECS:
        try:
            module = importlib.import_module(module_name)
            blueprint = getattr(module, blueprint_attr)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load blueprint '{blueprint_attr}' from '{module_name}': {exc}"
            ) from exc

        flask_app.register_blueprint(blueprint, url_prefix=prefix)


load_and_register_blueprints(app)


# Basic Routes
@app.route('/')
def home():
    """API documentation endpoint."""
    return jsonify({
        "message": "Zero Waste Management System API",
        "version": "1.0.0",
        "database": "PostgreSQL with 3NF normalization",
        "total_endpoints": 63,
        "endpoints": {
            "auth": {
                "POST /api/auth/register": "Register new user",
                "POST /api/auth/login": "Login user",
                "GET /api/auth/me": "Get current user profile",
                "POST /api/auth/logout": "Logout user"
            },
            "citizen": {
                "GET /api/citizen/profile": "Get citizen profile",
                "PUT /api/citizen/profile": "Update citizen profile",
                "GET /api/citizen/stats": "Get citizen statistics",
                "POST /api/citizen/reports": "Submit waste report",
                "GET /api/citizen/reports": "Get my reports",
                "GET /api/citizen/reports/<id>": "Get report details",
                "POST /api/citizen/reports/<id>/review": "Submit cleanup review",
                "GET /api/citizen/badges": "Get my badges",
                "GET /api/citizen/points": "Get points history",
                "GET /api/citizen/leaderboard": "Get citizen leaderboard",
                "GET /api/citizen/notifications": "Get notifications"
            },
            "cleaner": {
                "GET /api/cleaner/profile": "Get cleaner profile",
                "PUT /api/cleaner/profile": "Update cleaner profile",
                "GET /api/cleaner/stats": "Get cleaner statistics",
                "GET /api/cleaner/tasks/available": "Get available tasks",
                "POST /api/cleaner/tasks/<id>/take": "Take task",
                "GET /api/cleaner/tasks": "Get my tasks",
                "POST /api/cleaner/tasks/<id>/complete": "Complete task",
                "GET /api/cleaner/tasks/<id>": "Get task details",
                "GET /api/cleaner/earnings": "Get earnings history",
                "GET /api/cleaner/reviews": "Get reviews",
                "GET /api/cleaner/leaderboard": "Get cleaner leaderboard"
            },
            "admin": {
                "GET /api/admin/profile": "Get admin profile",
                "PUT /api/admin/profile": "Update admin profile",
                "GET /api/admin/users": "Get all users",
                "GET /api/admin/users/<id>": "Get user details",
                "GET /api/admin/stats": "Get system stats",
                "GET /api/admin/reports/pending": "Get pending reports",
                "POST /api/admin/reports/<id>/approve": "Approve report",
                "POST /api/admin/reports/<id>/decline": "Decline report",
                "GET /api/admin/reports": "Get all reports",
                "GET /api/admin/tasks": "Get all tasks",
                "POST /api/admin/tasks": "Create manual task",
                "PUT /api/admin/tasks/<id>": "Update task",
                "DELETE /api/admin/tasks/<id>": "Delete task",
                "GET /api/admin/zones": "Get zones",
                "POST /api/admin/zones": "Create zone",
                "PUT /api/admin/zones/<id>": "Update zone",
                "GET /api/admin/zones/<id>": "Get zone details",
                "DELETE /api/admin/zones/<id>": "Delete zone",
                "GET /api/admin/payments/pending": "Get pending payments",
                "POST /api/admin/payments/process": "Process payments"
            },
            "ai": {
                "POST /api/ai/analyze-waste": "Analyze waste image",
                "POST /api/ai/compare-cleanup": "Compare before/after images",
                "POST /api/ai/analyze-report/<id>": "Analyze existing report"
            },
            "notifications": {
                "GET /api/notifications": "Get notifications (all roles)",
                "PUT /api/notifications/<id>/read": "Mark notification read",
                "PUT /api/notifications/read-all": "Mark all notifications read",
                "POST /api/admin/notifications/bulk": "Send bulk notification (admin)"
            },
            "leaderboards": {
                "GET /api/leaderboards/citizens": "Get citizen leaderboard",
                "GET /api/leaderboards/cleaners": "Get cleaner leaderboard",
                "POST /api/admin/leaderboards/recalculate": "Recalculate leaderboards (admin)"
            },
            "shared": {
                "GET /api/zones": "Get all zones",
                "GET /api/zones/by-location": "Find zone by coordinates",
                "GET /api/zones/<id>/stats": "Get zone statistics",
                "GET /api/reports/<id>": "Get report details (any role)",
                "GET /api/tasks/<id>": "Get task details (any role)"
            },
            "health": {
                "GET /api/health": "Check API health"
            }
        }
    })


@app.route('/api/health')
def health():
    """Check if API and database are running."""
    try:
        with db_connection.get_cursor() as cursor:
            cursor.execute("SELECT 1")
        db_status = "connected"
    except:
        db_status = "disconnected"
    
    return jsonify({
        "status": "healthy",
        "database": db_status,
        "timestamp": datetime.now().isoformat()
    }), 200



# Authentication Routes
@app.route('/api/auth/register', methods=['POST'])
def register():
    """
    Register a new user
    Required JSON body:
        - email: User's email
        - password: User's password
        - name: User's name
        - role: User role (CITIZEN, CLEANER, ADMIN)
        - phone: User's phone (optional)
    """
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['email', 'password', 'name', 'role']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'{field} is required'}), 400
        
        # Validate role
        valid_roles = ['CITIZEN', 'CLEANER', 'ADMIN']
        if data['role'] not in valid_roles:
            return jsonify({'success': False, 'error': f'Role must be one of: {", ".join(valid_roles)}'}), 400
        
        # Check if email already exists
        existing = users_model.execute_raw(
            "SELECT id FROM users WHERE email = %s",
            [data['email']]
        )
        
        if existing:
            return jsonify({'success': False, 'error': 'Email already exists'}), 409
        
        # Hash password
        password_hash = bcrypt.hashpw(data['password'].encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        # Create user
        user_data = {
            'email': data['email'],
            'password_hash': password_hash,
            'name': data['name'],
            'role': data['role'],
            'phone': data.get('phone'),
            'is_active': True
        }
        
        new_user = users_model.create(user_data)
        
        # Remove password hash from response
        new_user.pop('password_hash', None)
        
        return jsonify({
            'success': True,
            'message': 'User registered successfully',
            'data': new_user
        }), 201
    
    except Exception as e:
        error_text = str(e)
        if 'couldn\'t get a connection' in error_text.lower():
            return jsonify({'success': False, 'error': 'Authentication service temporarily unavailable'}), 503
        return jsonify({'success': False, 'error': error_text}), 500


@app.route('/api/auth/login', methods=['POST'])
def login():
    """
    Login user
    Required JSON body:
        - email: User's email
        - password: User's password
    """
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Validate required fields
        if not data.get('email') or not data.get('password'):
            return jsonify({'success': False, 'error': 'Email and password are required'}), 400

        if not _acquire_login_request_lock(data['email']):
            return jsonify({
                'success': False,
                'error': 'A login attempt is already in progress for this account'
            }), 429

        try:
            # Find user by email
            users = users_model.execute_raw(
                "SELECT * FROM users WHERE email = %s",
                [data['email']]
            )
        
            if not users:
                return jsonify({'success': False, 'error': 'Invalid email or password'}), 401
            
            user = users[0]
            
            # Check if user is active
            if not user.get('is_active'):
                return jsonify({'success': False, 'error': 'Account is deactivated'}), 401
            
            # Verify password
            if not bcrypt.checkpw(data['password'].encode('utf-8'), user['password_hash'].encode('utf-8')):
                return jsonify({'success': False, 'error': 'Invalid email or password'}), 401
            
            # Update last login
            users_model.execute_raw(
                "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s",
                [user['id']],
                commit=True
            )
            
            # Generate JWT token
            token = jwt.encode({
                'user_id': user['id'],
                'email': user['email'],
                'role': user['role'],
                'name': user.get('name'),
                'is_active': user.get('is_active', True),
                'is_superadmin': user.get('is_superadmin', False),
            }, app.config['SECRET_KEY'], algorithm="HS256")
            
            # Remove password hash from response
            user.pop('password_hash', None)
            
            return jsonify({
                'success': True,
                'message': 'Login successful',
                'token': token,
                'user': user
            }), 200
        finally:
            _release_login_request_lock(data['email'])
    
    except Exception as e:
        error_text = str(e)
        if 'couldn\'t get a connection' in error_text.lower():
            return jsonify({'success': False, 'error': 'Authentication service temporarily unavailable'}), 503
        return jsonify({'success': False, 'error': error_text}), 500


@app.route('/api/auth/me', methods=['GET'])
@token_required
def get_current_user():
    """Get current authenticated user profile"""
    try:
        user = request.current_user.copy()
        user.pop('password_hash', None)
        
        # Get role-specific profile
        profile = None
        if user['role'] == 'CITIZEN':
            profiles = citizen_profiles_model.execute_raw(
                "SELECT * FROM citizen_profiles WHERE user_id = %s",
                [user['id']]
            )
            profile = profiles[0] if profiles else None
        elif user['role'] == 'CLEANER':
            profiles = cleaner_profiles_model.execute_raw(
                "SELECT * FROM cleaner_profiles WHERE user_id = %s",
                [user['id']]
            )
            profile = profiles[0] if profiles else None
        elif user['role'] == 'ADMIN':
            profiles = admin_profiles_model.execute_raw(
                "SELECT * FROM admin_profiles WHERE user_id = %s",
                [user['id']]
            )
            profile = profiles[0] if profiles else None
        
        return jsonify({
            'success': True,
            'data': {
                'user': user,
                'profile': profile
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
@token_required
def logout():
    """Logout current user by clearing server-side auth cache snapshot."""
    try:
        user_id = request.current_user.get('id')
        invalidate_cached_user(user_id)

        return jsonify({
            'success': True,
            'message': 'Logged out successfully'
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({
        "success": False,
        "error": "Resource not found"
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return jsonify({
        "success": False,
        "error": "Internal server error"
    }), 500


@app.teardown_appcontext
def cleanup(error):
    """Cleanup resources."""
    pass


if __name__ == '__main__':
    print("\n" + "="*70)
    print("Zero Waste Management System API")
    print("="*70)
    if os.getenv('RERUN_MIGRATIONS', 'true').lower() == 'true':
        print("\nSetting up database...")
        setup_database()
    else:
        print("\nDatabase already setup...")
    print("\nAvailable endpoints:")
    print("  POST   http://127.0.0.1:5000/api/auth/register")
    print("  POST   http://127.0.0.1:5000/api/auth/login")
    print("  GET    http://127.0.0.1:5000/api/auth/me")
    print("  GET    http://127.0.0.1:5000/api/citizen/profile")
    print("  GET    http://127.0.0.1:5000/api/cleaner/profile")
    print("  GET    http://127.0.0.1:5000/api/admin/profile")
    print("  GET    http://127.0.0.1:5000/api/health")
    print("\nTip: Use Postman, curl, or Thunder Client to test the API")
    print("="*70 + "\n")
    
    try:
        app.run(debug=True, port=5000)
    finally:
        db_connection.close_pool()
