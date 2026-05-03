"""Compatibility launcher for the autarkic Mycelia web server.

The previous local file-backed demo has been retired. Use this module when old
scripts still call mycelia_web_server.py; it simply starts the SQL-free Flask
frontend from mycelia_web_mysql.py.
"""
from mycelia_web_mysql import app

if __name__ == "__main__":
    print("--- Mycelia Web Server (DAD/OpenCL Backend) ---")
    app.run(debug=True, use_reloader=False)
