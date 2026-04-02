from flask import jsonify, request
from datetime import datetime, timedelta

from auth import token_required, role_required
from models import db_connection

from admin_blueprint import admin_bp
from admin_helpers import _suggest_reward_from_report

@admin_bp.route('/reports', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_all_reports():
    """Get all reports (admin only)"""
    try:
        # Get query parameters
        status = request.args.get('status')
        severity = request.args.get('severity')
        zone_id = request.args.get('zone_id')
        limit = request.args.get('limit', type=int, default=50)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        
        # Build query
        query = """
            SELECT
                   r.id,
                   r.user_id,
                   r.zone_id,
                   r.description,
                   r.severity,
                   r.status,
                   CASE WHEN r.image_url LIKE 'data:%%' THEN NULL ELSE r.image_url END AS image_url,
                   CASE WHEN r.after_image_url LIKE 'data:%%' THEN NULL ELSE r.after_image_url END AS after_image_url,
                   r.latitude,
                   r.longitude,
                   r.created_at,
                   r.completed_at,
                   u.name as user_name,
                   z.name as zone_name,
                   cu.name as cleaner_name
            FROM reports r
            LEFT JOIN users u ON r.user_id = u.id
            LEFT JOIN zones z ON r.zone_id = z.id
            LEFT JOIN users cu ON r.cleaner_id = cu.id
            WHERE 1=1
        """
        params = []
        
        if status:
            query += " AND r.status = %s"
            params.append(status)
        
        if severity:
            query += " AND r.severity = %s"
            params.append(severity)
        
        if zone_id:
            query += " AND r.zone_id = %s"
            params.append(zone_id)
        
        query += " ORDER BY r.created_at DESC"
        
        query += " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(query, params)
            reports = cursor.fetchall()
        
        # Get total count
        count_query = "SELECT COUNT(*) as total FROM reports WHERE 1=1"
        count_params = []
        if status:
            count_query += " AND status = %s"
            count_params.append(status)
        if severity:
            count_query += " AND severity = %s"
            count_params.append(severity)
        if zone_id:
            count_query += " AND zone_id = %s"
            count_params.append(zone_id)
        
        with db_connection.get_cursor() as cursor:
            cursor.execute(count_query, count_params if count_params else None)
            count_result = cursor.fetchone()
            total = count_result['total'] if count_result else 0
        
        return jsonify({
            'success': True,
            'total': total,
            'count': len(reports),
            'data': reports
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/reports/pending', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_pending_reports():
    """Get pending reports (admin only)"""
    try:
        limit = request.args.get('limit', type=int, default=50)
        offset = request.args.get('offset', type=int, default=0)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT
                       r.id,
                       r.user_id,
                       r.zone_id,
                       r.description,
                       r.severity,
                       r.status,
                       NULL::text AS image_url,
                       NULL::text AS after_image_url,
                       r.latitude,
                       r.longitude,
                       r.created_at,
                       r.completed_at,
                       u.name as user_name,
                       z.name as zone_name
                FROM reports r
                LEFT JOIN users u ON r.user_id = u.id
                LEFT JOIN zones z ON r.zone_id = z.id
                WHERE r.status = 'SUBMITTED'
                ORDER BY r.created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            reports = cursor.fetchall()
        
        return jsonify({
            'success': True,
            'count': len(reports),
            'data': reports
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/reports/<report_id>', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_report_details(report_id):
    """Get detailed report information (admin only)"""
    try:
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT 
                       r.*, 
                       u.name as user_name,
                       u.email as user_email,
                       u.phone as user_phone,
                       z.name as zone_name,
                       z.cleanliness_score,
                       cu.name as cleaner_name,
                       cu.avatar_url as cleaner_avatar,
                       cp.rating as cleaner_rating,
                       wa.description as ai_description,
                       wa.severity as ai_severity,
                       wa.estimated_volume,
                       wa.environmental_impact,
                       wa.health_hazard,
                       wa.hazard_details,
                       wa.recommended_action,
                       wa.estimated_cleanup_time,
                       wa.confidence as ai_confidence,
                       cc.completion_percentage,
                       cc.before_summary,
                       cc.after_summary,
                       cc.quality_rating,
                       cc.environmental_benefit,
                       cc.verification_status,
                       cc.feedback,
                       cc.confidence as comparison_confidence,
                       cr.rating as citizen_rating,
                       cr.comment as citizen_comment,
                       cr.created_at as citizen_reviewed_at
                FROM reports r
                LEFT JOIN users u ON r.user_id = u.id
                LEFT JOIN zones z ON r.zone_id = z.id
                LEFT JOIN users cu ON r.cleaner_id = cu.id
                LEFT JOIN cleaner_profiles cp ON r.cleaner_id = cp.user_id
                LEFT JOIN waste_analyses wa ON r.id = wa.report_id
                LEFT JOIN cleanup_comparisons cc ON r.id = cc.report_id
                LEFT JOIN cleanup_reviews cr ON r.id = cr.report_id
                WHERE r.id = %s
            """, (report_id,))
            report = cursor.fetchone()
        
        if not report:
            return jsonify({'success': False, 'error': 'Report not found'}), 404

        waste_composition = []
        special_equipment = []

        if report.get('ai_description'):
            with db_connection.get_cursor() as cursor:
                cursor.execute("""
                    SELECT waste_type, percentage, recyclable
                    FROM waste_compositions wc
                    JOIN waste_analyses wa ON wc.waste_analysis_id = wa.id
                    WHERE wa.report_id = %s
                """, (report_id,))
                waste_composition = cursor.fetchall()

                cursor.execute("""
                    SELECT equipment_name
                    FROM special_equipment se
                    JOIN waste_analyses wa ON se.waste_analysis_id = wa.id
                    WHERE wa.report_id = %s
                """, (report_id,))
                special_equipment_rows = cursor.fetchall()
                special_equipment = [row['equipment_name'] for row in special_equipment_rows]

        response_data = {
            'report': {
                'id': report['id'],
                'user_id': report['user_id'],
                'user_name': report.get('user_name'),
                'zone_id': report['zone_id'],
                'description': report['description'],
                'severity': report['severity'],
                'status': report['status'],
                'image_url': report.get('image_url'),
                'after_image_url': report.get('after_image_url'),
                'latitude': float(report['latitude']) if report.get('latitude') is not None else None,
                'longitude': float(report['longitude']) if report.get('longitude') is not None else None,
                'created_at': report['created_at'].isoformat() if report.get('created_at') else None,
                'completed_at': report['completed_at'].isoformat() if report.get('completed_at') else None,
            },
            'zone': {
                'name': report.get('zone_name') or 'Unknown Zone',
                'cleanliness_score': report.get('cleanliness_score'),
            },
            'user': {
                'name': report.get('user_name'),
                'email': report.get('user_email'),
                'phone': report.get('user_phone'),
            }
        }

        if report.get('cleaner_name'):
            response_data['cleaner'] = {
                'name': report.get('cleaner_name'),
                'avatar_url': report.get('cleaner_avatar'),
                'rating': float(report['cleaner_rating']) if report.get('cleaner_rating') is not None else None,
            }

        if report.get('ai_description'):
            response_data['ai_analysis'] = {
                'description': report.get('ai_description'),
                'severity': report.get('ai_severity') or report.get('severity'),
                'estimated_volume': report.get('estimated_volume'),
                'environmental_impact': report.get('environmental_impact'),
                'health_hazard': report.get('health_hazard'),
                'hazard_details': report.get('hazard_details'),
                'recommended_action': report.get('recommended_action'),
                'estimated_cleanup_time': report.get('estimated_cleanup_time'),
                'confidence': report.get('ai_confidence'),
                'waste_composition': waste_composition,
                'special_equipment_needed': special_equipment,
            }

        if report.get('completion_percentage') is not None:
            response_data['cleanup_comparison'] = {
                'completion_percentage': report.get('completion_percentage'),
                'before_summary': report.get('before_summary'),
                'after_summary': report.get('after_summary'),
                'quality_rating': report.get('quality_rating'),
                'environmental_benefit': report.get('environmental_benefit'),
                'verification_status': report.get('verification_status'),
                'feedback': report.get('feedback'),
                'confidence': report.get('comparison_confidence'),
            }

        if report.get('citizen_rating') is not None:
            response_data['review'] = {
                'rating': report.get('citizen_rating'),
                'comment': report.get('citizen_comment'),
                'reviewed_at': report['citizen_reviewed_at'].isoformat() if report.get('citizen_reviewed_at') else None,
            }
        
        return jsonify({
            'success': True,
            'data': response_data
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/reports/<report_id>/approve', methods=['POST'])
@token_required
@role_required('ADMIN')
def approve_report(report_id):
    """Approve a report and create a task (admin only)"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        admin_id = request.current_user['id']
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Get report details
            cursor.execute("""
                SELECT * FROM reports WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()
            
            if not report:
                return jsonify({'success': False, 'error': 'Report not found'}), 404
            
            if report['status'] != 'SUBMITTED':
                return jsonify({'success': False, 'error': 'Report is not in SUBMITTED status'}), 400
            
            # Update report status
            cursor.execute("""
                UPDATE reports 
                SET status = 'APPROVED', 
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (report_id,))
            
            # Create task with due date (default: 7 days from now if none provided)
            reward = data.get('reward', 500)
            priority = data.get('priority', report['severity'])
            due_date_raw = data.get('dueDate') or data.get('due_date')

            try:
                due_date = datetime.fromisoformat(due_date_raw) if due_date_raw else (datetime.utcnow() + timedelta(days=7))
            except Exception:
                due_date = datetime.utcnow() + timedelta(days=7)

            cursor.execute("""
                INSERT INTO tasks (
                    report_id, zone_id, description, status, priority,
                    due_date, reward, created_by, created_at
                )
                VALUES (%s, %s, %s, 'APPROVED', %s, %s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING *
            """, (
                report_id,
                report['zone_id'],
                report['description'],
                priority,
                due_date,
                reward,
                admin_id,
            ))
            task = cursor.fetchone()
            
            # Award points to citizen
            cursor.execute("""
                UPDATE citizen_profiles 
                SET green_points_balance = green_points_balance + 10,
                    approved_reports = approved_reports + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (report['user_id'],))
            
            # Create notification for citizen
            cursor.execute("""
                INSERT INTO notifications (
                    user_id, type, title, message, created_at
                )
                VALUES (%s, 'REPORT', 'Report Approved', %s, CURRENT_TIMESTAMP)
            """, (
                report['user_id'],
                f'Your report has been approved! You earned 10 green points.'
            ))
        
        return jsonify({
            'success': True,
            'message': 'Report approved and task created',
            'data': {
                'report_id': report_id,
                'task': task
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/reports/<report_id>/reward-suggestion', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_report_reward_suggestion(report_id):
    """Get AI + local market based suggested task reward for a report."""
    try:
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT id, description, severity, latitude, longitude
                FROM reports
                WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()

            if not report:
                return jsonify({'success': False, 'error': 'Report not found'}), 404

            cursor.execute("""
                SELECT description, severity, estimated_volume, environmental_impact,
                       health_hazard, hazard_details, recommended_action, estimated_cleanup_time,
                       confidence
                FROM waste_analyses
                WHERE report_id = %s
            """, (report_id,))
            ai_analysis = cursor.fetchone() or {}

            cursor.execute("""
                SELECT wc.waste_type, wc.percentage, wc.recyclable
                FROM waste_compositions wc
                JOIN waste_analyses wa ON wa.id = wc.waste_analysis_id
                WHERE wa.report_id = %s
            """, (report_id,))
            waste_composition = cursor.fetchall() or []

            cursor.execute("""
                SELECT se.equipment_name
                FROM special_equipment se
                JOIN waste_analyses wa ON wa.id = se.waste_analysis_id
                WHERE wa.report_id = %s
            """, (report_id,))
            special_equipment_rows = cursor.fetchall() or []
            special_equipment = [row['equipment_name'] for row in special_equipment_rows]

        suggestion = _suggest_reward_from_report(report, ai_analysis, waste_composition, special_equipment)

        return jsonify({
            'success': True,
            'data': suggestion
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/reports/<report_id>/decline', methods=['POST'])
@token_required
@role_required('ADMIN')
def decline_report(report_id):
    """Decline a report (admin only)"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        admin_id = request.current_user['id']
        reason = data.get('reason', 'Report declined by admin')
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Get report details
            cursor.execute("""
                SELECT * FROM reports WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()
            
            if not report:
                return jsonify({'success': False, 'error': 'Report not found'}), 404
            
            if report['status'] != 'SUBMITTED':
                return jsonify({'success': False, 'error': 'Report is not in SUBMITTED status'}), 400
            
            # Update report status
            cursor.execute("""
                UPDATE reports 
                SET status = 'DECLINED', 
                    decline_reason = %s,
                    reviewed_by = %s,
                    reviewed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (reason, admin_id, report_id))
            
            # Create notification for citizen
            cursor.execute("""
                INSERT INTO notifications (
                    user_id, type, title, message, created_at
                )
                VALUES (%s, 'REPORT', 'Report Declined', %s, CURRENT_TIMESTAMP)
            """, (
                report['user_id'],
                f'Your report was declined. Reason: {reason}'
            ))
        
        return jsonify({
            'success': True,
            'message': 'Report declined',
            'data': {
                'report_id': report_id,
                'reason': reason
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_bp.route('/reports/<report_id>/reopen', methods=['POST'])
@token_required
@role_required('ADMIN')
def reopen_declined_report(report_id):
    """Move a declined report back to SUBMITTED for re-review."""
    try:
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                SELECT id, user_id, status
                FROM reports
                WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()

            if not report:
                return jsonify({'success': False, 'error': 'Report not found'}), 404

            if report['status'] != 'DECLINED':
                return jsonify({'success': False, 'error': 'Only DECLINED reports can be moved back to pending'}), 400

            cursor.execute("""
                UPDATE reports
                SET status = 'SUBMITTED',
                    decline_reason = NULL,
                    reviewed_by = NULL,
                    reviewed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (report_id,))

            cursor.execute("""
                INSERT INTO notifications (user_id, type, title, message, created_at)
                VALUES (%s, 'REPORT', 'Report Reopened', %s, CURRENT_TIMESTAMP)
            """, (
                report['user_id'],
                'Your report has been moved back to pending review by the admin.'
            ))

        return jsonify({
            'success': True,
            'message': 'Report moved back to pending review',
            'data': {
                'report_id': report_id,
                'status': 'SUBMITTED'
            }
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500






