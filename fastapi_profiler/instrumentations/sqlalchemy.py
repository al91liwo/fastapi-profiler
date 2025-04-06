import time
import traceback
from typing import Any, Dict, Optional

from sqlalchemy import event

from fastapi_profiler.instrumentations.base import BaseInstrumentation

# TODO: Replace fucked print statements with a proper logging framework


class SQLAlchemyInstrumentation(BaseInstrumentation):
    """SQLAlchemy-specific instrumentation implementation"""

    # Track instrumented engines to avoid duplicate instrumentation
    _instrumented_engines = set()

    @classmethod
    def instrument(cls, engine: Any) -> None:
        """Instrument a SQLAlchemy engine"""
        # Skip if already instrumented
        engine_id = id(engine)
        if engine_id in cls._instrumented_engines:
            print(f"FastAPI Profiler: Engine {engine} already instrumented, skipping")
            return

        print(f"FastAPI Profiler: Instrumenting SQLAlchemy engine {engine}")

        try:
            engine_metadata = cls._extract_engine_metadata(engine)

            # Store metadata on the engine for later use
            if not hasattr(engine, "_profiler_metadata"):
                engine._profiler_metadata = engine_metadata

                # Use the standard SQLAlchemy event API
                # Define event handlers
                def before_execute(conn, cursor, stmt, params, context, executemany):
                    try:
                        context._query_start = time.perf_counter()
                        # Store the original statement,
                        # parameters and metadata for debugging
                        context._stmt = stmt
                        context._params = params
                        context._engine_metadata = getattr(
                            engine, "_profiler_metadata", {}
                        )
                        context._query_type = cls._detect_query_type(stmt)
                    except Exception as e:
                        print(f"FastAPI Profiler: Error in before_execute: {str(e)}")

                def after_execute(conn, cursor, stmt, params, context, executemany):
                    try:
                        duration = time.perf_counter() - getattr(
                            context, "_query_start", 0
                        )

                        # Use the stored statement if available,
                        # otherwise use the provided one
                        statement = getattr(context, "_stmt", stmt)

                        # Get metadata about the query
                        metadata = getattr(context, "_engine_metadata", {}).copy()

                        # Add query type to metadata
                        query_type = getattr(
                            context, "_query_type", cls._detect_query_type(statement)
                        )
                        if query_type:
                            metadata["query_type"] = query_type

                        # Add parameter info if available (safely)
                        if hasattr(context, "_params") and context._params:
                            try:
                                # Only include parameter count for security/privacy
                                if isinstance(context._params, dict):
                                    metadata["param_count"] = len(context._params)
                                elif isinstance(context._params, (list, tuple)):
                                    metadata["param_count"] = len(context._params)
                            except Exception:
                                pass

                        # Format SQL query before tracking
                        try:
                            import sqlparse

                            formatted_statement = sqlparse.format(
                                statement,
                                reindent=True,
                                keyword_case="upper",
                                indent_width=2,
                                strip_comments=False,
                            )
                            # Add formatted SQL to metadata
                            metadata["formatted_sql"] = formatted_statement
                        except ImportError:
                            # If sqlparse is not installed, use the original statement
                            pass
                        except Exception as e:
                            print(f"FastAPI Profiler: Error formatting SQL: {str(e)}")

                        # Track the query through the class method
                        cls.track_query(duration, statement, metadata)

                        # Debug log for slow queries
                        if duration > 0.1:  # TODO: Move to configurable threshold
                            print(
                                f"FastAPI Profiler: Tracked slow SQL query"
                                f" ({duration:.4f}s): {statement[:100]}..."
                            )
                            print(
                                f"  Query type: {query_type}, "
                                f"  Database: {metadata.get('dialect', 'unknown')}"
                            )
                    except Exception as e:
                        print(f"FastAPI Profiler: Error in after_execute: {str(e)}")
                        traceback.print_exc()

                try:
                    # Try using SQLAlchemy's event system
                    @event.listens_for(engine, "before_cursor_execute")
                    def _before_execute_wrapper(
                        conn, cursor, stmt, params, context, executemany
                    ):
                        return before_execute(
                            conn, cursor, stmt, params, context, executemany
                        )

                    @event.listens_for(engine, "after_cursor_execute")
                    def _after_execute_wrapper(
                        conn, cursor, stmt, params, context, executemany
                    ):
                        return after_execute(
                            conn, cursor, stmt, params, context, executemany
                        )

                    print(
                        "FastAPI Profiler: Successfully registered SQLAlchemy"
                        " event listeners"
                    )
                except Exception as e:
                    print(
                        f"FastAPI Profiler: Error registering SQLAlchemy "
                        f"event listeners: {str(e)}"
                    )

            # Mark this engine as instrumented, engine_id
            cls._instrumented_engines.add(engine_id)

        except Exception as e:
            print(f"FastAPI Profiler: Failed to instrument engine {engine}: {str(e)}")
            traceback.print_exc()

    @classmethod
    def uninstrument(cls, engine: Any) -> None:
        """Remove SQLAlchemy instrumentation"""
        engine_id = id(engine)
        if engine_id not in cls._instrumented_engines:
            print(
                f"FastAPI Profiler: Engine {engine} not instrumented,"
                f" skipping uninstrumentation"
            )
            return

        try:
            # SQLAlchemy doesn't provide a clean way to remove listeners AFAIK
            # TODO: investigate if there's a way to unregister event listeners safely
            # Remove from instrumented engines set
            cls._instrumented_engines.discard(engine_id)
            print(f"FastAPI Profiler: Uninstrumented engine {engine}")
        except Exception as e:
            print(f"FastAPI Profiler: Error uninstrumenting engine {engine}: {str(e)}")

    @staticmethod
    def _extract_engine_metadata(engine: Any) -> Dict[str, Any]:
        """Extract metadata from a SQLAlchemy engine."""
        metadata = {}

        try:
            # Get dialect name
            if hasattr(engine, "dialect") and hasattr(engine.dialect, "name"):
                dialect_name = str(engine.dialect.name).lower()
                metadata["dialect"] = dialect_name

                # Format a display name based on URL and engine ID
                engine_name = None

                # Try to get a meaningful name from the URL
                if hasattr(engine, "url"):
                    url = str(engine.url)
                    # Try to extract database name from URL safely
                    # without revealing credentials
                    if "/" in url:
                        db_name = url.split("/")[-1]
                        if db_name and db_name not in ("", "."):
                            # Remove extension if present
                            if "." in db_name:
                                db_name = db_name.split(".")[0]
                            engine_name = f"{db_name.capitalize()}DB"

                # If we still don't have a name, use dialect with unique ID
                if not engine_name:
                    engine_id = id(engine) % 10000
                    engine_name = f"{dialect_name.capitalize()}Engine_{engine_id:04d}"

                metadata["name"] = engine_name

                # Also store version info if available
                version_info = getattr(engine.dialect, "server_version_info", None)
                if version_info:
                    if isinstance(version_info, tuple) and len(version_info) >= 3:
                        version_str = ".".join(str(x) for x in version_info[:3])
                        metadata["version"] = version_str

            # Get URL info (without credentials)
            if hasattr(engine, "url"):
                url = str(engine.url)
                # Remove username/password for security
                safe_url = url.split("://")
                if len(safe_url) > 1 and "@" in safe_url[1]:
                    credentials, rest = safe_url[1].split("@", 1)
                    safe_url = f"{safe_url[0]}://****:****@{rest}"
                    metadata["url"] = safe_url
                else:
                    metadata["url"] = "???"  # TODO: investigate

            # Get engine name/id for identification
            if hasattr(engine, "name"):
                metadata["engine_name"] = engine.name
            else:
                # Use object id as fallback
                metadata["engine_id"] = id(engine)

        except Exception as e:
            print(f"FastAPI Profiler: Error extracting engine metadata: {str(e)}")

        return metadata

    @staticmethod
    def _detect_query_type(statement: str) -> Optional[str]:
        """Detect the type of SQL query from the statement."""
        # TODO: replace with sqlparse or move to rustcore,
        #  N.B. this is a simple implementation
        if not statement:
            return None

        # Normalize statement for easier detection
        stmt_lower = statement.strip().lower()

        # Detect query type based on first word
        if stmt_lower.startswith("select"):
            return "SELECT"
        elif stmt_lower.startswith("insert"):
            return "INSERT"
        elif stmt_lower.startswith("update"):
            return "UPDATE"
        elif stmt_lower.startswith("delete"):
            return "DELETE"
        elif stmt_lower.startswith("create"):
            return "CREATE"
        elif stmt_lower.startswith("alter"):
            return "ALTER"
        elif stmt_lower.startswith("drop"):
            return "DROP"
        elif stmt_lower.startswith("with"):
            # For CTEs, look for the actual operation after the WITH clause
            if " select " in stmt_lower:
                return "WITH-SELECT"
            elif " insert " in stmt_lower:
                return "WITH-INSERT"
            elif " update " in stmt_lower:
                return "WITH-UPDATE"
            elif " delete " in stmt_lower:
                return "WITH-DELETE"
            return "WITH"
        elif stmt_lower.startswith("begin"):
            return "BEGIN"
        elif stmt_lower.startswith("commit"):
            return "COMMIT"
        elif stmt_lower.startswith("rollback"):
            return "ROLLBACK"

        # Default case
        return "UNKNOWN"
