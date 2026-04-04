from flask import Blueprint, jsonify, request
from auth import token_required, role_required
from models import db_connection
from decimal import Decimal
from threading import Lock
import time

admin_payments_bp = Blueprint('admin_payments', __name__)

_funding_tables_ready = False
_funding_tables_lock = Lock()
_wallet_operation_lock = Lock()
_payment_summary_cache = {'expires_at': 0.0, 'payload': None}
_payment_summary_cache_lock = Lock()


def _ensure_funding_tables(cursor):
    """Create wallet tables lazily so feature works without manual migrations."""
    global _funding_tables_ready
    if _funding_tables_ready:
        return

    with _funding_tables_lock:
        if _funding_tables_ready:
            return

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_funds (
                id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
                current_balance DECIMAL(12,2) NOT NULL DEFAULT 0,
                total_added DECIMAL(12,2) NOT NULL DEFAULT 0,
                total_paid DECIMAL(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_fund_transactions (
                id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
                type VARCHAR(20) NOT NULL,
                amount DECIMAL(12,2) NOT NULL,
                balance_after DECIMAL(12,2) NOT NULL,
                reference_type VARCHAR(30),
                reference_id VARCHAR(36),
                note TEXT,
                created_by VARCHAR(36) REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_system_fund_transactions_created_at
            ON system_fund_transactions(created_at DESC)
        """)

        cursor.execute("""
            INSERT INTO system_funds (current_balance, total_added, total_paid)
            SELECT 0, 0, 0
            WHERE NOT EXISTS (SELECT 1 FROM system_funds)
        """)

        _funding_tables_ready = True


def _get_wallet_row(cursor, lock=False):
    lock_clause = "FOR UPDATE" if lock else ""
    cursor.execute(f"""
        SELECT id, current_balance, total_added, total_paid
        FROM system_funds
        ORDER BY created_at ASC
        LIMIT 1
        {lock_clause}
    """)
    return cursor.fetchone()


def _amount_to_float(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


@admin_payments_bp.route('/payments/pending', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_pending_payments():
    """Get pending cleaner payout promises with completion evidence."""
    try:
        # Get query parameters
        cleaner_id = request.args.get('cleaner_id')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        
        # Build query
        where_clause = "WHERE et.status = 'PENDING'"
        params = []
        
        if cleaner_id:
            where_clause += " AND et.cleaner_id = %s"
            params.append(cleaner_id)
        
        with db_connection.get_cursor() as cursor:
            _ensure_funding_tables(cursor)

            # Get pending payments
            cursor.execute(f"""
                SELECT 
                    et.id, et.amount, et.created_at,
                    et.cleaner_id, et.task_id,
                    u.name as cleaner_name, u.email as cleaner_email,
                    t.description as task_description, t.completed_at as task_completed_at,
                    t.status as task_status,
                    r.id as report_id,
                    r.description as report_description,
                    CASE WHEN r.image_url LIKE 'data:%%' THEN NULL ELSE r.image_url END AS before_image_url,
                    CASE WHEN r.after_image_url LIKE 'data:%%' THEN NULL ELSE r.after_image_url END AS after_image_url,
                    r.latitude,
                    r.longitude,
                    cr.rating as review_rating,
                    cr.comment as review_comment,
                    cr.created_at as review_created_at,
                    ccp.completion_percentage,
                    ccp.quality_rating,
                    ccp.verification_status
                FROM earnings_transactions et
                JOIN users u ON et.cleaner_id = u.id
                JOIN tasks t ON et.task_id = t.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN cleanup_reviews cr ON r.id = cr.report_id
                LEFT JOIN cleanup_comparisons ccp ON r.id = ccp.report_id
                {where_clause}
                ORDER BY et.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            transactions = cursor.fetchall()
            
            # Get summary
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(amount) as total_amount
                FROM earnings_transactions et
                {where_clause}
            """, params)
            summary = cursor.fetchone()

            wallet = _get_wallet_row(cursor, lock=False)
        
        # Convert timestamps to ISO format and amounts to float
        for transaction in transactions:
            transaction['amount'] = _amount_to_float(transaction['amount'])
            transaction['created_at'] = transaction['created_at'].isoformat()
            transaction['task_completed_at'] = transaction['task_completed_at'].isoformat() if transaction.get('task_completed_at') else None
            transaction['review_created_at'] = transaction['review_created_at'].isoformat() if transaction.get('review_created_at') else None
            if transaction.get('latitude') is not None and transaction.get('longitude') is not None:
                transaction['location'] = {
                    'lat': float(transaction['latitude']),
                    'lng': float(transaction['longitude'])
                }
            else:
                transaction['location'] = None
        
        return jsonify({
            'success': True,
            'total': summary['total'],
            'total_amount': _amount_to_float(summary['total_amount']),
            'wallet_balance': _amount_to_float(wallet['current_balance']) if wallet else 0,
            'data': transactions
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_payments_bp.route('/payments/pending/<transaction_id>', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_pending_payment_details(transaction_id):
    """Get full payout review details (including before/after images) for a single transaction."""
    try:
        with db_connection.get_cursor() as cursor:
            _ensure_funding_tables(cursor)

            cursor.execute("""
                SELECT
                    et.id, et.amount, et.created_at,
                    et.cleaner_id, et.task_id,
                    u.name as cleaner_name, u.email as cleaner_email,
                    t.description as task_description, t.completed_at as task_completed_at,
                    t.status as task_status,
                    r.id as report_id,
                    r.description as report_description,
                    r.image_url as before_image_url,
                    r.after_image_url as after_image_url,
                    r.latitude,
                    r.longitude,
                    cr.rating as review_rating,
                    cr.comment as review_comment,
                    cr.created_at as review_created_at,
                    ccp.completion_percentage,
                    ccp.quality_rating,
                    ccp.verification_status
                FROM earnings_transactions et
                JOIN users u ON et.cleaner_id = u.id
                JOIN tasks t ON et.task_id = t.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN cleanup_reviews cr ON r.id = cr.report_id
                LEFT JOIN cleanup_comparisons ccp ON r.id = ccp.report_id
                WHERE et.id = %s
                  AND et.status = 'PENDING'
                LIMIT 1
            """, (transaction_id,))
            transaction = cursor.fetchone()

        if not transaction:
            return jsonify({'success': False, 'error': 'Pending transaction not found'}), 404

        transaction['amount'] = _amount_to_float(transaction['amount'])
        transaction['created_at'] = transaction['created_at'].isoformat() if transaction.get('created_at') else None
        transaction['task_completed_at'] = transaction['task_completed_at'].isoformat() if transaction.get('task_completed_at') else None
        transaction['review_created_at'] = transaction['review_created_at'].isoformat() if transaction.get('review_created_at') else None

        if transaction.get('latitude') is not None and transaction.get('longitude') is not None:
            transaction['location'] = {
                'lat': float(transaction['latitude']),
                'lng': float(transaction['longitude'])
            }
        else:
            transaction['location'] = None

        return jsonify({'success': True, 'data': transaction}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_payments_bp.route('/payments/process', methods=['POST'])
@token_required
@role_required('ADMIN')
def process_payments():
    """Confirm pending cleaner payouts after admin verification."""
    lock_acquired = False
    try:
        lock_acquired = _wallet_operation_lock.acquire(blocking=False)
        if not lock_acquired:
            return jsonify({
                'success': False,
                'error': 'Another wallet operation is currently in progress. Please retry in a moment.'
            }), 429

        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        admin_id = request.current_user['id']
        
        # Validate required fields
        if not data.get('transaction_ids') or not isinstance(data['transaction_ids'], list):
            return jsonify({'success': False, 'error': 'transaction_ids array is required'}), 400
        
        transaction_ids = data['transaction_ids']

        # Phase 1: validate candidate payouts in read-only mode (no wallet lock yet).
        with db_connection.get_cursor() as cursor:
            _ensure_funding_tables(cursor)

            placeholders = ', '.join(['%s'] * len(transaction_ids))
            cursor.execute(f"""
                SELECT et.id, et.amount, et.task_id, t.status as task_status
                FROM earnings_transactions et
                JOIN tasks t ON t.id = et.task_id
                WHERE et.id IN ({placeholders})
                  AND et.status = 'PENDING'
            """, transaction_ids)
            pending_rows = cursor.fetchall()

        if not pending_rows:
            return jsonify({'success': False, 'error': 'No pending payments found for selected transactions'}), 400

        not_completed = [row['id'] for row in pending_rows if row.get('task_status') != 'COMPLETED']
        if not_completed:
            return jsonify({
                'success': False,
                'error': 'Some selected tasks are not completed yet',
                'data': {'invalid_transaction_ids': not_completed}
            }), 400

        paid_ids = [row['id'] for row in pending_rows]
        expected_total_amount = sum(_amount_to_float(row['amount']) for row in pending_rows)

        # Phase 2: short critical section with wallet row lock and atomic payout update.
        with db_connection.get_cursor(commit=True) as cursor:
            _ensure_funding_tables(cursor)
            wallet = _get_wallet_row(cursor, lock=True)

            current_balance = _amount_to_float(wallet['current_balance']) if wallet else 0
            if current_balance < expected_total_amount:
                return jsonify({
                    'success': False,
                    'error': 'Insufficient system balance. Please add funds before confirming payout.',
                    'data': {
                        'required_amount': expected_total_amount,
                        'available_balance': current_balance,
                        'shortfall': expected_total_amount - current_balance,
                    }
                }), 400

            cursor.execute("""
                UPDATE earnings_transactions
                SET status = 'PAID',
                    paid_at = CURRENT_TIMESTAMP,
                    paid_by = %s
                WHERE id = ANY(%s)
                  AND status = 'PENDING'
                RETURNING id, amount
            """, (admin_id, paid_ids))
            paid_rows = cursor.fetchall()

            if not paid_rows:
                return jsonify({'success': False, 'error': 'Selected transactions are no longer pending'}), 409

            paid_ids = [row['id'] for row in paid_rows]
            total_amount = sum(_amount_to_float(row['amount']) for row in paid_rows)
            new_balance = current_balance - total_amount

            cursor.execute("""
                UPDATE system_funds
                SET current_balance = %s,
                    total_paid = total_paid + %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (new_balance, total_amount, wallet['id']))

            cursor.execute("""
                INSERT INTO system_fund_transactions (
                    type, amount, balance_after, reference_type, note, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                'PAYOUT',
                total_amount,
                new_balance,
                'EARNINGS_TRANSACTIONS',
                f'Payout confirmation for {len(paid_ids)} cleaner transaction(s)',
                admin_id
            ))

            processed_count = len(paid_ids)
        
        return jsonify({
            'success': True,
            'message': 'Payments processed successfully',
            'data': {
                'processed_count': processed_count,
                'total_amount': total_amount,
                'remaining_balance': new_balance,
                'transaction_ids': paid_ids
            }
        }), 200
    
    except Exception as e:
        if "couldn't get a connection" in str(e).lower():
            return jsonify({'success': False, 'error': 'Payment service is busy. Please retry in a few seconds.'}), 503
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if lock_acquired:
            try:
                _wallet_operation_lock.release()
            except RuntimeError:
                pass


@admin_payments_bp.route('/payments/history', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_payment_history():
    """Get payment history with filtering"""
    try:
        # Get query parameters
        status = request.args.get('status')
        cleaner_id = request.args.get('cleaner_id')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        
        # Build query
        where_clause = "WHERE 1=1"
        params = []
        
        if status:
            where_clause += " AND et.status = %s"
            params.append(status)
        
        if cleaner_id:
            where_clause += " AND et.cleaner_id = %s"
            params.append(cleaner_id)
        
        if start_date:
            where_clause += " AND et.created_at >= %s"
            params.append(start_date)
        
        if end_date:
            where_clause += " AND et.created_at <= %s"
            params.append(end_date)
        
        with db_connection.get_cursor() as cursor:
            # Get payment history
            cursor.execute(f"""
                SELECT 
                    et.id, et.amount, et.status, et.created_at, et.paid_at,
                    u.name as cleaner_name, u.email as cleaner_email,
                    t.description as task_description,
                    CASE WHEN r.image_url LIKE 'data:%%' THEN NULL ELSE r.image_url END AS before_image_url,
                    CASE WHEN r.after_image_url LIKE 'data:%%' THEN NULL ELSE r.after_image_url END AS after_image_url,
                    admin_u.name as paid_by_name
                FROM earnings_transactions et
                JOIN users u ON et.cleaner_id = u.id
                JOIN tasks t ON et.task_id = t.id
                LEFT JOIN reports r ON t.report_id = r.id
                LEFT JOIN users admin_u ON et.paid_by = admin_u.id
                {where_clause}
                ORDER BY et.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            transactions = cursor.fetchall()
            
            # Get summary
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(amount) as total_amount,
                    SUM(CASE WHEN status = 'PAID' THEN amount ELSE 0 END) as paid_amount,
                    SUM(CASE WHEN status = 'PENDING' THEN amount ELSE 0 END) as pending_amount
                FROM earnings_transactions et
                {where_clause}
            """, params)
            summary = cursor.fetchone()
        
        # Convert timestamps to ISO format and amounts to float
        for transaction in transactions:
            transaction['amount'] = float(transaction['amount'])
            transaction['created_at'] = transaction['created_at'].isoformat()
            transaction['paid_at'] = transaction['paid_at'].isoformat() if transaction['paid_at'] else None
        
        return jsonify({
            'success': True,
            'total': summary['total'],
            'total_amount': float(summary['total_amount']) if summary['total_amount'] else 0,
            'paid_amount': float(summary['paid_amount']) if summary['paid_amount'] else 0,
            'pending_amount': float(summary['pending_amount']) if summary['pending_amount'] else 0,
            'data': transactions
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_payments_bp.route('/payments/summary', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_payment_summary():
    """Get payout summary plus current system wallet status."""
    try:
        now = time.time()
        with _payment_summary_cache_lock:
            if _payment_summary_cache['payload'] is not None and now < _payment_summary_cache['expires_at']:
                return jsonify({'success': True, 'data': _payment_summary_cache['payload']}), 200

        with db_connection.get_cursor() as cursor:
            _ensure_funding_tables(cursor)

            # Get overall payment statistics
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_transactions,
                    SUM(amount) as total_amount,
                    SUM(CASE WHEN status = 'PAID' THEN amount ELSE 0 END) as paid_amount,
                    SUM(CASE WHEN status = 'PENDING' THEN amount ELSE 0 END) as pending_amount,
                    COUNT(CASE WHEN status = 'PAID' THEN 1 END) as paid_transactions,
                    COUNT(CASE WHEN status = 'PENDING' THEN 1 END) as pending_transactions,
                    AVG(amount) as avg_transaction_amount
                FROM earnings_transactions
            """)
            overall_stats = cursor.fetchone()
            
            # Get monthly statistics for current year
            cursor.execute("""
                SELECT 
                    EXTRACT(MONTH FROM created_at) as month,
                    COUNT(*) as transactions,
                    SUM(amount) as total_amount,
                    SUM(CASE WHEN status = 'PAID' THEN amount ELSE 0 END) as paid_amount
                FROM earnings_transactions
                WHERE EXTRACT(YEAR FROM created_at) = EXTRACT(YEAR FROM CURRENT_DATE)
                GROUP BY EXTRACT(MONTH FROM created_at)
                ORDER BY month
            """)
            monthly_stats = cursor.fetchall()
            
            # Get top cleaners by earnings
            cursor.execute("""
                SELECT 
                    u.name as cleaner_name,
                    COUNT(*) as total_transactions,
                    SUM(et.amount) as total_earnings,
                    AVG(et.amount) as avg_earning
                FROM earnings_transactions et
                JOIN users u ON et.cleaner_id = u.id
                WHERE et.status = 'PAID'
                GROUP BY u.id, u.name
                ORDER BY total_earnings DESC
                LIMIT 10
            """)
            top_cleaners = cursor.fetchall()

            wallet = _get_wallet_row(cursor, lock=False)
        
        # Convert amounts to float
        for key in ['total_amount', 'paid_amount', 'pending_amount', 'avg_transaction_amount']:
            if overall_stats[key]:
                overall_stats[key] = float(overall_stats[key])
        
        for stat in monthly_stats:
            stat['total_amount'] = float(stat['total_amount'])
            stat['paid_amount'] = float(stat['paid_amount'])
        
        for cleaner in top_cleaners:
            cleaner['total_earnings'] = float(cleaner['total_earnings'])
            cleaner['avg_earning'] = float(cleaner['avg_earning'])
        
        payload = {
            'overall': overall_stats,
            'monthly': monthly_stats,
            'top_cleaners': top_cleaners,
            'wallet': {
                'current_balance': _amount_to_float(wallet['current_balance']) if wallet else 0,
                'total_added': _amount_to_float(wallet['total_added']) if wallet else 0,
                'total_paid': _amount_to_float(wallet['total_paid']) if wallet else 0,
            }
        }

        with _payment_summary_cache_lock:
            _payment_summary_cache['payload'] = payload
            _payment_summary_cache['expires_at'] = time.time() + 5.0

        return jsonify({'success': True, 'data': payload}), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_payments_bp.route('/payments/top-up', methods=['POST'])
@token_required
@role_required('ADMIN')
def top_up_system_funds():
    """Mock gateway endpoint to add money to system wallet."""
    lock_acquired = False
    try:
        lock_acquired = _wallet_operation_lock.acquire(blocking=False)
        if not lock_acquired:
            return jsonify({
                'success': False,
                'error': 'Another wallet operation is currently in progress. Please retry in a moment.'
            }), 429

        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        admin_id = request.current_user['id']
        amount = data.get('amount')

        if amount is None:
            return jsonify({'success': False, 'error': 'amount is required'}), 400

        try:
            amount_decimal = Decimal(str(amount))
        except Exception:
            return jsonify({'success': False, 'error': 'amount must be numeric'}), 400

        if amount_decimal <= 0:
            return jsonify({'success': False, 'error': 'amount must be greater than 0'}), 400

        note = data.get('note') or 'Manual top-up via mock gateway'

        with db_connection.get_cursor(commit=True) as cursor:
            _ensure_funding_tables(cursor)
            wallet = _get_wallet_row(cursor, lock=True)

            new_balance = _amount_to_float(wallet['current_balance']) + float(amount_decimal)

            cursor.execute("""
                UPDATE system_funds
                SET current_balance = %s,
                    total_added = total_added + %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (new_balance, float(amount_decimal), wallet['id']))

            cursor.execute("""
                INSERT INTO system_fund_transactions (
                    type, amount, balance_after, reference_type, note, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                'TOP_UP',
                float(amount_decimal),
                new_balance,
                'MOCK_GATEWAY',
                note,
                admin_id
            ))

        return jsonify({
            'success': True,
            'message': 'System wallet topped up successfully',
            'data': {
                'amount_added': float(amount_decimal),
                'current_balance': new_balance,
                'note': note,
            }
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if lock_acquired:
            try:
                _wallet_operation_lock.release()
            except RuntimeError:
                pass


@admin_payments_bp.route('/payments/funds/history', methods=['GET'])
@token_required
@role_required('ADMIN')
def get_fund_transaction_history():
    """Get wallet ledger (top-ups and payout deductions)."""
    try:
        limit = request.args.get('limit', type=int, default=50)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        with db_connection.get_cursor() as cursor:
            _ensure_funding_tables(cursor)

            cursor.execute("""
                SELECT
                    sft.id, sft.type, sft.amount, sft.balance_after, sft.reference_type,
                    sft.reference_id, sft.note, sft.created_at,
                    u.name as created_by_name
                FROM system_fund_transactions sft
                LEFT JOIN users u ON sft.created_by = u.id
                ORDER BY sft.created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cursor.fetchall()

            cursor.execute("""
                SELECT COUNT(*) as total
                FROM system_fund_transactions
            """)
            total_result = cursor.fetchone()

        for row in rows:
            row['amount'] = _amount_to_float(row['amount'])
            row['balance_after'] = _amount_to_float(row['balance_after'])
            row['created_at'] = row['created_at'].isoformat() if row.get('created_at') else None

        return jsonify({
            'success': True,
            'total': total_result['total'] if total_result else 0,
            'data': rows,
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500