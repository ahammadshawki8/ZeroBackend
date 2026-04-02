"""
Authentication middleware and decorators
"""
from flask import request, jsonify
from functools import wraps
import jwt
import os
import time
from threading import Lock
from dotenv import load_dotenv

load_dotenv()

_auth_user_cache = {}
_auth_user_cache_lock = Lock()


def _cache_ttl_seconds() -> int:
    """Return cache TTL for auth user snapshots."""
    try:
        return max(0, int(os.getenv('AUTH_USER_CACHE_TTL', '30')))
    except Exception:
        return 30


def _get_cached_user(user_id: str):
    """Get cached user snapshot if still valid."""
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None

    now = time.time()
    with _auth_user_cache_lock:
        item = _auth_user_cache.get(user_id)
        if not item:
            return None
        expires_at, user = item
        if now > expires_at:
            _auth_user_cache.pop(user_id, None)
            return None
        return user


def _set_cached_user(user_id: str, user):
    """Store authenticated user snapshot briefly to cut DB reads under burst traffic."""
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return

    with _auth_user_cache_lock:
        _auth_user_cache[user_id] = (time.time() + ttl, user)


def invalidate_cached_user(user_id: str) -> None:
    """Remove a user snapshot from auth cache, used on logout/password reset flows."""
    if not user_id:
        return
    with _auth_user_cache_lock:
        _auth_user_cache.pop(user_id, None)


def _get_secret_key() -> str:
    """Load JWT secret from environment after dotenv is initialized."""
    secret_key = os.getenv('SECRET_KEY')
    if not secret_key:
        raise RuntimeError('Missing required SECRET_KEY environment variable. Set it in ZeroBackend/.env')
    return secret_key


def token_required(f):
    """Decorator to protect routes with JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Import here to avoid circular import
        from models import db_connection
        from db_helper import Model
        users_model = Model(db_connection, 'users')
        
        token = None

        # Get token from header
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]  # Bearer <token>
            except IndexError:
                return jsonify({'success': False, 'error': 'Invalid token format'}), 401
        
        if not token:
            return jsonify({'success': False, 'error': 'Token is missing'}), 401
        
        try:
            # Decode token
            secret_key = _get_secret_key()
            data = jwt.decode(token, secret_key, algorithms=["HS256"])
            user_id = data.get('user_id')
            if not user_id:
                return jsonify({'success': False, 'error': 'Invalid token payload'}), 401

            current_user = _get_cached_user(user_id)
            if not current_user:
                current_user = users_model.find_by_id(user_id)
                if current_user:
                    _set_cached_user(user_id, current_user)
            
            if not current_user:
                return jsonify({'success': False, 'error': 'User not found'}), 401
            
            # Add user to request context
            request.current_user = current_user

            return f(*args, **kwargs)
            
        except jwt.ExpiredSignatureError:
            return jsonify({'success': False, 'error': 'Token has expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        except Exception as e:
            # Protect API routes from raw DB/connection failures during auth lookup.
            print(f"Token validation failed due to backend error: {e}")
            return jsonify({'success': False, 'error': 'Authentication service temporarily unavailable'}), 503
    
    return decorated


def role_required(*roles):
    """Decorator to check if user has required role"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(request, 'current_user'):
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
            
            if request.current_user['role'] not in roles:
                return jsonify({'success': False, 'error': 'Insufficient permissions'}), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def superadmin_required(f):
    """Decorator to ensure current user is marked as superadmin."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not hasattr(request, 'current_user'):
            return jsonify({'success': False, 'error': 'Authentication required'}), 401

        if request.current_user.get('role') != 'ADMIN' or not request.current_user.get('is_superadmin', False):
            return jsonify({'success': False, 'error': 'Superadmin permissions required'}), 403

        return f(*args, **kwargs)

    return decorated_function
