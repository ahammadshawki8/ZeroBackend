from flask import Blueprint, jsonify, request
from auth import token_required, role_required
from models import db_connection

admin_tasks_bp = Blueprint('admin_tasks', __name__)


@admin_tasks_bp.route('/tasks', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_all_tasks():
    """Get all tasks with filtering"""
    try:
        # Get query parameters
        status = request.args.get('status')
        priority = request.args.get('priority')
        zone_id = request.args.get('zone_id')
        cleaner_id = request.args.get('cleaner_id')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        
        # Build query
        where_clause = "WHERE 1=1"
        params = []
        
        if status:
            where_clause += " AND t.status = %s"
            params.append(status)
        
        if priority:
            where_clause += " AND t.priority = %s"
            params.append(priority)
        
        if zone_id:
            where_clause += " AND t.zone_id = %s"
            params.append(zone_id)
        
        if cleaner_id:
            where_clause += " AND t.cleaner_id = %s"
            params.append(cleaner_id)
        
        with db_connection.get_cursor() as cursor:
            # Get tasks
            cursor.execute(f"""
                SELECT 
                    t.id, t.report_id, t.zone_id, t.cleaner_id,
                    t.description, t.priority, t.status, t.reward,
                    t.due_date, t.created_at, t.taken_at, t.completed_at,
                    CASE WHEN t.evidence_image_url LIKE 'data:%%' THEN NULL ELSE t.evidence_image_url END AS evidence_image_url,
                    z.name as zone_name,
                    c.name as cleaner_name,
                    CASE WHEN r.image_url LIKE 'data:%%' THEN NULL ELSE r.image_url END AS before_image_url,
                    CASE WHEN r.after_image_url LIKE 'data:%%' THEN NULL ELSE r.after_image_url END AS report_after_image_url,
                    wa.description as ai_description,
                    wa.severity as ai_severity,
                    wa.estimated_volume,
                    wa.environmental_impact,
                    wa.health_hazard,
                    wa.hazard_details,
                    wa.recommended_action,
                    wa.estimated_cleanup_time,
                    wa.confidence as ai_confidence,
                    (
                        SELECT COALESCE(
                            json_agg(
                                json_build_object(
                                    'waste_type', wc.waste_type,
                                    'percentage', wc.percentage,
                                    'recyclable', wc.recyclable
                                )
                            ),
                            '[]'::json
                        )
                        FROM waste_compositions wc
                        WHERE wa.id IS NOT NULL AND wc.waste_analysis_id = wa.id
                    ) as waste_composition,
                    (
                        SELECT COALESCE(
                            json_agg(se.equipment_name),
                            '[]'::json
                        )
                        FROM special_equipment se
                        WHERE wa.id IS NOT NULL AND se.waste_analysis_id = wa.id
                    ) as special_equipment_needed,
                    cc.completion_percentage,
                    cc.before_summary,
                    cc.after_summary,
                    cc.quality_rating,
                    cc.environmental_benefit,
                    cc.verification_status,
                    cc.feedback,
                    cc.confidence as comparison_confidence
                FROM tasks t
                JOIN zones z ON t.zone_id = z.id
                LEFT JOIN users c ON t.cleaner_id = c.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN waste_analyses wa ON r.id = wa.report_id
                LEFT JOIN cleanup_comparisons cc ON r.id = cc.report_id
                {where_clause}
                ORDER BY t.created_at DESC
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
        
        # Convert timestamps to ISO format and amounts to float
        for task in tasks:
            task['reward'] = float(task['reward'])
            task['due_date'] = task['due_date'].isoformat() if task['due_date'] else None
            task['created_at'] = task['created_at'].isoformat() if task['created_at'] else None
            task['taken_at'] = task['taken_at'].isoformat() if task['taken_at'] else None
            task['completed_at'] = task['completed_at'].isoformat() if task['completed_at'] else None
        
        return jsonify({
            'success': True,
            'total': total,
            'data': tasks
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_tasks_bp.route('/tasks/<task_id>', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_task_details(task_id):
    """Get full details for a single task (includes full image URLs)."""
    try:
        with db_connection.get_cursor() as cursor:
            cursor.execute("""
                SELECT
                    t.id, t.report_id, t.zone_id, t.cleaner_id,
                    t.description, t.priority, t.status, t.reward,
                    t.due_date, t.created_at, t.taken_at, t.completed_at,
                    t.evidence_image_url,
                    z.name as zone_name,
                    c.name as cleaner_name,
                    r.image_url as before_image_url,
                    r.after_image_url as report_after_image_url,
                    wa.description as ai_description,
                    wa.severity as ai_severity,
                    wa.estimated_volume,
                    wa.environmental_impact,
                    wa.health_hazard,
                    wa.hazard_details,
                    wa.recommended_action,
                    wa.estimated_cleanup_time,
                    wa.confidence as ai_confidence,
                    (
                        SELECT COALESCE(
                            json_agg(
                                json_build_object(
                                    'waste_type', wc.waste_type,
                                    'percentage', wc.percentage,
                                    'recyclable', wc.recyclable
                                )
                            ),
                            '[]'::json
                        )
                        FROM waste_compositions wc
                        WHERE wa.id IS NOT NULL AND wc.waste_analysis_id = wa.id
                    ) as waste_composition,
                    (
                        SELECT COALESCE(
                            json_agg(se.equipment_name),
                            '[]'::json
                        )
                        FROM special_equipment se
                        WHERE wa.id IS NOT NULL AND se.waste_analysis_id = wa.id
                    ) as special_equipment_needed,
                    cc.completion_percentage,
                    cc.before_summary,
                    cc.after_summary,
                    cc.quality_rating,
                    cc.environmental_benefit,
                    cc.verification_status,
                    cc.feedback,
                    cc.confidence as comparison_confidence
                FROM tasks t
                JOIN zones z ON t.zone_id = z.id
                LEFT JOIN users c ON t.cleaner_id = c.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN waste_analyses wa ON r.id = wa.report_id
                LEFT JOIN cleanup_comparisons cc ON r.id = cc.report_id
                WHERE t.id = %s
                LIMIT 1
            """, (task_id,))
            task = cursor.fetchone()

        if not task:
            return jsonify({'success': False, 'error': 'Task not found'}), 404

        task['reward'] = float(task['reward'])
        task['due_date'] = task['due_date'].isoformat() if task.get('due_date') else None
        task['created_at'] = task['created_at'].isoformat() if task.get('created_at') else None
        task['taken_at'] = task['taken_at'].isoformat() if task.get('taken_at') else None
        task['completed_at'] = task['completed_at'].isoformat() if task.get('completed_at') else None

        return jsonify({'success': True, 'data': task}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_tasks_bp.route('/tasks', methods=['POST'])
@token_required
@role_required('ADMIN')
def create_manual_task():
    """Create a manual cleanup task (not from report)"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        admin_id = request.current_user['id']
        
        # Validate required fields
        required_fields = ['zone_id', 'description', 'priority', 'due_date', 'reward']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'{field} is required'}), 400
        
        # Validate priority
        valid_priorities = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
        if data['priority'] not in valid_priorities:
            return jsonify({'success': False, 'error': f'Priority must be one of: {", ".join(valid_priorities)}'}), 400
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Verify zone exists
            cursor.execute("""
                SELECT name FROM zones WHERE id = %s AND is_active = true
            """, (data['zone_id'],))
            zone = cursor.fetchone()
            
            if not zone:
                return jsonify({'success': False, 'error': 'Zone not found or inactive'}), 404
            
            # Create task
            cursor.execute("""
                INSERT INTO tasks (zone_id, description, priority, due_date, reward, created_by, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'APPROVED')
                RETURNING id, created_at
            """, (
                data['zone_id'], data['description'], data['priority'], 
                data['due_date'], data['reward'], admin_id
            ))
            new_task = cursor.fetchone()
        
        return jsonify({
            'success': True,
            'message': 'Task created successfully',
            'data': {
                'id': new_task['id'],
                'zone_name': zone['name'],
                'description': data['description'],
                'priority': data['priority'],
                'reward': float(data['reward']),
                'status': 'APPROVED',
                'created_at': new_task['created_at'].isoformat()
            }
        }), 201
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_tasks_bp.route('/tasks/<task_id>', methods=['PUT'])
@token_required
@role_required('ADMIN')
def update_task(task_id):
    """Update task details"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Prepare update data
        task_update = {}
        allowed_fields = ['description', 'priority', 'due_date', 'reward']
        
        for field in allowed_fields:
            if field in data:
                task_update[field] = data[field]
        
        if not task_update:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400
        
        # Validate priority if provided
        if 'priority' in task_update:
            valid_priorities = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
            if task_update['priority'] not in valid_priorities:
                return jsonify({'success': False, 'error': f'Priority must be one of: {", ".join(valid_priorities)}'}), 400
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Verify task exists and is not completed
            cursor.execute("""
                SELECT status FROM tasks WHERE id = %s
            """, (task_id,))
            task = cursor.fetchone()
            
            if not task:
                return jsonify({'success': False, 'error': 'Task not found'}), 404
            
            if task['status'] == 'COMPLETED':
                return jsonify({'success': False, 'error': 'Cannot update completed task'}), 400
            
            # Update task
            set_clause = ', '.join([f"{field} = %s" for field in task_update.keys()])
            values = list(task_update.values()) + [task_id]
            
            cursor.execute(f"""
                UPDATE tasks 
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING id, description, priority, reward, due_date, status
            """, values)
            updated_task = cursor.fetchone()
        
        # Convert values for response
        updated_task['reward'] = float(updated_task['reward'])
        updated_task['due_date'] = updated_task['due_date'].isoformat()
        
        return jsonify({
            'success': True,
            'message': 'Task updated successfully',
            'data': updated_task
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_tasks_bp.route('/tasks/<task_id>', methods=['DELETE'])
@token_required
@role_required('ADMIN')
def delete_task(task_id):
    """Delete a task (only if not taken)"""
    try:
        with db_connection.get_cursor(commit=True) as cursor:
            # Verify task exists and is not taken
            cursor.execute("""
                SELECT status, cleaner_id FROM tasks WHERE id = %s
            """, (task_id,))
            task = cursor.fetchone()
            
            if not task:
                return jsonify({'success': False, 'error': 'Task not found'}), 404
            
            if task['cleaner_id'] is not None:
                return jsonify({'success': False, 'error': 'Cannot delete task that has been taken'}), 400
            
            # Delete task
            cursor.execute("""
                DELETE FROM tasks WHERE id = %s
            """, (task_id,))
        
        return jsonify({
            'success': True,
            'message': 'Task deleted successfully'
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500