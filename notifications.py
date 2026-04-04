from datetime import timezone

from flask import Blueprint, jsonify, request
from auth import token_required, role_required
from models import db_connection

notifications_bp = Blueprint('notifications_api', __name__)


def _to_utc_iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


@notifications_bp.route('/notifications', methods=['GET'])
@token_required
def get_notifications():
    """Get user notifications filtered by their preferences"""
    try:
        user_id = request.current_user['id']
        
        # Get query parameters
        is_read = request.args.get('is_read')
        notification_type = request.args.get('type')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)
        limit = max(1, min(limit, 50))
        offset = max(0, offset)

        # Get user's notification preferences
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT notify_report_updates, notify_news_updates
                FROM users
                WHERE id = %s
            """, (user_id,))
            prefs = cursor.fetchone()
        
        if not prefs:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Build type filter based on preferences
        type_filter = []
        type_params = []
        
        # Report & Activity Updates: report status, task assignments, point updates, alerts
        if prefs.get('notify_report_updates', True):
            type_filter.append("n.type IN ('REPORT', 'TASK', 'POINTS', 'BADGE', 'ALERT')")
        
        # News & Updates: platform announcements and eco tips
        if prefs.get('notify_news_updates', False):
            type_filter.append("n.type IN ('ANNOUNCEMENT')")
        
        # If neither preference is enabled, return empty list
        if not type_filter:
            return jsonify({
                'success': True,
                'unread_count': 0,
                'total': 0,
                'data': []
            }), 200
        
        type_clause = "AND (" + " OR ".join(type_filter) + ")"
        
        # Build base query
        where_clause = f"WHERE n.user_id = %s {type_clause}"
        params = [user_id]
        
        if is_read is not None:
            where_clause += " AND n.is_read = %s"
            params.append(is_read.lower() == 'true')
        
        if notification_type:
            where_clause += " AND n.type = %s"
            params.append(notification_type)
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(f"""
                WITH filtered AS (
                    SELECT
                        n.id,
                        n.type,
                        n.title,
                        n.message,
                        n.is_read,
                        n.related_report_id,
                        n.related_task_id,
                        n.created_at
                    FROM notifications n
                    {where_clause}
                )
                SELECT
                    id,
                    type,
                    title,
                    message,
                    is_read,
                    related_report_id,
                    related_task_id,
                    created_at,
                    COUNT(*) OVER() AS total_count,
                    COUNT(*) FILTER (WHERE is_read = false) OVER() AS unread_count
                FROM filtered
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            notifications = cursor.fetchall()

        counts = {
            'total': notifications[0]['total_count'] if notifications else 0,
            'unread_count': notifications[0]['unread_count'] if notifications else 0,
        }
        for notification in notifications:
            notification.pop('total_count', None)
            notification.pop('unread_count', None)
        
        # Convert timestamps to explicit UTC ISO strings so the client does not
        # misread naive database timestamps as local time.
        for notification in notifications:
            notification['created_at'] = _to_utc_iso(notification['created_at'])
        
        return jsonify({
            'success': True,
            'unread_count': counts['unread_count'],
            'total': counts['total'],
            'data': notifications
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@notifications_bp.route('/notifications/<notification_id>/read', methods=['PUT'])
@token_required
def mark_notification_read(notification_id):
    """Mark a specific notification as read"""
    try:
        user_id = request.current_user['id']
        
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                UPDATE notifications 
                SET is_read = true
                WHERE id = %s AND user_id = %s AND is_read = false
            """, (notification_id, user_id))
            
            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Notification not found or already read'}), 404
        
        return jsonify({
            'success': True,
            'message': 'Notification marked as read'
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@notifications_bp.route('/notifications/read-all', methods=['PUT'])
@token_required
def mark_all_notifications_read():
    """Mark all user notifications as read"""
    try:
        user_id = request.current_user['id']
        
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                UPDATE notifications 
                SET is_read = true
                WHERE user_id = %s AND is_read = false
            """, (user_id,))
            count = cursor.rowcount
        
        return jsonify({
            'success': True,
            'message': 'All notifications marked as read',
            'count': count
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@notifications_bp.route('/admin/notifications/bulk', methods=['POST'])
@token_required
@role_required('ADMIN')
def send_bulk_notification():
    """Send notification to multiple users"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        admin_id = request.current_user['id']

        raw_type = str(data.get('type', '')).strip().lower()
        # Map UI style-level types into DB enum notification_type values.
        # Default to ALERT so bulk broadcasts are visible in normal activity feed.
        if raw_type in {'announcement', 'news'}:
            notification_type = 'ANNOUNCEMENT'
        elif raw_type in {'alert', 'warning', 'info', 'success'}:
            notification_type = 'ALERT'
        else:
            notification_type = 'ALERT'
        
        # Validate required fields
        required_fields = ['audience', 'type', 'title', 'message']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'{field} is required'}), 400
        
        # Validate audience
        valid_audiences = ['all', 'citizens', 'cleaners']
        if data['audience'] not in valid_audiences:
            return jsonify({'success': False, 'error': f'Audience must be one of: {", ".join(valid_audiences)}'}), 400
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Insert bulk notification record
            cursor.execute("""
                INSERT INTO bulk_notifications (audience, type, title, message, sent_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at
            """, (data['audience'], raw_type or 'alert', data['title'], data['message'], admin_id))
            bulk_notification = cursor.fetchone()
            
            # Send to individual users based on audience
            recipients_count = 0
            if data['audience'] == 'all':
                cursor.execute("""
                    INSERT INTO notifications (user_id, type, title, message, created_by)
                    SELECT id, %s::notification_type, %s, %s, %s
                    FROM users WHERE is_active = true
                """, (notification_type, data['title'], data['message'], admin_id))
                recipients_count = cursor.rowcount
            elif data['audience'] == 'citizens':
                cursor.execute("""
                    INSERT INTO notifications (user_id, type, title, message, created_by)
                    SELECT id, %s::notification_type, %s, %s, %s
                    FROM users WHERE role = 'CITIZEN' AND is_active = true
                """, (notification_type, data['title'], data['message'], admin_id))
                recipients_count = cursor.rowcount
            elif data['audience'] == 'cleaners':
                cursor.execute("""
                    INSERT INTO notifications (user_id, type, title, message, created_by)
                    SELECT id, %s::notification_type, %s, %s, %s
                    FROM users WHERE role = 'CLEANER' AND is_active = true
                """, (notification_type, data['title'], data['message'], admin_id))
                recipients_count = cursor.rowcount
        
        return jsonify({
            'success': True,
            'message': 'Bulk notification sent successfully',
            'data': {
                'audience': data['audience'],
                'notification_type': notification_type,
                'recipients_count': recipients_count,
                    'sent_at': _to_utc_iso(bulk_notification['created_at'])
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500