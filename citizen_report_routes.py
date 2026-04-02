from flask import jsonify, request

from auth import token_required, role_required
from models import db_connection

from citizen_blueprint import citizen_bp


def _to_int_percentage(value, default=0):
    """Convert AI percentage values like '35' or '35%' into bounded ints."""
    try:
        if isinstance(value, str):
            value = value.strip().rstrip('%')
        parsed = int(float(value))
        return max(0, min(100, parsed))
    except Exception:
        return default

@citizen_bp.route('/reports', methods=['POST'])
@token_required
@role_required('CITIZEN')
def submit_report():
    """Submit a new waste report"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        user_id = request.current_user['id']
        
        # Validate required fields
        required_fields = ['zone_id', 'description', 'severity', 'latitude', 'longitude']
        for field in required_fields:
            if field not in data or data.get(field) is None or (isinstance(data.get(field), str) and not data.get(field).strip()):
                return jsonify({'success': False, 'error': f'{field} is required'}), 400

        try:
            latitude = float(data['latitude'])
            longitude = float(data['longitude'])
        except Exception:
            return jsonify({'success': False, 'error': 'latitude and longitude must be numeric'}), 400
        
        # Validate severity
        valid_severities = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
        if data['severity'] not in valid_severities:
            return jsonify({'success': False, 'error': f'Severity must be one of: {", ".join(valid_severities)}'}), 400
        
        # Create report
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                INSERT INTO reports (user_id, zone_id, description, image_url, severity, latitude, longitude)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at, status
            """, (
                user_id, data['zone_id'], data['description'], 
                data.get('image_url'), data['severity'], 
                latitude, longitude
            ))
            new_report = cursor.fetchone()

            # Persist AI analysis if supplied from frontend pre-analysis.
            ai_analysis = data.get('ai_analysis')
            if ai_analysis and isinstance(ai_analysis, dict):
                ai_severity = ai_analysis.get('severity', data['severity'])
                if ai_severity not in valid_severities:
                    ai_severity = data['severity']

                valid_impacts = ['LOW', 'MODERATE', 'HIGH', 'SEVERE']
                ai_impact = ai_analysis.get('environmental_impact', 'MODERATE')
                if ai_impact not in valid_impacts:
                    ai_impact = 'MODERATE'

                ai_confidence = ai_analysis.get('confidence', 0)
                try:
                    ai_confidence = int(ai_confidence)
                except Exception:
                    ai_confidence = 0
                ai_confidence = max(0, min(100, ai_confidence))

                cursor.execute("""
                    INSERT INTO waste_analyses
                    (report_id, description, severity, estimated_volume, environmental_impact,
                     health_hazard, hazard_details, recommended_action, estimated_cleanup_time,
                     confidence, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    new_report['id'],
                    ai_analysis.get('description', ''),
                    ai_severity,
                    ai_analysis.get('estimated_volume', ''),
                    ai_impact,
                    bool(ai_analysis.get('health_hazard', False)),
                    ai_analysis.get('hazard_details', ''),
                    ai_analysis.get('recommended_action', ''),
                    ai_analysis.get('estimated_cleanup_time', ''),
                    ai_confidence,
                    user_id,
                ))
                waste_analysis = cursor.fetchone()

                for waste in ai_analysis.get('waste_composition', []) or []:
                    if not isinstance(waste, dict):
                        continue
                    cursor.execute("""
                        INSERT INTO waste_compositions
                        (waste_analysis_id, waste_type, percentage, recyclable)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        waste_analysis['id'],
                        waste.get('waste_type', 'Unknown'),
                        _to_int_percentage(waste.get('percentage', 0), 0),
                        bool(waste.get('recyclable', False)),
                    ))

                for equipment in ai_analysis.get('special_equipment_needed', []) or []:
                    cursor.execute("""
                        INSERT INTO special_equipment (waste_analysis_id, equipment_name)
                        VALUES (%s, %s)
                    """, (waste_analysis['id'], str(equipment)))
            
            # Get points earned (calculated by trigger)
            cursor.execute("""
                SELECT SUM(green_points) as points_earned
                FROM green_points_transactions 
                WHERE user_id = %s AND report_id = %s
            """, (user_id, new_report['id']))
            points_result = cursor.fetchone()
            points_earned = points_result['points_earned'] if points_result else 0
        
        return jsonify({
            'success': True,
            'message': 'Report submitted successfully',
            'data': {
                'id': new_report['id'],
                'zone_id': data['zone_id'],
                'description': data['description'],
                'severity': data['severity'],
                'status': new_report['status'],
                'created_at': new_report['created_at'].isoformat(),
                'points_earned': points_earned
            }
        }), 201
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/reports', methods=['GET'])
@token_required
@role_required('CITIZEN')
def get_my_reports():
    """Get all reports submitted by the citizen"""
    try:
        user_id = request.current_user['id']
        
        # Get query parameters
        status = request.args.get('status')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        # Build query
        where_clause = "WHERE r.user_id = %s"
        params = [user_id]
        
        if status:
            where_clause += " AND r.status = %s"
            params.append(status)
        
        with db_connection.get_cursor() as cursor:
            # Get reports
            cursor.execute(f"""
                SELECT
                    r.id,
                    r.description,
                    r.severity,
                    r.status,
                    CASE WHEN r.image_url LIKE 'data:%' THEN NULL ELSE r.image_url END AS image_url,
                    CASE WHEN r.after_image_url LIKE 'data:%' THEN NULL ELSE r.after_image_url END AS after_image_url,
                    r.created_at,
                    r.completed_at,
                    z.name as zone_name,
                    u.name as cleaner_name,
                    cr.rating as citizen_rating,
                    cr.comment as citizen_comment,
                    cr.created_at as citizen_reviewed_at
                FROM reports r
                JOIN zones z ON r.zone_id = z.id
                LEFT JOIN users u ON r.cleaner_id = u.id
                LEFT JOIN cleanup_reviews cr ON r.id = cr.report_id
                {where_clause}
                ORDER BY r.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            reports = cursor.fetchall()
            
            # Get total count
            cursor.execute(f"""
                SELECT COUNT(*) as total
                FROM reports r
                {where_clause}
            """, params)
            total_result = cursor.fetchone()
            total = total_result['total'] if total_result else 0
        
        return jsonify({
            'success': True,
            'total': total,
            'count': len(reports),
            'data': reports
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/reports/<report_id>', methods=['GET'])
@token_required
@role_required('CITIZEN')
def get_report_details(report_id):
    """Get detailed information about a specific report"""
    try:
        user_id = request.current_user['id']
        
        with db_connection.get_cursor() as cursor:
            # Get report with all related data
            cursor.execute("""
                SELECT 
                    r.*,
                    z.name as zone_name, z.cleanliness_score,
                    u.name as cleaner_name, u.avatar_url as cleaner_avatar,
                    cp.rating as cleaner_rating,
                    wa.description as ai_description, wa.severity as ai_severity,
                    wa.estimated_volume, wa.environmental_impact, wa.health_hazard,
                    wa.hazard_details, wa.recommended_action, wa.estimated_cleanup_time,
                    wa.confidence as ai_confidence,
                    cc.completion_percentage, cc.before_summary, cc.after_summary,
                    cc.quality_rating, cc.environmental_benefit, cc.verification_status,
                    cc.feedback, cc.confidence as comparison_confidence,
                    cr.rating as citizen_rating, cr.comment as citizen_comment,
                    cr.created_at as citizen_reviewed_at
                FROM reports r
                JOIN zones z ON r.zone_id = z.id
                LEFT JOIN users u ON r.cleaner_id = u.id
                LEFT JOIN cleaner_profiles cp ON r.cleaner_id = cp.user_id
                LEFT JOIN waste_analyses wa ON r.id = wa.report_id
                LEFT JOIN cleanup_comparisons cc ON r.id = cc.report_id
                LEFT JOIN cleanup_reviews cr ON r.id = cr.report_id
                WHERE r.id = %s AND r.user_id = %s
            """, (report_id, user_id))
            report = cursor.fetchone()
            
            if not report:
                return jsonify({'success': False, 'error': 'Report not found'}), 404
            
            # Get waste composition if available
            waste_composition = []
            special_equipment = []
            if report.get('ai_description'):
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
        
        # Structure the response
        response_data = {
            'report': {
                'id': report['id'],
                'zone_id': report['zone_id'],
                'description': report['description'],
                'severity': report['severity'],
                'status': report['status'],
                'image_url': report['image_url'],
                'after_image_url': report['after_image_url'],
                'latitude': float(report['latitude']) if report['latitude'] else None,
                'longitude': float(report['longitude']) if report['longitude'] else None,
                'created_at': report['created_at'].isoformat(),
                'completed_at': report['completed_at'].isoformat() if report['completed_at'] else None
            },
            'zone': {
                'name': report['zone_name'],
                'cleanliness_score': report['cleanliness_score']
            }
        }
        
        if report['cleaner_name']:
            response_data['cleaner'] = {
                'name': report['cleaner_name'],
                'avatar_url': report['cleaner_avatar'],
                'rating': float(report['cleaner_rating']) if report['cleaner_rating'] else None
            }
        
        if report['ai_description']:
            response_data['ai_analysis'] = {
                'description': report['ai_description'],
                'severity': report['ai_severity'],
                'estimated_volume': report['estimated_volume'],
                'environmental_impact': report['environmental_impact'],
                'health_hazard': report['health_hazard'],
                'hazard_details': report.get('hazard_details'),
                'recommended_action': report['recommended_action'],
                'estimated_cleanup_time': report.get('estimated_cleanup_time'),
                'confidence': report.get('ai_confidence'),
                'waste_composition': waste_composition,
                'special_equipment_needed': special_equipment,
            }
        
        if report['completion_percentage']:
            response_data['cleanup_comparison'] = {
                'completion_percentage': report['completion_percentage'],
                'before_summary': report.get('before_summary'),
                'after_summary': report.get('after_summary'),
                'quality_rating': report['quality_rating'],
                'environmental_benefit': report['environmental_benefit'],
                'verification_status': report.get('verification_status'),
                'feedback': report.get('feedback'),
                'confidence': report.get('comparison_confidence'),
            }
        
        if report['citizen_rating']:
            response_data['review'] = {
                'rating': report['citizen_rating'],
                'comment': report['citizen_comment'],
                'reviewed_at': report['citizen_reviewed_at'].isoformat() if report.get('citizen_reviewed_at') else None,
            }
        
        return jsonify({
            'success': True,
            'data': response_data
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/reports/<report_id>', methods=['PUT'])
@token_required
@role_required('CITIZEN')
def update_report(report_id):
    """Update a submitted report owned by the citizen."""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = request.current_user['id']

        allowed_fields = {
            'zone_id': 'zone_id',
            'description': 'description',
            'severity': 'severity',
            'latitude': 'latitude',
            'longitude': 'longitude',
            'image_url': 'image_url',
        }

        updates = {}
        for incoming_key, db_field in allowed_fields.items():
            if incoming_key in data:
                updates[db_field] = data[incoming_key]

        if not updates:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400

        if 'severity' in updates:
            valid_severities = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
            if updates['severity'] not in valid_severities:
                return jsonify({'success': False, 'error': f'Severity must be one of: {", ".join(valid_severities)}'}), 400

        with db_connection.get_cursor(commit=True) as cursor:
            # Only allow editing submitted reports that belong to this citizen.
            cursor.execute("""
                SELECT id, status
                FROM reports
                WHERE id = %s AND user_id = %s
            """, (report_id, user_id))
            existing = cursor.fetchone()

            if not existing:
                return jsonify({'success': False, 'error': 'Report not found'}), 404

            if existing['status'] != 'SUBMITTED':
                return jsonify({'success': False, 'error': 'Only submitted reports can be edited'}), 409

            set_clause = ', '.join([f"{field} = %s" for field in updates.keys()])
            values = list(updates.values()) + [report_id, user_id]

            cursor.execute(f"""
                UPDATE reports
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP, updated_by = %s
                WHERE id = %s AND user_id = %s
                RETURNING id, zone_id, description, severity, status, image_url, latitude, longitude, created_at, updated_at
            """, [*list(updates.values()), user_id, report_id, user_id])
            updated_report = cursor.fetchone()

        return jsonify({
            'success': True,
            'message': 'Report updated successfully',
            'data': updated_report,
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/reports/<report_id>', methods=['DELETE'])
@token_required
@role_required('CITIZEN')
def delete_report(report_id):
    """Delete a submitted report owned by the citizen."""
    try:
        user_id = request.current_user['id']

        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                SELECT id, status
                FROM reports
                WHERE id = %s AND user_id = %s
            """, (report_id, user_id))
            existing = cursor.fetchone()

            if not existing:
                return jsonify({'success': False, 'error': 'Report not found'}), 404

            if existing['status'] != 'SUBMITTED':
                return jsonify({'success': False, 'error': 'Only submitted reports can be deleted'}), 409

            # If tasks reference this report, keep integrity and block deletion.
            cursor.execute("""
                SELECT COUNT(*) AS cnt
                FROM tasks
                WHERE report_id = %s
            """, (report_id,))
            task_ref = cursor.fetchone()
            if task_ref and task_ref['cnt'] > 0:
                return jsonify({'success': False, 'error': 'Cannot delete report linked to tasks'}), 409

            # Remove points transactions linked to this report and rollback the citizen balance.
            cursor.execute("""
                SELECT COALESCE(SUM(green_points), 0) AS points_total
                FROM green_points_transactions
                WHERE user_id = %s AND report_id = %s
            """, (user_id, report_id))
            points_row = cursor.fetchone()
            points_total = int(points_row['points_total']) if points_row and points_row['points_total'] is not None else 0

            if points_total != 0:
                cursor.execute("""
                    UPDATE citizen_profiles
                    SET green_points_balance = GREATEST(0, green_points_balance - %s),
                        total_reports = GREATEST(0, total_reports - 1),
                        updated_at = CURRENT_TIMESTAMP,
                        updated_by = %s
                    WHERE user_id = %s
                """, (points_total, user_id, user_id))

            cursor.execute("""
                DELETE FROM green_points_transactions
                WHERE report_id = %s
            """, (report_id,))

            # Break optional FK links in notifications that point to this report.
            cursor.execute("""
                UPDATE notifications
                SET related_report_id = NULL
                WHERE related_report_id = %s
            """, (report_id,))

            cursor.execute("""
                DELETE FROM reports
                WHERE id = %s AND user_id = %s
            """, (report_id, user_id))

        return jsonify({
            'success': True,
            'message': 'Report deleted successfully',
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@citizen_bp.route('/reports/<report_id>/review', methods=['POST'])
@token_required
@role_required('CITIZEN')
def submit_cleanup_review(report_id):
    """Submit a review for completed cleanup work"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        user_id = request.current_user['id']
        
        # Validate required fields
        if not data.get('rating') or not isinstance(data['rating'], int) or data['rating'] < 1 or data['rating'] > 5:
            return jsonify({'success': False, 'error': 'Rating must be an integer between 1 and 5'}), 400
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Verify report exists, is completed, and belongs to user
            cursor.execute("""
                SELECT cleaner_id FROM reports 
                WHERE id = %s AND user_id = %s AND status = 'COMPLETED' AND cleaner_id IS NOT NULL
            """, (report_id, user_id))
            report = cursor.fetchone()
            
            if not report:
                return jsonify({'success': False, 'error': 'Report not found, not completed, or not yours'}), 404
            
            # Check if review already exists
            cursor.execute("""
                SELECT id FROM cleanup_reviews WHERE report_id = %s
            """, (report_id,))
            existing_review = cursor.fetchone()
            
            if existing_review:
                return jsonify({'success': False, 'error': 'Review already submitted for this report'}), 409
            
            # Create review
            cursor.execute("""
                INSERT INTO cleanup_reviews (report_id, citizen_id, cleaner_id, rating, comment)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at
            """, (report_id, user_id, report['cleaner_id'], data['rating'], data.get('comment')))
            new_review = cursor.fetchone()
            
            # Get points earned (calculated by trigger)
            cursor.execute("""
                SELECT green_points FROM green_points_config WHERE action_type = 'REVIEW_SUBMITTED'
            """, ())
            points_result = cursor.fetchone()
            points_earned = points_result['green_points'] if points_result else 0
        
        return jsonify({
            'success': True,
            'message': 'Review submitted successfully',
            'data': {
                'rating': data['rating'],
                'comment': data.get('comment'),
                'points_earned': points_earned,
                'created_at': new_review['created_at'].isoformat()
            }
        }), 201
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


