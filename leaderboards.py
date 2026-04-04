from flask import Blueprint, jsonify, request
from auth import token_required, role_required
from models import db_connection
from datetime import datetime
import os
import time

leaderboards_bp = Blueprint('leaderboards', __name__)


_leaderboard_cache = {}


def _leaderboard_cache_ttl_seconds() -> int:
    try:
        return max(0, int(os.getenv('LEADERBOARD_CACHE_TTL', '45')))
    except Exception:
        return 45


def _read_cached_leaderboard(cache_key):
    entry = _leaderboard_cache.get(cache_key)
    if not entry:
        return None
    if time.time() >= entry['expires_at']:
        _leaderboard_cache.pop(cache_key, None)
        return None
    return entry['value']


def _store_cached_leaderboard(cache_key, value):
    ttl = _leaderboard_cache_ttl_seconds()
    if ttl <= 0:
        return
    _leaderboard_cache[cache_key] = {
        'expires_at': time.time() + ttl,
        'value': value,
    }


@leaderboards_bp.route('/leaderboards/citizens', methods=['GET'])
@token_required
def get_citizen_leaderboard():
    """Get citizen leaderboard rankings"""
    try:
        # Get query parameters
        period = request.args.get('period', default='all_time')
        limit = request.args.get('limit', type=int, default=10)
        
        # Validate period
        valid_periods = ['all_time', 'month', 'week']
        if period not in valid_periods:
            return jsonify({'success': False, 'error': f'Period must be one of: {", ".join(valid_periods)}'}), 400

        if limit < 1:
            limit = 10
        if limit > 100:
            limit = 100

        cache_key = ('citizens', period, limit)
        cached_payload = _read_cached_leaderboard(cache_key)
        if cached_payload is not None:
            return jsonify({'success': True, 'period': period, 'data': cached_payload}), 200

        date_filter_sql = ""
        if period == 'month':
            date_filter_sql = "AND gpt.created_at >= DATE_TRUNC('month', CURRENT_DATE)"
        elif period == 'week':
            date_filter_sql = "AND gpt.created_at >= DATE_TRUNC('week', CURRENT_DATE)"
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(f"""
                WITH points_by_user AS (
                    SELECT gpt.user_id, COALESCE(SUM(gpt.green_points), 0) AS total_green_points
                    FROM green_points_transactions gpt
                    WHERE 1=1 {date_filter_sql}
                    GROUP BY gpt.user_id
                ),
                approved_by_user AS (
                    SELECT r.user_id,
                           COUNT(*) FILTER (WHERE r.status IN ('APPROVED', 'COMPLETED')) AS approved_reports
                    FROM reports r
                    GROUP BY r.user_id
                ),
                badges_by_user AS (
                    SELECT ub.user_id, COUNT(*) AS badges_count
                    FROM user_badges ub
                    GROUP BY ub.user_id
                ),
                ranked AS (
                    SELECT
                        u.id AS user_id,
                        u.name AS user_name,
                        u.avatar_url,
                        COALESCE(p.total_green_points, 0) AS total_green_points,
                        COALESCE(a.approved_reports, 0) AS approved_reports,
                        COALESCE(b.badges_count, 0) AS badges_count,
                        ROW_NUMBER() OVER (
                            ORDER BY COALESCE(p.total_green_points, 0) DESC,
                                     COALESCE(a.approved_reports, 0) DESC,
                                     u.created_at ASC
                        ) AS rank
                    FROM users u
                    JOIN citizen_profiles cp ON cp.user_id = u.id
                    LEFT JOIN points_by_user p ON p.user_id = u.id
                    LEFT JOIN approved_by_user a ON a.user_id = u.id
                    LEFT JOIN badges_by_user b ON b.user_id = u.id
                    WHERE u.is_active = true
                )
                SELECT rank, user_id, user_name, avatar_url, total_green_points, approved_reports, badges_count
                FROM ranked
                ORDER BY rank
                LIMIT %s
            """, (limit,))
            leaderboard = cursor.fetchall()

        _store_cached_leaderboard(cache_key, leaderboard)
        
        return jsonify({
            'success': True,
            'period': period,
            'data': leaderboard
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@leaderboards_bp.route('/leaderboards/cleaners', methods=['GET'])
@token_required
def get_cleaner_leaderboard():
    """Get cleaner leaderboard rankings"""
    try:
        # Get query parameters
        period = request.args.get('period', default='all_time')
        limit = request.args.get('limit', type=int, default=10)
        
        # Validate period
        valid_periods = ['all_time', 'month', 'week']
        if period not in valid_periods:
            return jsonify({'success': False, 'error': f'Period must be one of: {", ".join(valid_periods)}'}), 400

        if limit < 1:
            limit = 10
        if limit > 100:
            limit = 100

        cache_key = ('cleaners', period, limit)
        cached_payload = _read_cached_leaderboard(cache_key)
        if cached_payload is not None:
            return jsonify({'success': True, 'period': period, 'data': cached_payload}), 200

        date_filter_sql = ""
        if period == 'month':
            date_filter_sql = "AND et.created_at >= DATE_TRUNC('month', CURRENT_DATE)"
        elif period == 'week':
            date_filter_sql = "AND et.created_at >= DATE_TRUNC('week', CURRENT_DATE)"
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(f"""
                WITH earnings_by_user AS (
                    SELECT et.cleaner_id AS user_id,
                           COALESCE(SUM(et.amount), 0) AS total_earnings
                    FROM earnings_transactions et
                    WHERE et.status = 'PAID' {date_filter_sql}
                    GROUP BY et.cleaner_id
                ),
                completed_by_user AS (
                    SELECT t.cleaner_id AS user_id,
                           COUNT(*) FILTER (WHERE t.status = 'COMPLETED') AS completed_tasks
                    FROM tasks t
                    GROUP BY t.cleaner_id
                ),
                ranked AS (
                    SELECT
                        u.id AS user_id,
                        u.name AS user_name,
                        u.avatar_url,
                        COALESCE(e.total_earnings, 0) AS total_earnings,
                        COALESCE(c.completed_tasks, 0) AS completed_tasks,
                        COALESCE(cp.rating, 0) AS rating,
                        ROW_NUMBER() OVER (
                            ORDER BY COALESCE(e.total_earnings, 0) DESC,
                                     COALESCE(c.completed_tasks, 0) DESC,
                                     COALESCE(cp.rating, 0) DESC,
                                     u.created_at ASC
                        ) AS rank
                    FROM users u
                    JOIN cleaner_profiles cp ON cp.user_id = u.id
                    LEFT JOIN earnings_by_user e ON e.user_id = u.id
                    LEFT JOIN completed_by_user c ON c.user_id = u.id
                    WHERE u.is_active = true
                )
                SELECT rank, user_id, user_name, avatar_url, total_earnings, completed_tasks, rating
                FROM ranked
                ORDER BY rank
                LIMIT %s
            """, (limit,))
            leaderboard = cursor.fetchall()
        
        # Convert earnings to float
        for entry in leaderboard:
            entry['total_earnings'] = float(entry['total_earnings'])
            entry['rating'] = float(entry['rating'])

        _store_cached_leaderboard(cache_key, leaderboard)
        
        return jsonify({
            'success': True,
            'period': period,
            'data': leaderboard
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@leaderboards_bp.route('/admin/leaderboards/recalculate', methods=['POST'])
@token_required
@role_required('ADMIN')
def recalculate_leaderboards():
    """Recalculate leaderboards (admin only)"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Validate required fields
        if not data.get('type') or not data.get('period'):
            return jsonify({'success': False, 'error': 'type and period are required'}), 400
        
        # Validate type and period
        valid_types = ['citizens', 'cleaners', 'both']
        valid_periods = ['all_time', 'month', 'week']
        
        if data['type'] not in valid_types:
            return jsonify({'success': False, 'error': f'Type must be one of: {", ".join(valid_types)}'}), 400
        
        if data['period'] not in valid_periods:
            return jsonify({'success': False, 'error': f'Period must be one of: {", ".join(valid_periods)}'}), 400
        
        with db_connection.get_cursor(commit=True) as cursor:
            recalculated = []
            
            if data['type'] in ['citizens', 'both']:
                # Recalculate citizen leaderboard
                cursor.execute("""
                    CALL sp_recalculate_citizen_leaderboard(%s)
                """, (data['period'],))
                recalculated.append(f"citizen_{data['period']}")
            
            if data['type'] in ['cleaners', 'both']:
                # Recalculate cleaner leaderboard
                cursor.execute("""
                    CALL sp_recalculate_cleaner_leaderboard(%s)
                """, (data['period'],))
                recalculated.append(f"cleaner_{data['period']}")

        _leaderboard_cache.clear()
        
        return jsonify({
            'success': True,
            'message': 'Leaderboards recalculated successfully',
            'data': {
                'recalculated': recalculated,
                'timestamp': datetime.now().isoformat()
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500