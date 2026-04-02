from flask import jsonify, request

from auth import token_required, role_required
from ai_service import ai_service
from models import db_connection

from cleaner_blueprint import cleaner_bp

@cleaner_bp.route('/tasks/available', methods=['GET'])
@token_required
@role_required('CLEANER')
def get_available_tasks():
    """Get all available tasks that can be taken"""
    try:
        # Get query parameters
        zone_id = request.args.get('zone_id')
        priority = request.args.get('priority')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        
        # Build query
        where_clause = "WHERE t.status = 'APPROVED' AND t.cleaner_id IS NULL"
        params = []
        
        if zone_id:
            where_clause += " AND t.zone_id = %s"
            params.append(zone_id)
        
        if priority:
            where_clause += " AND t.priority = %s"
            params.append(priority)
        
        with db_connection.get_cursor() as cursor:
            # Get available tasks.
            cursor.execute(f"""
                SELECT 
                    t.id, t.zone_id, t.description, t.priority, t.due_date, t.reward, t.created_at,
                    z.name as zone_name, z.cleanliness_score,
                    r.id as report_id, r.description as report_description, 
                    CASE WHEN r.image_url LIKE 'data:%%' THEN NULL ELSE r.image_url END AS report_image, r.severity,
                    r.latitude, r.longitude,
                    u.name as reporter_name,
                    wa.description as ai_description,
                    wa.severity as ai_severity,
                    wa.estimated_volume, wa.estimated_cleanup_time,
                    wa.environmental_impact, wa.health_hazard, wa.hazard_details,
                    wa.recommended_action, wa.confidence as ai_confidence
                FROM tasks t
                JOIN zones z ON t.zone_id = z.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN users u ON r.user_id = u.id
                LEFT JOIN waste_analyses wa ON r.id = wa.report_id
                {where_clause}
                ORDER BY t.priority DESC, t.due_date ASC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            tasks = cursor.fetchall()

            cursor.execute(f"""
                SELECT COUNT(*) as total
                FROM tasks t
                {where_clause}
            """, params)
            total_result = cursor.fetchone()
            total = total_result['total'] if total_result else 0

            report_ids = [task['report_id'] for task in tasks if task.get('report_id')]
            waste_by_report = {}
            equipment_by_report = {}

            if report_ids:
                cursor.execute("""
                    SELECT
                        wa.report_id,
                        wc.waste_type,
                        wc.percentage,
                        wc.recyclable
                    FROM waste_compositions wc
                    JOIN waste_analyses wa ON wc.waste_analysis_id = wa.id
                    WHERE wa.report_id = ANY(%s)
                    ORDER BY wa.report_id
                """, (report_ids,))
                for row in cursor.fetchall():
                    waste_by_report.setdefault(row['report_id'], []).append({
                        'waste_type': row['waste_type'],
                        'percentage': row['percentage'],
                        'recyclable': row['recyclable'],
                    })

                cursor.execute("""
                    SELECT
                        wa.report_id,
                        se.equipment_name
                    FROM special_equipment se
                    JOIN waste_analyses wa ON se.waste_analysis_id = wa.id
                    WHERE wa.report_id = ANY(%s)
                    ORDER BY wa.report_id
                """, (report_ids,))
                for row in cursor.fetchall():
                    equipment_by_report.setdefault(row['report_id'], []).append(row['equipment_name'])

            for task in tasks:
                report_id = task.get('report_id')
                task['waste_composition'] = waste_by_report.get(report_id, []) if report_id else []
                task['special_equipment'] = equipment_by_report.get(report_id, []) if report_id else []
        
        # Convert timestamps to ISO format
        for task in tasks:
            task['created_at'] = task['created_at'].isoformat()
            task['due_date'] = task['due_date'].isoformat()
            task['latitude'] = float(task['latitude']) if task.get('latitude') is not None else None
            task['longitude'] = float(task['longitude']) if task.get('longitude') is not None else None
        
        return jsonify({
            'success': True,
            'total': total,
            'data': tasks
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/tasks/<task_id>/take', methods=['POST'])
@token_required
@role_required('CLEANER')
def take_task(task_id):
    """Take an available task"""
    try:
        user_id = request.current_user['id']
        
        with db_connection.get_cursor(commit=True) as cursor:
            cursor.execute("""
                UPDATE tasks
                SET cleaner_id = %s,
                    status = 'IN_PROGRESS',
                    taken_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND cleaner_id IS NULL
                  AND status = 'APPROVED'
                RETURNING taken_at
            """, (user_id, task_id))
            updated_task = cursor.fetchone()

            if not updated_task:
                return jsonify({'success': False, 'error': 'Task not available or already taken'}), 404
        
        return jsonify({
            'success': True,
            'message': 'Task taken successfully',
            'data': {
                'id': task_id,
                'status': 'IN_PROGRESS',
                'taken_at': updated_task['taken_at'].isoformat()
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/tasks', methods=['GET'])
@token_required
@role_required('CLEANER')
def get_my_tasks():
    """Get all tasks assigned to the cleaner"""
    try:
        user_id = request.current_user['id']
        
        # Get query parameters
        status = request.args.get('status')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        
        # Build query
        where_clause = "WHERE t.cleaner_id = %s"
        params = [user_id]
        
        if status:
            where_clause += " AND t.status = %s"
            params.append(status)
        
        with db_connection.get_cursor() as cursor:
            # Get tasks
            cursor.execute(f"""
                SELECT 
                    t.id, t.description, t.priority, t.status, t.reward, 
                    t.due_date, t.taken_at, t.completed_at,
                    CASE WHEN t.evidence_image_url LIKE 'data:%%' THEN NULL ELSE t.evidence_image_url END AS evidence_image_url,
                    z.name as zone_name,
                    r.id as report_id,
                    CASE WHEN r.image_url LIKE 'data:%%' THEN NULL ELSE r.image_url END AS report_image,
                    u.name as reporter_name
                    ,cc.completion_percentage, cc.before_summary, cc.after_summary,
                    cc.quality_rating, cc.environmental_benefit, cc.verification_status,
                    cc.feedback, cc.confidence as comparison_confidence
                FROM tasks t
                JOIN zones z ON t.zone_id = z.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN users u ON r.user_id = u.id
                LEFT JOIN cleanup_comparisons cc ON r.id = cc.report_id
                {where_clause}
                ORDER BY t.taken_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            tasks = cursor.fetchall()
            
            # Get total count
            cursor.execute(f"""
                SELECT COUNT(*) as total
                FROM tasks t
                {where_clause}
            """, params)
            total_result = cursor.fetchone()
            total = total_result['total'] if total_result else 0
        
        # Convert timestamps to ISO format
        for task in tasks:
            task['due_date'] = task['due_date'].isoformat()
            task['taken_at'] = task['taken_at'].isoformat() if task['taken_at'] else None
            task['completed_at'] = task['completed_at'].isoformat() if task['completed_at'] else None
        
        return jsonify({
            'success': True,
            'total': total,
            'data': tasks
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/tasks/<task_id>/complete', methods=['POST'])
@token_required
@role_required('CLEANER')
def complete_task(task_id):
    """Mark a task as completed with evidence"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        user_id = request.current_user['id']
        comparison_result = None
        report_id = None
        reward = 0
        completed_at = None
        
        # Validate required fields
        if not data.get('evidence_image_url'):
            return jsonify({'success': False, 'error': 'evidence_image_url is required'}), 400

        # Phase 1: read task data quickly without opening a long transaction.
        with db_connection.get_cursor() as cursor:
            # Verify task belongs to cleaner and is in progress
            cursor.execute("""
                SELECT t.report_id, t.reward, r.image_url as before_image_url
                FROM tasks t
                LEFT JOIN reports r ON t.report_id = r.id
                WHERE t.id = %s AND t.cleaner_id = %s AND t.status = 'IN_PROGRESS'
            """, (task_id, user_id))
            task = cursor.fetchone()
            
            if not task:
                return jsonify({'success': False, 'error': 'Task not found or not in progress'}), 404

        report_id = task.get('report_id')
        reward = float(task.get('reward') or 0)

        before_image_url = task.get('before_image_url')
        after_image_url = data.get('after_image_url', data['evidence_image_url'])

        # Phase 2: AI call outside any DB cursor to avoid pool starvation.
        if before_image_url and after_image_url:
            try:
                comparison_result = ai_service.compare_cleanup_images(before_image_url, after_image_url, report_id)
            except Exception as compare_error:
                print(f"⚠️ Cleanup comparison generation failed: {compare_error}")

        # Phase 3: write updates in a short transaction.
        with db_connection.get_cursor(commit=True) as cursor:
            
            # Complete the task
            cursor.execute("""
                UPDATE tasks 
                SET status = 'COMPLETED', 
                    evidence_image_url = %s,
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING completed_at
            """, (data['evidence_image_url'], task_id))
            updated_task = cursor.fetchone()
            completed_at = updated_task['completed_at']
            
            # Update report if exists
            if report_id:
                cursor.execute("""
                    UPDATE reports 
                    SET status = 'COMPLETED',
                        after_image_url = %s,
                        completed_at = CURRENT_TIMESTAMP,
                        cleaner_id = %s
                    WHERE id = %s
                """, (data.get('after_image_url', data['evidence_image_url']), 
                      user_id, report_id))

                if comparison_result:
                    # Upsert cleanup comparison base row.
                    cursor.execute("""
                        INSERT INTO cleanup_comparisons
                        (report_id, completion_percentage, before_summary, after_summary,
                         quality_rating, environmental_benefit, verification_status, feedback,
                         confidence, created_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (report_id)
                        DO UPDATE SET
                            completion_percentage = EXCLUDED.completion_percentage,
                            before_summary = EXCLUDED.before_summary,
                            after_summary = EXCLUDED.after_summary,
                            quality_rating = EXCLUDED.quality_rating,
                            environmental_benefit = EXCLUDED.environmental_benefit,
                            verification_status = EXCLUDED.verification_status,
                            feedback = EXCLUDED.feedback,
                            confidence = EXCLUDED.confidence,
                            updated_at = CURRENT_TIMESTAMP,
                            updated_by = EXCLUDED.created_by
                        RETURNING id
                    """, (
                        report_id,
                        comparison_result.get('completionPercentage', 0),
                        comparison_result.get('beforeSummary', ''),
                        comparison_result.get('afterSummary', ''),
                        comparison_result.get('qualityRating', 'FAIR'),
                        comparison_result.get('environmentalBenefit', ''),
                        comparison_result.get('verificationStatus', 'NEEDS_REVIEW'),
                        comparison_result.get('feedback', ''),
                        comparison_result.get('confidence', 0),
                        user_id,
                    ))
                    cleanup_row = cursor.fetchone()

                    if cleanup_row:
                        cleanup_id = cleanup_row['id']

                        # Replace waste removed details.
                        cursor.execute("""
                            DELETE FROM cleanup_waste_removed
                            WHERE cleanup_comparison_id = %s
                        """, (cleanup_id,))

                        for waste in comparison_result.get('wasteRemoved', []) or []:
                            cursor.execute("""
                                INSERT INTO cleanup_waste_removed
                                (cleanup_comparison_id, waste_type, percentage, recyclable)
                                VALUES (%s, %s, %s, %s)
                            """, (
                                cleanup_id,
                                waste.get('type', 'Mixed'),
                                int(waste.get('percentage', 0) or 0),
                                bool(waste.get('recyclable', False)),
                            ))

                        # Replace remaining issues details.
                        cursor.execute("""
                            DELETE FROM cleanup_remaining_issues
                            WHERE cleanup_comparison_id = %s
                        """, (cleanup_id,))

                        for issue in comparison_result.get('remainingIssues', []) or []:
                            if not issue:
                                continue
                            cursor.execute("""
                                INSERT INTO cleanup_remaining_issues
                                (cleanup_comparison_id, issue_description)
                                VALUES (%s, %s)
                            """, (cleanup_id, str(issue)))
        
        return jsonify({
            'success': True,
            'message': 'Task completed successfully',
            'data': {
                'id': task_id,
                'status': 'COMPLETED',
                'completed_at': completed_at.isoformat() if completed_at else None,
                'earnings': reward
            }
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/tasks/<task_id>', methods=['GET'])
@token_required
@role_required('CLEANER')
def get_task_details(task_id):
    """Get detailed information about a specific task"""
    try:
        user_id = request.current_user['id']
        
        with db_connection.get_cursor() as cursor:
            # Get task with all related data
            cursor.execute("""
                SELECT 
                    t.*,
                    z.name as zone_name, z.cleanliness_score,
                    r.id as report_id, r.description as report_description, 
                    r.image_url as report_image, r.after_image_url,
                    u.name as reporter_name,
                    wa.estimated_volume, wa.environmental_impact, wa.recommended_action,
                    cr.rating as review_rating, cr.comment as review_comment, cr.created_at as review_date,
                    et.amount as earnings_amount, et.status as earnings_status, et.paid_at
                FROM tasks t
                JOIN zones z ON t.zone_id = z.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN users u ON r.user_id = u.id
                LEFT JOIN waste_analyses wa ON r.id = wa.report_id
                LEFT JOIN cleanup_reviews cr ON r.id = cr.report_id
                LEFT JOIN earnings_transactions et ON t.id = et.task_id
                WHERE t.id = %s AND t.cleaner_id = %s
            """, (task_id, user_id))
            task = cursor.fetchone()
            
            if not task:
                return jsonify({'success': False, 'error': 'Task not found'}), 404
            
            # Get special equipment if available
            special_equipment = []
            if task['report_id']:
                cursor.execute("""
                    SELECT se.equipment_name
                    FROM special_equipment se
                    JOIN waste_analyses wa ON se.waste_analysis_id = wa.id
                    WHERE wa.report_id = %s
                """, (task['report_id'],))
                equipment = cursor.fetchall()
                special_equipment = [eq['equipment_name'] for eq in equipment]
        
        # Structure the response
        response_data = {
            'task': {
                'id': task['id'],
                'description': task['description'],
                'priority': task['priority'],
                'status': task['status'],
                'reward': float(task['reward']),
                'due_date': task['due_date'].isoformat(),
                'taken_at': task['taken_at'].isoformat() if task['taken_at'] else None,
                'completed_at': task['completed_at'].isoformat() if task['completed_at'] else None,
                'evidence_image_url': task['evidence_image_url']
            },
            'zone': {
                'name': task['zone_name'],
                'cleanliness_score': task['cleanliness_score']
            }
        }
        
        if task['report_id']:
            response_data['report'] = {
                'id': task['report_id'],
                'description': task['report_description'],
                'image_url': task['report_image'],
                'after_image_url': task['after_image_url'],
                'reporter_name': task['reporter_name']
            }
            
            if task['estimated_volume']:
                response_data['ai_analysis'] = {
                    'estimated_volume': task['estimated_volume'],
                    'environmental_impact': task['environmental_impact'],
                    'recommended_action': task['recommended_action'],
                    'special_equipment': special_equipment
                }
        
        if task['review_rating']:
            response_data['review'] = {
                'rating': task['review_rating'],
                'comment': task['review_comment'],
                'created_at': task['review_date'].isoformat()
            }
        
        if task['earnings_amount']:
            response_data['earnings'] = {
                'amount': float(task['earnings_amount']),
                'status': task['earnings_status'],
                'paid_at': task['paid_at'].isoformat() if task['paid_at'] else None
            }
        
        return jsonify({
            'success': True,
            'data': response_data
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



