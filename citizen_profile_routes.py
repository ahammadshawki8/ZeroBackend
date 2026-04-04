from flask import jsonify, request
from datetime import datetime
import os
import time

from auth import token_required, role_required
from models import db_connection

from citizen_blueprint import citizen_bp


_citizen_rank_cache = {}


def _rank_cache_ttl_seconds() -> int:
    try:
        return max(0, int(os.getenv('CITIZEN_PROFILE_RANK_CACHE_TTL', '45')))
    except Exception:
        return 45


def _get_cached_rank(user_id):
    entry = _citizen_rank_cache.get(user_id)
    if not entry:
        return None
    if time.time() >= entry['expires_at']:
        _citizen_rank_cache.pop(user_id, None)
        return None
    return entry['rank']


def _set_cached_rank(user_id, rank):
    ttl = _rank_cache_ttl_seconds()
    if ttl <= 0:
        return
    _citizen_rank_cache[user_id] = {
        'rank': rank,
        'expires_at': time.time() + ttl,
    }

@citizen_bp.route('/profile', methods=['GET'])
@token_required
@role_required('CITIZEN')
def get_profile():
    """Get citizen profile with user details"""
    try:
        user = request.current_user.copy()
        user.pop('password_hash', None)
        
        # Get citizen profile with comprehensive data
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                SELECT cp.*, 
                       (SELECT COUNT(*) FROM user_badges WHERE user_id = cp.user_id) as badges_count,
                       (SELECT COUNT(*) FROM reports WHERE user_id = cp.user_id) as total_reports_count
                FROM citizen_profiles cp
                WHERE cp.user_id = %s
            """, (user['id'],))
            profile = cursor.fetchone()
            
            # If no citizen profile exists, create one
            if not profile:
                cursor.execute("""
                    INSERT INTO citizen_profiles (user_id, green_points_balance, total_reports, approved_reports, current_streak, longest_streak)
                    VALUES (%s, 0, 0, 0, 0, 0)
                    RETURNING *
                """, (user['id'],))
                profile = cursor.fetchone()
                # Add the counts that would be 0 for a new profile
                profile['badges_count'] = 0
                profile['total_reports_count'] = 0
            
            # Get badges
            cursor.execute("""
                SELECT 
                    b.id, b.name, b.description, b.icon,
                    ub.earned_at
                FROM user_badges ub
                JOIN badges b ON ub.badge_id = b.id
                WHERE ub.user_id = %s
                ORDER BY ub.earned_at DESC
            """, (user['id'],))
            badges = cursor.fetchall()
            
            # Convert earned_at to ISO format
            for badge in badges:
                if badge['earned_at']:
                    badge['earned_at'] = badge['earned_at'].isoformat()

        # Compute live rank in a separate short read and cache briefly to avoid repeated heavy scans.
        live_rank = _get_cached_rank(user['id'])
        if live_rank is None:
            with db_connection.get_cursor() as cursor:
                cursor.execute("""
                    SELECT ranked.rank
                    FROM (
                        SELECT cp.user_id,
                               ROW_NUMBER() OVER (
                                   ORDER BY cp.green_points_balance DESC,
                                            cp.approved_reports DESC,
                                            cp.total_reports DESC
                               ) AS rank
                        FROM citizen_profiles cp
                        JOIN users u ON u.id = cp.user_id
                        WHERE u.is_active = true
                    ) ranked
                    WHERE ranked.user_id = %s
                """, (user['id'],))
                rank_result = cursor.fetchone()
                live_rank = rank_result['rank'] if rank_result else None
                _set_cached_rank(user['id'], live_rank)
        
        # Flatten the response to match frontend expectations
        flattened_profile = {
            'userId': user['id'],
            'name': user['name'],
            'email': user['email'],
            'phone': user['phone'],
            'address': user['address'],
            'avatarUrl': user['avatar_url'],
            'greenPoints': profile['green_points_balance'] if profile else 0,
            'totalReports': profile['total_reports'] if profile else 0,
            'approvedReports': profile['approved_reports'] if profile else 0,
            'currentStreak': profile['current_streak'] if profile else 0,
            'longestStreak': profile['longest_streak'] if profile else 0,
            'rank': live_rank,
            'badges': badges,
            'createdAt': user['created_at'].isoformat() if user.get('created_at') else None,
            'joinedAt': profile['created_at'].isoformat() if profile and profile.get('created_at') else None,
            'notificationSettings': {
                'reportUpdates': user.get('notify_report_updates', True),
                'promotions': user.get('notify_news_updates', False),
            }
        }
        
        return jsonify({
            'success': True,
            'data': flattened_profile
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/profile', methods=['PUT'])
@token_required
@role_required('CITIZEN')
def update_profile():
    """Update citizen user details (name, phone, avatar, settings)"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        user_id = request.current_user['id']
        
        # Prepare update data for users table
        user_update = {}
        allowed_fields = ['name', 'phone', 'avatar_url', 'address', 'language', 
                         'email_notifications', 'push_notifications', 'dark_mode']
        
        for field in allowed_fields:
            if field in data:
                user_update[field] = data[field]
        
        if not user_update:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400
        
        # Update user
        with db_connection.get_cursor(commit=True) as cursor:
            # Build dynamic update query
            set_clause = ', '.join([f"{field} = %s" for field in user_update.keys()])
            values = list(user_update.values()) + [user_id]
            
            cursor.execute(f"""
                UPDATE users 
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING id, email, name, phone, avatar_url, role, address, language,
                         email_notifications, push_notifications, dark_mode, is_active, created_at
            """, values)
            updated_user = cursor.fetchone()
            
            # Get updated profile
            cursor.execute("""
                SELECT cp.*, 
                       (SELECT COUNT(*) FROM user_badges WHERE user_id = cp.user_id) as badges_count,
                       (SELECT COUNT(*) FROM reports WHERE user_id = cp.user_id) as total_reports_count
                FROM citizen_profiles cp
                WHERE cp.user_id = %s
            """, (user_id,))
            profile = cursor.fetchone()
            
            # If no citizen profile exists, create one
            if not profile:
                cursor.execute("""
                    INSERT INTO citizen_profiles (user_id, green_points_balance, total_reports, approved_reports, current_streak, longest_streak)
                    VALUES (%s, 0, 0, 0, 0, 0)
                    RETURNING *
                """, (user_id,))
                profile = cursor.fetchone()
                # Add the counts that would be 0 for a new profile
                profile['badges_count'] = 0
                profile['total_reports_count'] = 0
            
            # Get badges
            cursor.execute("""
                SELECT 
                    b.id, b.name, b.description, b.icon,
                    ub.earned_at
                FROM user_badges ub
                JOIN badges b ON ub.badge_id = b.id
                WHERE ub.user_id = %s
                ORDER BY ub.earned_at DESC
            """, (user_id,))
            badges = cursor.fetchall()
            
            # Convert earned_at to ISO format
            for badge in badges:
                if badge['earned_at']:
                    badge['earned_at'] = badge['earned_at'].isoformat()
        
        # Flatten the response to match frontend expectations
        flattened_profile = {
            'userId': updated_user['id'],
            'name': updated_user['name'],
            'email': updated_user['email'],
            'phone': updated_user['phone'],
            'address': updated_user['address'],
            'avatarUrl': updated_user['avatar_url'],
            'greenPoints': profile['green_points_balance'] if profile else 0,
            'totalReports': profile['total_reports'] if profile else 0,
            'approvedReports': profile['approved_reports'] if profile else 0,
            'currentStreak': profile['current_streak'] if profile else 0,
            'longestStreak': profile['longest_streak'] if profile else 0,
            'rank': profile['rank'] if profile else None,
            'badges': badges,
            'createdAt': updated_user['created_at'].isoformat() if updated_user.get('created_at') else None,
            'joinedAt': profile['created_at'].isoformat() if profile and profile.get('created_at') else None,
            'notificationSettings': {
                'reportUpdates': updated_user.get('notify_report_updates', True),
                'promotions': updated_user.get('notify_news_updates', False),
            }
        }
        
        return jsonify({
            'success': True,
            'message': 'Profile updated successfully',
            'data': flattened_profile
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/stats', methods=['GET'])
@token_required
@role_required('CITIZEN')
def get_stats():
    """Get citizen statistics"""
    try:
        user_id = request.current_user['id']
        
        # Get comprehensive stats
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT 
                    cp.*,
                    (SELECT COUNT(*) FROM reports WHERE user_id = %s AND status = 'SUBMITTED') as pending_reports,
                    (SELECT COUNT(*) FROM reports WHERE user_id = %s AND status = 'COMPLETED') as completed_reports,
                    (SELECT COUNT(*) FROM user_badges WHERE user_id = %s) as total_badges,
                    (SELECT COUNT(*) FROM cleanup_reviews WHERE citizen_id = %s) as reviews_given
                FROM citizen_profiles cp
                WHERE cp.user_id = %s
            """, (user_id, user_id, user_id, user_id, user_id))
            stats = cursor.fetchone()
        
        return jsonify({
            'success': True,
            'data': stats
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


