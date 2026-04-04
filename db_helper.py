try:
    # Try psycopg3 first (for Python 3.12+)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool, PoolTimeout
    PSYCOPG_VERSION = 3
except ImportError:
    # Fall back to psycopg2 (for Python < 3.12)
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    PSYCOPG_VERSION = 2

from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Tuple
import os
import time
from threading import Lock

try:
    from flask import has_request_context, request
except Exception:
    has_request_context = None
    request = None

if PSYCOPG_VERSION == 3:
    DB_OPERATIONAL_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)
else:
    DB_OPERATIONAL_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def _is_transient_db_error(error: Exception) -> bool:
    """Identify connection-level failures that are safe to retry for read operations."""
    text = str(error).lower()
    transient_markers = [
        'connection is lost',
        'server closed the connection unexpectedly',
        'bad record mac',
        'consuming input failed',
        'ssl error',
        'terminating connection',
        'connection not open',
        'could not receive data from server',
    ]
    return any(marker in text for marker in transient_markers)


def _diag_enabled() -> bool:
    return os.getenv('DB_DIAG_LOG', 'true').lower() == 'true'


def _diag_acquire_warn_ms() -> float:
    return float(os.getenv('DB_DIAG_ACQUIRE_WARN_MS', '150'))


def _diag_hold_warn_ms() -> float:
    return float(os.getenv('DB_DIAG_HOLD_WARN_MS', '1000'))


def _request_label() -> str:
    try:
        if has_request_context and has_request_context() and request is not None:
            return f"{request.method} {request.path}"
    except Exception:
        pass
    return 'non-request-context'

class DatabaseConfig:
    """Holds connection configuration details for establishing PostgreSQL connections."""

    def __init__(self, host="localhost", port=5432, database="mydb",
                 user="postgres", password="password"):
        """Initialize config values for the database connection."""
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    def get_cursor_string(self):
        """Return a connection string compatible with psycopg2 cursor usage."""
        return f"host={self.host} port={self.port} dbname={self.database} user={self.user} password={self.password}"
        

class DatabaseConnection:
    """Manages lifecycle of a PostgreSQL connection pool and cursor access."""

    def __init__(self, config: DatabaseConfig, min_conn=1, max_conn=10):
        """Store configuration and pool sizing before pool creation."""
        self.config = config
        self.connection_pool = None
        self.min_conn = min_conn
        self.max_conn = max_conn
        self._pool_reset_lock = Lock()

    def create_pool(self):
        """Create a connection pool with configured bounds; returns True on success."""
        try:
            ssl_mode = os.getenv('DB_SSLMODE', 'require')
            connect_timeout = int(os.getenv('DB_CONNECT_TIMEOUT', '10'))
            keepalives = int(os.getenv('DB_KEEPALIVES', '1'))
            keepalives_idle = int(os.getenv('DB_KEEPALIVES_IDLE', '30'))
            keepalives_interval = int(os.getenv('DB_KEEPALIVES_INTERVAL', '10'))
            keepalives_count = int(os.getenv('DB_KEEPALIVES_COUNT', '5'))
            pool_timeout = int(os.getenv('DB_POOL_TIMEOUT', '10'))
            pool_max_lifetime = int(os.getenv('DB_POOL_MAX_LIFETIME', '300'))
            pool_max_idle = int(os.getenv('DB_POOL_MAX_IDLE', '60'))
            statement_timeout_ms = int(os.getenv('DB_STATEMENT_TIMEOUT_MS', '12000'))
            idle_tx_timeout_ms = int(os.getenv('DB_IDLE_TX_TIMEOUT_MS', '10000'))
            conn_options = (
                f"-c statement_timeout={statement_timeout_ms} "
                f"-c idle_in_transaction_session_timeout={idle_tx_timeout_ms}"
            )

            if PSYCOPG_VERSION == 3:
                # psycopg3 connection string
                conninfo = (
                    f"host={self.config.host} "
                    f"port={self.config.port} "
                    f"dbname={self.config.database} "
                    f"user={self.config.user} "
                    f"password={self.config.password} "
                    f"sslmode={ssl_mode} "
                    f"connect_timeout={connect_timeout} "
                    f"keepalives={keepalives} "
                    f"keepalives_idle={keepalives_idle} "
                    f"keepalives_interval={keepalives_interval} "
                    f"keepalives_count={keepalives_count} "
                    f"options='{conn_options}'"
                )
                self.connection_pool = ConnectionPool(
                    conninfo=conninfo,
                    min_size=self.min_conn,
                    max_size=self.max_conn,
                    timeout=pool_timeout,
                    max_lifetime=pool_max_lifetime,
                    max_idle=pool_max_idle,
                )
            else:
                # psycopg2 connection pool
                self.connection_pool = psycopg2.pool.SimpleConnectionPool(
                    self.min_conn,
                    self.max_conn,
                    host=self.config.host,
                    port=self.config.port,
                    database=self.config.database,
                    user=self.config.user,
                    password=self.config.password,
                    sslmode=ssl_mode,
                    connect_timeout=connect_timeout,
                    keepalives=keepalives,
                    keepalives_idle=keepalives_idle,
                    keepalives_interval=keepalives_interval,
                    keepalives_count=keepalives_count,
                    options=conn_options,
                )
            print(f"Connection pool created successfully (psycopg{PSYCOPG_VERSION})")
            return True
        except Exception as e:
            print(f"Error creating connection pool: {e}")
            return False

    def _pool_is_usable(self) -> bool:
        """Return True when a connection pool exists and is not closed."""
        pool = self.connection_pool
        if pool is None:
            return False
        return not bool(getattr(pool, 'closed', False))

    def _close_pool_safely(self) -> None:
        """Close current pool object defensively to avoid leaked sessions."""
        pool = self.connection_pool
        if pool is None:
            return
        try:
            if PSYCOPG_VERSION == 3:
                pool.close()
            else:
                pool.closeall()
        except Exception as close_error:
            print(f"Pool close warning: {close_error}")
        finally:
            self.connection_pool = None

    def _ensure_pool(self) -> None:
        """Create or recreate the pool lazily when it is missing or closed."""
        if self._pool_is_usable():
            return

        with self._pool_reset_lock:
            if self._pool_is_usable():
                return

            # If a stale pool object exists, close it before creating a new one.
            self._close_pool_safely()
            if not self.create_pool():
                raise RuntimeError('Database connection pool is unavailable')

    @contextmanager
    def get_cursor(self, commit=False):
        """Yield a cursor from the pool; commit or rollback based on execution outcome."""
        if PSYCOPG_VERSION == 3:
            self._ensure_pool()
            # psycopg3 uses connection context manager
            retries = max(0, int(os.getenv('DB_POOL_ACQUIRE_RETRIES', '0')))
            for attempt in range(retries + 1):
                try:
                    acquire_started = time.perf_counter()
                    with self.connection_pool.connection() as connection:
                        acquired_ms = (time.perf_counter() - acquire_started) * 1000
                        route_label = _request_label()
                        if _diag_enabled() and acquired_ms >= _diag_acquire_warn_ms():
                            print(
                                f"DB acquire slow ({acquired_ms:.1f} ms) route={route_label} "
                                f"commit={commit} attempt={attempt + 1}/{retries + 1}"
                            )

                        with connection.cursor(row_factory=dict_row) as cursor:
                            operation_started = time.perf_counter()
                            try:
                                yield cursor
                                if commit:
                                    connection.commit()
                                else:
                                    connection.rollback()
                            except Exception as e:
                                try:
                                    if not connection.closed:
                                        connection.rollback()
                                except Exception as rollback_error:
                                    print(f"Rollback skipped due to lost connection: {rollback_error}")
                                print(f"Error during database operation: {e}")
                                raise
                            finally:
                                hold_ms = (time.perf_counter() - operation_started) * 1000
                                if _diag_enabled() and hold_ms >= _diag_hold_warn_ms():
                                    print(
                                        f"DB hold slow ({hold_ms:.1f} ms) route={route_label} "
                                        f"commit={commit}"
                                    )
                    return
                except PoolTimeout:
                    pool_stats = {}
                    try:
                        pool_stats = self.connection_pool.get_stats() if self.connection_pool else {}
                    except Exception:
                        pool_stats = {}

                    print(
                        f"Pool timeout while acquiring DB connection (max_conn={self.max_conn}, "
                        f"attempt={attempt + 1}/{retries + 1}, stats={pool_stats})."
                    )

                    if attempt < retries:
                        time.sleep(float(os.getenv('DB_POOL_RETRY_DELAY', '0.15')))
                        continue
                    raise
                except Exception as exc:
                    message = str(exc).lower()
                    if attempt < retries and ('pool closed' in message or 'pool is closed' in message):
                        self._close_pool_safely()
                        self._ensure_pool()
                        time.sleep(float(os.getenv('DB_POOL_RETRY_DELAY', '0.15')))
                        continue
                    raise
        else:
            # psycopg2 manual connection management
            self._ensure_pool()
            acquire_started = time.perf_counter()
            connection = self.connection_pool.getconn()
            acquired_ms = (time.perf_counter() - acquire_started) * 1000
            route_label = _request_label()
            if _diag_enabled() and acquired_ms >= _diag_acquire_warn_ms():
                print(
                    f"DB acquire slow ({acquired_ms:.1f} ms) route={route_label} "
                    f"commit={commit}"
                )

            cursor = connection.cursor(cursor_factory=RealDictCursor)
            should_close_connection = False
            operation_started = time.perf_counter()
            try:
                yield cursor
                if commit:
                    connection.commit()
                else:
                    connection.rollback()
            except Exception as e:
                should_close_connection = True
                try:
                    if not connection.closed:
                        connection.rollback()
                except Exception as rollback_error:
                    print(f"Rollback skipped due to lost connection: {rollback_error}")
                print(f"Error during database operation: {e}")
                raise
            finally:
                hold_ms = (time.perf_counter() - operation_started) * 1000
                if _diag_enabled() and hold_ms >= _diag_hold_warn_ms():
                    print(
                        f"DB hold slow ({hold_ms:.1f} ms) route={route_label} "
                        f"commit={commit}"
                    )
                try:
                    cursor.close()
                except Exception:
                    pass
                try:
                    self.connection_pool.putconn(connection, close=should_close_connection or bool(connection.closed))
                except Exception:
                    pass
    
    def close_pool(self):
        """Close all connections in the pool if it has been initialized."""
        if self.connection_pool:
            if PSYCOPG_VERSION == 3:
                self.connection_pool.close()
            else:
                self.connection_pool.closeall()
            print("Connection pool closed")

    def _get_pool_stats_safe(self) -> Dict[str, Any]:
        """Read pool stats defensively so debug endpoints never crash on stats fetch."""
        pool = self.connection_pool
        if pool is None or not hasattr(pool, 'get_stats'):
            return {}
        try:
            return pool.get_stats() or {}
        except Exception as stats_error:
            return {'stats_error': str(stats_error)}

    def _terminate_server_sessions(self, terminate_only_current_user: bool = True) -> Dict[str, Any]:
        """Terminate backend sessions in current database using a direct maintenance connection."""
        ssl_mode = os.getenv('DB_SSLMODE', 'require')
        connect_timeout = int(os.getenv('DB_CONNECT_TIMEOUT', '10'))

        conn = None
        cursor = None
        try:
            if PSYCOPG_VERSION == 3:
                conninfo = (
                    f"host={self.config.host} "
                    f"port={self.config.port} "
                    f"dbname={self.config.database} "
                    f"user={self.config.user} "
                    f"password={self.config.password} "
                    f"sslmode={ssl_mode} "
                    f"connect_timeout={connect_timeout}"
                )
                conn = psycopg.connect(conninfo, autocommit=True)
                cursor = conn.cursor(row_factory=dict_row)
            else:
                conn = psycopg2.connect(
                    host=self.config.host,
                    port=self.config.port,
                    database=self.config.database,
                    user=self.config.user,
                    password=self.config.password,
                    sslmode=ssl_mode,
                    connect_timeout=connect_timeout,
                )
                conn.autocommit = True
                cursor = conn.cursor(cursor_factory=RealDictCursor)

            query = """
                SELECT pid
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
            """
            if terminate_only_current_user:
                query += " AND usename = current_user"

            cursor.execute(query)
            rows = cursor.fetchall() or []
            pids = [row.get('pid') for row in rows if row.get('pid')]

            terminated = 0
            for pid in pids:
                cursor.execute("SELECT pg_terminate_backend(%s) AS terminated", (pid,))
                result = cursor.fetchone() or {}
                if bool(result.get('terminated')):
                    terminated += 1

            return {
                'requested_terminations': len(pids),
                'terminated_sessions': terminated,
                'terminate_only_current_user': terminate_only_current_user,
            }
        except Exception as e:
            return {
                'requested_terminations': 0,
                'terminated_sessions': 0,
                'terminate_only_current_user': terminate_only_current_user,
                'termination_error': str(e),
            }
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def force_reset_pool(self, terminate_sessions: bool = False, terminate_only_current_user: bool = True) -> Dict[str, Any]:
        """Force-close the local pool and recreate it; optionally terminate DB sessions first."""
        with self._pool_reset_lock:
            before_stats = self._get_pool_stats_safe()
            self._close_pool_safely()

            termination_result: Dict[str, Any] = {
                'requested_terminations': 0,
                'terminated_sessions': 0,
                'terminate_only_current_user': terminate_only_current_user,
            }
            if terminate_sessions:
                termination_result = self._terminate_server_sessions(
                    terminate_only_current_user=terminate_only_current_user
                )

            recreated = self.create_pool()
            after_stats = self._get_pool_stats_safe() if recreated else {}

            return {
                'pool_recreated': bool(recreated),
                'before_pool_stats': before_stats,
                'after_pool_stats': after_stats,
                'termination': termination_result,
            }

class QueryBuilder:
    """Provides helper methods to build parameterized SQL queries."""

    @staticmethod
    def select(table, columns=None, where=None, order_by=None, limit=None):
        """Construct a SELECT statement with optional columns, filters, ordering, and limit."""
        col_str = "*" if not columns else ", ".join(columns)
        query = f"SELECT {col_str} FROM {table}"
        params = []
        if where:
            conditions = []
            for key,value in where.items():
                conditions.append(f"{key} = %s")
                params.append(value)
            query += " WHERE " + " AND ".join(conditions)
        if order_by:
            query += f" ORDER BY {order_by}"
        if limit:
            query += f" LIMIT {limit}"
        return query, params

    @staticmethod
    def insert(table, data):
        """Construct an INSERT statement returning the inserted row."""
        columns = list(data.keys())
        values = list(data.values())
        col_str = ", ".join(columns)
        placeholders = ", ".join(["%s"]*len(columns))
        query = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) RETURNING *"
        return query, values

    @staticmethod
    def update(table, data, where):
        """Construct an UPDATE statement with filters, returning the updated row."""
        set_parts = []
        params = []
        for key, value in data.items():
            set_parts.append(f"{key} = %s")
            params.append(value)
        
        query = f"UPDATE {table} SET  {', '.join(set_parts)}"
        if where:
            conditions = []
            for key, value in where.items():
                conditions.append(f"{key} = %s")
                params.append(value)
            query += " WHERE " + " AND ".join(conditions)
        query += " RETURNING *"
        return query, params

    @staticmethod
    def delete(table, where):
        """Construct a DELETE statement with filters, returning the deleted row."""
        params = []
        query = f"DELETE FROM {table}"
        if where:
            conditions = []
            for key, value in where.items():
                conditions.append(f"{key} = %s")
                params.append(value)
            query += " WHERE " + " AND ".join(conditions)
        query += " RETURNING *"
        return query, params
    
class Model:
    """Lightweight base model offering CRUD helpers tied to a specific table."""

    def __init__(self, db_connection, table_name):
        """Bind the model to a database connection and target table."""
        self.db = db_connection
        self.table_name = table_name
        self.query_builder = QueryBuilder()

    def _run_with_retry(self, operation):
        """Retry transient connection failures for read-only operations."""
        retries = max(0, int(os.getenv('DB_QUERY_RETRIES', '1')))
        delay_seconds = max(0.0, float(os.getenv('DB_QUERY_RETRY_DELAY', '0.2')))

        attempt = 0
        while True:
            try:
                return operation()
            except DB_OPERATIONAL_ERRORS as e:
                should_retry = attempt < retries and _is_transient_db_error(e)
                if not should_retry:
                    raise
                attempt += 1
                print(f"Transient DB error detected (attempt {attempt}/{retries}). Retrying read operation...")
                time.sleep(delay_seconds)

    def find_all(self, where=None, order_by=None,limit=None):
        """Fetch multiple rows with optional filters, ordering, and limit."""
        query, params = self.query_builder.select(
            self.table_name, where=where, order_by=order_by, limit=limit
        )

        def _op():
            with self.db.get_cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()

        return self._run_with_retry(_op)
    
    def find_by_id(self, id_value, id_column="id"):
        """Fetch a single row by primary key or specified identifier column."""
        query, params = self.query_builder.select(
            self.table_name, where={id_column: id_value}
        )

        def _op():
            with self.db.get_cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()

        return self._run_with_retry(_op)
    
    def create(self, data):
        """Insert a new record and return the inserted row."""
        query, params = self.query_builder.insert(self.table_name, data)
        with self.db.get_cursor(commit=True) as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()

    def update(self, data, where):
        """Update matching records and return the updated row."""
        query, params = self.query_builder.update(self.table_name, data, where)
        with self.db.get_cursor(commit=True) as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()

    def delete(self, where):
        """Delete matching records and return the deleted row."""
        query, params = self.query_builder.delete(self.table_name, where)
        with self.db.get_cursor(commit=True) as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()

    def execute_raw(self, query, params=None, commit=False):
        """Execute arbitrary SQL with optional parameters; returns fetched rows when available."""
        def _op():
            with self.db.get_cursor(commit=commit) as cursor:
                cursor.execute(query, params or [])
                # Only fetch when the statement produced a result set (e.g., SELECT or RETURNING).
                if cursor.description is None:
                    return []
                return cursor.fetchall()

        # Retry only non-committing raw operations to avoid duplicate writes.
        if commit:
            return _op()
        return self._run_with_retry(_op)

class Migration:
    """Provides generic helpers to execute schema migrations (DDL/DML operations)."""

    def __init__(self, db_connection):
        """Store the shared database connection for migration operations."""
        self.db = db_connection
    
    def execute(self, sql, description):
        """Execute arbitrary SQL; log success or exception. Useful for all DDL/schema work."""
        try:
            with self.db.get_cursor(commit=True) as cursor:
                cursor.execute(sql)
            msg = description if description else "Migration executed"
            print(f"✓ {msg}")
        except Exception as e:
            print(f"✗ Error: {e}")
            raise





# Example usage:
if __name__ == "__main__":
    config = DatabaseConfig(
        host="localhost",
        port=5432,
        database="testdb",
        user="testuser",
        password="testpassword"
    )
    db = DatabaseConnection(config)
    if db.create_pool():
        migration = Migration(db)
        migration.create_users_table()
        user_model = Model(db, "users")
        new_user = user_model.create({
            "name": "John Doe", 
            "email": "john@email.com"
        })
        print(f"Created User: {new_user}")
        all_users = user_model.find_all()
        print(f"All Users: {all_users}")

        first_user = user_model.find_by_id(new_user['id'])
        print(f"First User: {first_user}")

        updated_user = user_model.update(
            {"name": "Jane Doe"},
            {"id": new_user['id']}
        )
        print(f"Updated User: {updated_user}")

        db.close_pool()
