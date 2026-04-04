from flask import jsonify, request
import os
import time

from auth import token_required, role_required
from models import db_connection

from admin_blueprint import admin_bp


_dashboard_summary_cache = {
    'expires_at': 0.0,
    'data': None,
}


def _dashboard_cache_ttl_seconds() -> int:
    try:
        return max(0, int(os.getenv('ADMIN_DASHBOARD_CACHE_TTL', '30')))
    except Exception:
        return 30

@admin_bp.route('/users', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_all_users():
    """Get all users (admin only)"""
    try:
        # Get query parameters
        role = request.args.get('role')
        is_active = request.args.get('is_active')
        limit = request.args.get('limit', type=int, default=50)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        
        # Build query
        query = "SELECT id, email, name, phone, role, is_active, created_at, last_login_at FROM users WHERE 1=1"
        params = []
        
        if role:
            query += " AND role = %s"
            params.append(role)
        
        if is_active is not None:
            query += " AND is_active = %s"
            params.append(is_active.lower() == 'true')
        
        query += " ORDER BY created_at DESC"
        
        query += " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(query, params)
            users = cursor.fetchall()
        
        # Get total count
        count_query = "SELECT COUNT(*) as total FROM users WHERE 1=1"
        count_params = []
        if role:
            count_query += " AND role = %s"
            count_params.append(role)
        if is_active is not None:
            count_query += " AND is_active = %s"
            count_params.append(is_active.lower() == 'true')
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(count_query, count_params if count_params else None)
            count_result = cursor.fetchone()
            total = count_result['total'] if count_result else 0
        
        return jsonify({
            'success': True,
            'total': total,
            'count': len(users),
            'data': users
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/users/<user_id>', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_user_details(user_id):
    """Get detailed user information (admin only)"""
    try:
        # Get user
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT id, email, name, phone, avatar_url, role, address, language,
                       email_notifications, push_notifications, dark_mode, is_active,
                       created_at, last_login_at
                FROM users WHERE id = %s
            """, (user_id,))
            user = cursor.fetchone()
        
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Get role-specific profile
        profile = None
        with db_connection.get_cursor() as cursor:
            if user['role'] == 'CITIZEN':
                cursor.execute("""
                    SELECT cp.*, 
                           (SELECT COUNT(*) FROM user_badges WHERE user_id = cp.user_id) as badges_count,
                           (SELECT COUNT(*) FROM reports WHERE user_id = cp.user_id) as total_reports_count
                    FROM citizen_profiles cp
                    WHERE cp.user_id = %s
                """, (user_id,))
                profile = cursor.fetchone()
            elif user['role'] == 'CLEANER':
                cursor.execute("""
                    SELECT cp.*,
                           (SELECT COUNT(*) FROM cleanup_reviews WHERE cleaner_id = cp.user_id) as total_reviews,
                           (SELECT COUNT(*) FROM tasks WHERE cleaner_id = cp.user_id) as total_tasks_count
                    FROM cleaner_profiles cp
                    WHERE cp.user_id = %s
                """, (user_id,))
                profile = cursor.fetchone()
            elif user['role'] == 'ADMIN':
                cursor.execute("""
                    SELECT * FROM admin_profiles WHERE user_id = %s
                """, (user_id,))
                profile = cursor.fetchone()
        
        return jsonify({
            'success': True,
            'data': {
                'user': user,
                'profile': profile
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/stats', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_system_stats():
    """Get system-wide statistics (admin only)"""
    try:
        # Get comprehensive system stats
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT 
                    (SELECT COUNT(*) FROM users WHERE role = 'CITIZEN') as total_citizens,
                    (SELECT COUNT(*) FROM users WHERE role = 'CLEANER') as total_cleaners,
                    (SELECT COUNT(*) FROM users WHERE role = 'ADMIN') as total_admins,
                    (SELECT COUNT(*) FROM reports) as total_reports,
                    (SELECT COUNT(*) FROM reports WHERE status = 'SUBMITTED') as pending_reports,
                    (SELECT COUNT(*) FROM reports WHERE status = 'COMPLETED') as completed_reports,
                    (SELECT COUNT(*) FROM tasks) as total_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE status = 'APPROVED' AND cleaner_id IS NULL) as available_tasks,
                    (SELECT COUNT(*) FROM zones) as total_zones,
                    (SELECT AVG(cleanliness_score) FROM zones) as avg_zone_cleanliness,
                    (SELECT COUNT(*) FROM alerts WHERE status = 'OPEN') as open_alerts
            """)
            stats = cursor.fetchone()
        
        return jsonify({
            'success': True,
            'data': stats if stats else None
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/dashboard-summary', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_dashboard_summary():
    """Get lightweight dashboard aggregates for fast first paint."""
    try:
        now = time.time()
        ttl = _dashboard_cache_ttl_seconds()
        if ttl > 0 and _dashboard_summary_cache['data'] is not None and now < _dashboard_summary_cache['expires_at']:
            return jsonify({'success': True, 'data': _dashboard_summary_cache['data']}), 200

        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'SUBMITTED') AS submitted,
                    COUNT(*) FILTER (WHERE status = 'APPROVED') AS approved,
                    COUNT(*) FILTER (WHERE status = 'IN_PROGRESS') AS in_progress,
                    COUNT(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                    COUNT(*) FILTER (WHERE status = 'DECLINED') AS declined,
                    COUNT(*) FILTER (
                        WHERE status = 'SUBMITTED' AND severity IN ('HIGH', 'CRITICAL')
                    ) AS critical_pending
                FROM reports
            """)
            report_stats = cursor.fetchone() or {}

            cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'APPROVED' AND cleaner_id IS NULL) AS available,
                    COUNT(*) FILTER (WHERE status = 'IN_PROGRESS') AS in_progress,
                    COUNT(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                    COALESCE(SUM(reward), 0) AS total_rewards,
                    COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN reward ELSE 0 END), 0) AS paid_out
                FROM tasks
            """)
            task_stats = cursor.fetchone() or {}

            cursor.execute("""
                SELECT
                    z.id AS zone_id,
                    z.name AS zone_name,
                    z.cleanliness_score,
                    COALESCE(r.report_count, 0) AS reports
                FROM zones z
                LEFT JOIN (
                    SELECT zone_id, COUNT(*) AS report_count
                    FROM reports
                    GROUP BY zone_id
                ) r ON r.zone_id = z.id
                ORDER BY z.name ASC
            """)
            reports_by_zone = cursor.fetchall() or []

            cursor.execute("""
                SELECT
                    r.id,
                    r.zone_id,
                    r.description,
                    r.severity,
                    r.status,
                    r.created_at,
                    z.name AS zone_name,
                    u.name AS user_name
                FROM reports r
                LEFT JOIN zones z ON z.id = r.zone_id
                LEFT JOIN users u ON u.id = r.user_id
                WHERE r.status = 'SUBMITTED'
                ORDER BY r.created_at DESC
                LIMIT 5
            """)
            pending_reports = cursor.fetchall() or []

        payload = {
            'report_stats': report_stats,
            'task_stats': task_stats,
            'reports_by_zone': reports_by_zone,
            'pending_reports': pending_reports,
        }

        if ttl > 0:
            _dashboard_summary_cache['data'] = payload
            _dashboard_summary_cache['expires_at'] = now + ttl

        return jsonify({'success': True, 'data': payload}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/debug/pool-health', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_pool_health():
    """Get lightweight DB pool/session diagnostics for production debugging."""
    try:
        pool_stats = {}
        pool = db_connection.connection_pool
        if pool is not None and hasattr(pool, 'get_stats'):
            try:
                pool_stats = pool.get_stats() or {}
            except Exception as stats_error:
                pool_stats = {'stats_error': str(stats_error)}

        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE state = 'active') AS active_sessions,
                    COUNT(*) FILTER (WHERE state = 'idle') AS idle_sessions,
                    COUNT(*) FILTER (WHERE wait_event_type IS NOT NULL) AS waiting_sessions
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
            """)
            session_counts = cursor.fetchone() or {}

            cursor.execute("""
                SELECT
                    pid,
                    usename,
                    state,
                    wait_event_type,
                    wait_event,
                    now() - query_start AS running_for,
                    LEFT(query, 180) AS query
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state = 'active'
                ORDER BY query_start ASC
                LIMIT 10
            """)
            active_queries = cursor.fetchall() or []

        for row in active_queries:
            running_for = row.get('running_for')
            row['running_for'] = str(running_for) if running_for is not None else None

        return jsonify({
            'success': True,
            'data': {
                'pool_stats': pool_stats,
                'session_counts': session_counts,
                'active_queries': active_queries,
            }
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



