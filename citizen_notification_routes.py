from flask import jsonify, request

from auth import token_required, role_required
from models import db_connection

from citizen_blueprint import citizen_bp

@citizen_bp.route('/notifications', methods=['GET'])
@token_required
@role_required('CITIZEN')
def get_notifications():
    """Get user notifications"""
    try:
        user_id = request.current_user['id']
        
        # Get query parameters
        is_read = request.args.get('is_read')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)
        
        limit = max(1, min(limit, 50))
        offset = max(0, offset)

        # Build query
        where_clause = "WHERE n.user_id = %s"
        params = [user_id]
        
        if is_read is not None:
            where_clause += " AND n.is_read = %s"
            params.append(is_read.lower() == 'true')
        
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

        unread_count = notifications[0]['unread_count'] if notifications else 0
        total = notifications[0]['total_count'] if notifications else 0
        for notification in notifications:
            notification.pop('total_count', None)
            notification.pop('unread_count', None)
        
        # Convert timestamps to ISO format
        for notification in notifications:
            notification['created_at'] = notification['created_at'].isoformat()
        
        return jsonify({
            'success': True,
            'unread_count': unread_count,
            'total': total,
            'data': notifications
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/notifications/<notification_id>/read', methods=['PUT'])
@token_required
@role_required('CITIZEN')
def mark_notification_read(notification_id):
    """Mark a notification as read"""
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


@citizen_bp.route('/notifications/read-all', methods=['PUT'])
@token_required
@role_required('CITIZEN')
def mark_all_notifications_read():
    """Mark all notifications as read"""
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


@citizen_bp.route('/notification-settings', methods=['PUT'])
@token_required
@role_required('CITIZEN')
def update_notification_settings():
    """Update notification preferences (which notifications to show in UI)"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        user_id = request.current_user['id']
        
        # Get existing preferences
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT notify_report_updates, notify_news_updates
                FROM users
                WHERE id = %s
            """, (user_id,))
            existing = cursor.fetchone()

        if not existing:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Map frontend fields to notification preference columns
        notify_report_updates = data.get('reportUpdates', existing.get('notify_report_updates', True))
        notify_news_updates = data.get('promotions', existing.get('notify_news_updates', False))
        
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                UPDATE users 
                SET notify_report_updates = %s, notify_news_updates = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (notify_report_updates, notify_news_updates, user_id))
        
        return jsonify({
            'success': True,
            'message': 'Notification preferences updated successfully',
            'data': {
                'reportUpdates': notify_report_updates,
                'promotions': notify_news_updates
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



