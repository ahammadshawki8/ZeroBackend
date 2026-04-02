from flask import jsonify, request

from auth import token_required, role_required
from models import db_connection

from cleaner_blueprint import cleaner_bp
from cleaner_helpers import _ensure_cleaner_withdrawals_table, _to_float

@cleaner_bp.route('/earnings', methods=['GET'])
@token_required
@role_required('CLEANER')
def get_earnings_history():
    """Get earnings transaction history"""
    try:
        user_id = request.current_user['id']
        
        # Get query parameters
        status = request.args.get('status')
        limit = request.args.get('limit', type=int, default=20)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        
        # Build query
        where_clause = "WHERE et.cleaner_id = %s"
        params = [user_id]
        
        if status:
            where_clause += " AND et.status = %s"
            params.append(status)
        
        with db_connection.get_cursor() as cursor:
            # Get transactions
            cursor.execute(f"""
                SELECT 
                    et.id, et.amount, et.status, et.created_at, et.paid_at,
                    t.id as task_id, t.description as task_description
                FROM earnings_transactions et
                JOIN tasks t ON et.task_id = t.id
                {where_clause}
                ORDER BY et.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            transactions = cursor.fetchall()
            
            # Get summary
            cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(amount) as total_earnings,
                    SUM(CASE WHEN status = 'PENDING' THEN amount ELSE 0 END) as pending_earnings
                FROM earnings_transactions
                WHERE cleaner_id = %s
            """, (user_id,))
            summary = cursor.fetchone()
        
        # Convert timestamps to ISO format
        for transaction in transactions:
            transaction['created_at'] = transaction['created_at'].isoformat()
            transaction['paid_at'] = transaction['paid_at'].isoformat() if transaction['paid_at'] else None
            transaction['amount'] = float(transaction['amount'])
        
        return jsonify({
            'success': True,
            'total': summary['total'],
            'total_earnings': float(summary['total_earnings']) if summary['total_earnings'] else 0,
            'pending_earnings': float(summary['pending_earnings']) if summary['pending_earnings'] else 0,
            'data': transactions
        }), 200
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/payments/summary', methods=['GET'])
@token_required
@role_required('CLEANER')
def get_payment_summary():
    """Get cleaner wallet summary (available balance updates when admin confirms payouts)."""
    try:
        user_id = request.current_user['id']

        with db_connection.get_cursor() as cursor:
            _ensure_cleaner_withdrawals_table(cursor)

            cursor.execute("""
                SELECT user_id
                FROM cleaner_profiles
                WHERE user_id = %s
            """, (user_id,))
            profile = cursor.fetchone()

            if not profile:
                return jsonify({'success': False, 'error': 'Cleaner profile not found'}), 404

            # Status-driven payout lifecycle:
            # COMPLETED task -> earnings_transactions.PENDING (pending promise increases)
            # ADMIN payment confirm -> status becomes PAID (pending decreases, available increases)
            # Cleaner withdrawal -> processed withdrawals increase, available decreases
            cursor.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'PENDING' THEN amount ELSE 0 END), 0) as pending_promises,
                    COALESCE(SUM(CASE WHEN status = 'PAID' THEN amount ELSE 0 END), 0) as paid_total,
                    COALESCE(SUM(amount), 0) as total_earnings
                FROM earnings_transactions
                WHERE cleaner_id = %s
            """, (user_id,))
            earnings = cursor.fetchone()

            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0) as withdrawn_total
                FROM cleaner_withdrawals
                WHERE cleaner_id = %s AND status = 'PROCESSED'
            """, (user_id,))
            withdrawals = cursor.fetchone()

            total_earnings = _to_float(earnings.get('total_earnings') if earnings else 0)
            pending_promises = _to_float(earnings.get('pending_promises') if earnings else 0)
            paid_total = _to_float(earnings.get('paid_total') if earnings else 0)
            withdrawn_total = _to_float(withdrawals.get('withdrawn_total') if withdrawals else 0)
            available_balance = max(0.0, paid_total - withdrawn_total)

        return jsonify({
            'success': True,
            'data': {
                'total_earnings': total_earnings,
                'paid_total': paid_total,
                'pending_promises': pending_promises,
                'withdrawn_total': withdrawn_total,
                'available_balance': available_balance,
            }
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/payments/withdraw', methods=['POST'])
@token_required
@role_required('CLEANER')
def request_withdrawal():
    """Withdraw available cleaner balance to BKASH/BANK/CARD (mock transfer)."""
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = request.current_user['id']

        amount_raw = data.get('amount')
        method = str(data.get('method') or '').upper()
        destination_account = str(data.get('destination_account') or '').strip()
        note = data.get('note')
        reference_code = str(data.get('reference_code') or '').strip() or None

        if amount_raw is None:
            return jsonify({'success': False, 'error': 'amount is required'}), 400

        try:
            amount = float(amount_raw)
        except Exception:
            return jsonify({'success': False, 'error': 'amount must be numeric'}), 400

        if amount <= 0:
            return jsonify({'success': False, 'error': 'amount must be greater than 0'}), 400

        if method not in ['BKASH', 'BANK', 'CARD']:
            return jsonify({'success': False, 'error': 'method must be one of BKASH, BANK, CARD'}), 400

        if not destination_account:
            return jsonify({'success': False, 'error': 'destination_account is required'}), 400

        with db_connection.get_cursor(commit=True) as cursor:
            _ensure_cleaner_withdrawals_table(cursor)

            cursor.execute("""
                SELECT user_id
                FROM cleaner_profiles
                WHERE user_id = %s
                FOR UPDATE
            """, (user_id,))
            profile = cursor.fetchone()

            if not profile:
                return jsonify({'success': False, 'error': 'Cleaner profile not found'}), 404

            cursor.execute("""
                SELECT COALESCE(SUM(CASE WHEN status = 'PAID' THEN amount ELSE 0 END), 0) as paid_total
                FROM earnings_transactions
                WHERE cleaner_id = %s
            """, (user_id,))
            paid_row = cursor.fetchone()

            cursor.execute("""
                SELECT COALESCE(SUM(amount), 0) as withdrawn_total
                FROM cleaner_withdrawals
                WHERE cleaner_id = %s AND status = 'PROCESSED'
            """, (user_id,))
            withdrawals = cursor.fetchone()

            paid_total = _to_float(paid_row.get('paid_total') if paid_row else 0)
            withdrawn_total = _to_float(withdrawals.get('withdrawn_total') if withdrawals else 0)
            available_balance = max(0.0, paid_total - withdrawn_total)

            if amount > available_balance:
                return jsonify({
                    'success': False,
                    'error': 'Insufficient available balance',
                    'data': {
                        'available_balance': available_balance,
                        'requested_amount': amount,
                        'shortfall': amount - available_balance,
                    }
                }), 400

            cursor.execute("""
                INSERT INTO cleaner_withdrawals (
                    cleaner_id, amount, method, destination_account,
                    reference_code, note, status, processed_at, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'PROCESSED', CURRENT_TIMESTAMP, %s)
                RETURNING id, requested_at, processed_at
            """, (
                user_id,
                amount,
                method,
                destination_account,
                reference_code,
                note,
                user_id,
            ))
            withdrawal = cursor.fetchone()

            new_available_balance = max(0.0, available_balance - amount)

        return jsonify({
            'success': True,
            'message': 'Withdrawal request processed successfully',
            'data': {
                'id': withdrawal['id'],
                'amount': amount,
                'method': method,
                'destination_account': destination_account,
                'status': 'PROCESSED',
                'requested_at': withdrawal['requested_at'].isoformat() if withdrawal.get('requested_at') else None,
                'processed_at': withdrawal['processed_at'].isoformat() if withdrawal.get('processed_at') else None,
                'available_balance': new_available_balance,
            }
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@cleaner_bp.route('/payments/history', methods=['GET'])
@token_required
@role_required('CLEANER')
def get_withdrawal_history():
    """Get cleaner payment timeline (admin-paid earnings + withdrawals)."""
    try:
        user_id = request.current_user['id']
        limit = request.args.get('limit', type=int, default=30)
        offset = request.args.get('offset', type=int, default=0)

        limit = max(1, min(limit, 50))
        offset = max(0, offset)

        with db_connection.get_cursor() as cursor:
            _ensure_cleaner_withdrawals_table(cursor)

            cursor.execute("""
                SELECT *
                FROM (
                    SELECT
                        CONCAT('WITHDRAW-', cw.id::text) AS id,
                        'WITHDRAWAL'::text AS event_type,
                        cw.amount AS amount,
                        cw.status::text AS status,
                        cw.method::text AS method,
                        cw.destination_account::text AS destination_account,
                        cw.reference_code::text AS reference_code,
                        cw.note::text AS note,
                        cw.requested_at AS event_at,
                        cw.processed_at AS processed_at,
                        NULL::text AS task_id,
                        NULL::text AS task_description
                    FROM cleaner_withdrawals cw
                    WHERE cw.cleaner_id = %s

                    UNION ALL

                    SELECT
                        CONCAT('EARN-', et.id::text) AS id,
                        'ADMIN_PAYMENT'::text AS event_type,
                        et.amount AS amount,
                        et.status::text AS status,
                        'ADMIN'::text AS method,
                        NULL::text AS destination_account,
                        NULL::text AS reference_code,
                        CONCAT('Task payout: ', COALESCE(t.description, 'Task'))::text AS note,
                        COALESCE(et.paid_at, et.created_at) AS event_at,
                        et.paid_at AS processed_at,
                        et.task_id::text AS task_id,
                        t.description::text AS task_description
                    FROM earnings_transactions et
                    LEFT JOIN tasks t ON t.id = et.task_id
                    WHERE et.cleaner_id = %s
                      AND et.status = 'PAID'
                ) timeline
                ORDER BY event_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, user_id, limit, offset))
            rows = cursor.fetchall()

            cursor.execute("""
                SELECT
                    (
                        (SELECT COUNT(*) FROM cleaner_withdrawals WHERE cleaner_id = %s)
                        +
                        (SELECT COUNT(*) FROM earnings_transactions WHERE cleaner_id = %s AND status = 'PAID')
                    ) AS total
            """, (user_id, user_id))
            total_result = cursor.fetchone()

        for row in rows:
            row['amount'] = _to_float(row.get('amount'))
            row['event_at'] = row['event_at'].isoformat() if row.get('event_at') else None
            row['processed_at'] = row['processed_at'].isoformat() if row.get('processed_at') else None

        return jsonify({
            'success': True,
            'total': total_result['total'] if total_result else 0,
            'data': rows
        }), 200

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


