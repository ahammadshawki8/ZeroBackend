from flask import Blueprint, jsonify, request
from auth import token_required, role_required
from models import db_connection
import json

admin_zones_bp = Blueprint('admin_zones', __name__)


@admin_zones_bp.route('/zones', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_zones():
    """Get all zones with statistics"""
    try:
        # Get query parameters
        is_active = request.args.get('is_active')
        limit = request.args.get('limit', type=int)
        if limit is None:
            limit = 100
        limit = max(1, min(limit, 200))
        
        # Build query
        where_clause = "WHERE 1=1"
        params = []
        
        if is_active is not None:
            where_clause += " AND z.is_active = %s"
            params.append(is_active.lower() == 'true')
        
        with db_connection.get_cursor() as cursor:
            # Get zones with statistics
            query = f"""
                SELECT 
                    z.id, z.name, z.description, z.cleanliness_score, z.color, z.is_active,
                    z.created_at,
                    (SELECT COUNT(*) FROM reports WHERE zone_id = z.id) as total_reports,
                    (SELECT COUNT(*) FROM reports WHERE zone_id = z.id AND status = 'SUBMITTED') as pending_reports,
                    (SELECT COUNT(*) FROM tasks WHERE zone_id = z.id AND status IN ('APPROVED', 'IN_PROGRESS')) as active_tasks
                FROM zones z
                {where_clause}
                ORDER BY z.created_at DESC
                LIMIT %s
            """

            cursor.execute(query, params + [limit])
            zones = cursor.fetchall()
            
            # Get polygon points for each zone
            for zone in zones:
                cursor.execute("""
                    SELECT latitude, longitude, point_order
                    FROM zone_polygons
                    WHERE zone_id = %s
                    ORDER BY point_order
                """, (zone['id'],))
                polygon_points = cursor.fetchall()
                
                # Format polygon to match frontend expectations
                zone['polygon'] = [
                    {
                        'lat': float(point['latitude']),
                        'lng': float(point['longitude'])
                    }
                    for point in polygon_points
                ]
                zone['created_at'] = zone['created_at'].isoformat()
        
        return jsonify({
            'success': True,
            'data': zones
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_zones_bp.route('/zones', methods=['POST'])
@token_required
@role_required('ADMIN')
def create_zone():
    """Create a new service zone"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        admin_id = request.current_user['id']
        
        # Validate required fields
        required_fields = ['name']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'{field} is required'}), 400
        
        # Get polygon data (accept both formats)
        polygon_points = data.get('polygon') or data.get('polygon_points')
        if not polygon_points:
            return jsonify({'success': False, 'error': 'polygon or polygon_points is required'}), 400
        
        # Validate polygon points
        if not isinstance(polygon_points, list) or len(polygon_points) < 3:
            return jsonify({'success': False, 'error': 'polygon must be an array with at least 3 points'}), 400
        
        # Normalize polygon format (accept both lat/lng and latitude/longitude)
        normalized_points = []
        for i, point in enumerate(polygon_points):
            if not isinstance(point, dict):
                return jsonify({'success': False, 'error': f'Point {i} must be an object'}), 400
            
            lat = point.get('lat') or point.get('latitude')
            lng = point.get('lng') or point.get('longitude')
            
            if lat is None or lng is None:
                return jsonify({'success': False, 'error': f'Point {i} must have lat/lng or latitude/longitude'}), 400
            
            normalized_points.append({'latitude': lat, 'longitude': lng})
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Create zone
            cursor.execute("""
                INSERT INTO zones (name, description, color, created_by)
                VALUES (%s, %s, %s, %s)
                RETURNING id, created_at
            """, (
                data['name'], 
                data.get('description'), 
                data.get('color', '#3b82f6'), 
                admin_id
            ))
            new_zone = cursor.fetchone()
            
            # Insert polygon points
            for i, point in enumerate(normalized_points):
                cursor.execute("""
                    INSERT INTO zone_polygons (zone_id, point_order, latitude, longitude)
                    VALUES (%s, %s, %s, %s)
                """, (new_zone['id'], i, point['latitude'], point['longitude']))
        
        return jsonify({
            'success': True,
            'message': 'Zone created successfully',
            'data': {
                'id': new_zone['id'],
                'name': data['name'],
                'description': data.get('description'),
                'color': data.get('color', '#3b82f6'),
                'cleanliness_score': 100,
                'is_active': True,
                'created_at': new_zone['created_at'].isoformat(),
                'polygon': [
                    {
                        'lat': float(point['latitude']),
                        'lng': float(point['longitude'])
                    }
                    for point in normalized_points
                ]
            }
        }), 201
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_zones_bp.route('/zones/<zone_id>', methods=['PUT'])
@token_required
@role_required('ADMIN')
def update_zone(zone_id):
    """Update zone information"""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Prepare update data
        zone_update = {}
        allowed_fields = ['name', 'description', 'color', 'is_active']
        
        for field in allowed_fields:
            if field in data:
                zone_update[field] = data[field]
        
        if not zone_update and 'polygon_points' not in data and 'polygon' not in data:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400
        
        with db_connection.get_cursor(commit=True) as cursor:
            # Verify zone exists
            cursor.execute("""
                SELECT id FROM zones WHERE id = %s
            """, (zone_id,))
            zone = cursor.fetchone()
            
            if not zone:
                return jsonify({'success': False, 'error': 'Zone not found'}), 404
            
            # Update zone if there are zone fields
            if zone_update:
                set_clause = ', '.join([f"{field} = %s" for field in zone_update.keys()])
                values = list(zone_update.values()) + [zone_id]
                
                cursor.execute(f"""
                    UPDATE zones 
                    SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, values)
            
            # Update polygon points if provided
            if 'polygon' in data or 'polygon_points' in data:
                polygon_points = data.get('polygon') or data.get('polygon_points')
                
                # Validate polygon points
                if not isinstance(polygon_points, list) or len(polygon_points) < 3:
                    return jsonify({'success': False, 'error': 'polygon must be an array with at least 3 points'}), 400
                
                # Normalize polygon format
                normalized_points = []
                for i, point in enumerate(polygon_points):
                    if not isinstance(point, dict):
                        return jsonify({'success': False, 'error': f'Point {i} must be an object'}), 400
                    
                    lat = point.get('lat') or point.get('latitude')
                    lng = point.get('lng') or point.get('longitude')
                    
                    if lat is None or lng is None:
                        return jsonify({'success': False, 'error': f'Point {i} must have lat/lng or latitude/longitude'}), 400
                    
                    normalized_points.append({'latitude': lat, 'longitude': lng})
                
                # Delete existing points
                cursor.execute("""
                    DELETE FROM zone_polygons WHERE zone_id = %s
                """, (zone_id,))
                
                # Insert new points
                for i, point in enumerate(normalized_points):
                    cursor.execute("""
                        INSERT INTO zone_polygons (zone_id, point_order, latitude, longitude)
                        VALUES (%s, %s, %s, %s)
                    """, (zone_id, i, point['latitude'], point['longitude']))
            
            # Get updated zone with polygon
            cursor.execute("""
                SELECT id, name, description, color, is_active, cleanliness_score
                FROM zones WHERE id = %s
            """, (zone_id,))
            updated_zone = cursor.fetchone()
            
            # Get polygon points
            cursor.execute("""
                SELECT latitude, longitude, point_order
                FROM zone_polygons
                WHERE zone_id = %s
                ORDER BY point_order
            """, (zone_id,))
            polygon_points = cursor.fetchall()
            
            # Add polygon to response
            updated_zone['polygon'] = [
                {
                    'lat': float(point['latitude']),
                    'lng': float(point['longitude'])
                }
                for point in polygon_points
            ]
        
        return jsonify({
            'success': True,
            'message': 'Zone updated successfully',
            'data': updated_zone
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_zones_bp.route('/zones/<zone_id>', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_zone_details(zone_id):
    """Get detailed zone information with statistics"""
    try:
        with db_connection.get_cursor() as cursor:
            # Get zone with statistics
            cursor.execute("""
                SELECT 
                    z.id, z.name, z.description, z.cleanliness_score, z.color, z.is_active,
                    z.created_at,
                    (SELECT COUNT(*) FROM reports WHERE zone_id = z.id) as total_reports,
                    (SELECT COUNT(*) FROM reports WHERE zone_id = z.id AND status = 'SUBMITTED') as pending_reports,
                    (SELECT COUNT(*) FROM reports WHERE zone_id = z.id AND status = 'COMPLETED') as completed_reports,
                    (SELECT COUNT(*) FROM tasks WHERE zone_id = z.id AND status IN ('APPROVED', 'IN_PROGRESS')) as active_tasks,
                    (SELECT AVG(EXTRACT(EPOCH FROM (completed_at - created_at))/86400) 
                     FROM reports WHERE zone_id = z.id AND status = 'COMPLETED') as avg_completion_days
                FROM zones z
                WHERE z.id = %s
            """, (zone_id,))
            zone = cursor.fetchone()
            
            if not zone:
                return jsonify({'success': False, 'error': 'Zone not found'}), 404
            
            # Get polygon points
            cursor.execute("""
                SELECT latitude, longitude, point_order
                FROM zone_polygons
                WHERE zone_id = %s
                ORDER BY point_order
            """, (zone_id,))
            polygon_points = cursor.fetchall()
            
            # Structure response
            response_data = {
                'zone': {
                    'id': zone['id'],
                    'name': zone['name'],
                    'description': zone['description'],
                    'cleanliness_score': zone['cleanliness_score'],
                    'color': zone['color'],
                    'is_active': zone['is_active'],
                    'created_at': zone['created_at'].isoformat()
                },
                'statistics': {
                    'total_reports': zone['total_reports'],
                    'pending_reports': zone['pending_reports'],
                    'completed_reports': zone['completed_reports'],
                    'active_tasks': zone['active_tasks'],
                    'avg_completion_time': f"{zone['avg_completion_days']:.1f} days" if zone['avg_completion_days'] else "N/A"
                },
                'polygon': [
                    {
                        'lat': float(point['latitude']),
                        'lng': float(point['longitude'])
                    }
                    for point in polygon_points
                ]
            }
        
        return jsonify({
            'success': True,
            'data': response_data
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_zones_bp.route('/zones/<zone_id>', methods=['DELETE'])
@token_required
@role_required('ADMIN')
def delete_zone(zone_id):
    """Delete a zone (only if no active reports/tasks)"""
    try:
        with db_connection.get_cursor(commit=True) as cursor:
            # Check if zone has active reports or tasks
            cursor.execute("""
                SELECT 
                    (SELECT COUNT(*) FROM reports WHERE zone_id = %s AND status NOT IN ('COMPLETED', 'DECLINED')) as active_reports,
                    (SELECT COUNT(*) FROM tasks WHERE zone_id = %s AND status != 'COMPLETED') as active_tasks
            """, (zone_id, zone_id))
            counts = cursor.fetchone()
            
            if counts['active_reports'] > 0 or counts['active_tasks'] > 0:
                return jsonify({
                    'success': False, 
                    'error': 'Cannot delete zone with active reports or tasks'
                }), 400
            
            # Delete zone (cascade will handle polygon points)
            cursor.execute("""
                DELETE FROM zones WHERE id = %s
            """, (zone_id,))
            
            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Zone not found'}), 404
        
        return jsonify({
            'success': True,
            'message': 'Zone deleted successfully'
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500